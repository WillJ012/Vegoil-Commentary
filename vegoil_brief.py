#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每天自动：
  1) 登录邮箱(IMAP)，找到最新一封 Fastmarkets 植物油 newsletter，下载 PDF 附件
  2) 抽取其中的 "Vegoils commentary" 评论正文（处理双栏排版）
  3) 用 Claude 翻译 + 总结成中文简报
  4) 通过 SMTP 把简报发到你的邮箱

所有配置都从环境变量读取（在 GitHub Secrets 里填）。本地测试可改用 .env / export。
"""

import os
import io
import re
import sys
import ssl
import imaplib
import smtplib
from datetime import datetime, timedelta
from email import message_from_bytes
from email.header import decode_header, make_header
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import parsedate_to_datetime, formataddr

import pdfplumber
from openai import OpenAI

# ──────────────────────────────────────────────────────────────────────────
# 配置（全部来自环境变量）
# ──────────────────────────────────────────────────────────────────────────
# 收邮件 (IMAP)
IMAP_HOST = os.environ["IMAP_HOST"]                       # 例: imap.exmail.qq.com
IMAP_PORT = int(os.environ.get("IMAP_PORT", "993"))
IMAP_USER = os.environ["IMAP_USER"]                       # 你的完整邮箱地址
IMAP_PASS = os.environ["IMAP_PASS"]                       # 邮箱「授权码/客户端专用密码」，不是登录密码
MAILBOX   = os.environ.get("MAILBOX", "INBOX")

# 发邮件 (SMTP) —— 可与收件邮箱相同
SMTP_HOST = os.environ["SMTP_HOST"]                       # 例: smtp.exmail.qq.com
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SMTP_USER", IMAP_USER)
SMTP_PASS = os.environ.get("SMTP_PASS", IMAP_PASS)
SMTP_SSL  = os.environ.get("SMTP_SSL", "true").lower() == "true"   # 465用SSL；587用STARTTLS则设false

MAIL_TO   = os.environ.get("MAIL_TO", IMAP_USER)          # 简报发到哪（默认发回自己）
MAIL_FROM = os.environ.get("MAIL_FROM", SMTP_USER)

# 识别 Fastmarkets 那封邮件用的条件（命中任一即可）
SENDER_CONTAINS  = os.environ.get("SENDER_CONTAINS", "fastmarkets").lower()
SUBJECT_CONTAINS = os.environ.get("SUBJECT_CONTAINS", "vegetable oils").lower()
LOOKBACK_DAYS    = int(os.environ.get("LOOKBACK_DAYS", "2"))   # 往回找几天的邮件

# MiniMax（OpenAI 兼容接口）
MINIMAX_API_KEY  = os.environ["MINIMAX_API_KEY"]
# 国际站用 https://api.minimax.io/v1 ；国内站用 https://api.minimaxi.com/v1
# 用哪个取决于你的 API key 在哪个平台申请的，填错会鉴权失败
MINIMAX_BASE_URL = os.environ.get("MINIMAX_BASE_URL", "https://api.minimax.io/v1")
MODEL = os.environ.get("MINIMAX_MODEL", "MiniMax-M2.5")


def log(msg):
    print(f"[{datetime.utcnow():%Y-%m-%d %H:%M:%S}Z] {msg}", flush=True)


# ──────────────────────────────────────────────────────────────────────────
# 1) 从邮箱抓最新的 Fastmarkets PDF
# ──────────────────────────────────────────────────────────────────────────
def _decode(s):
    if not s:
        return ""
    try:
        return str(make_header(decode_header(s)))
    except Exception:
        return s


def fetch_latest_pdf():
    log(f"连接 IMAP {IMAP_HOST}:{IMAP_PORT} ...")
    M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    M.login(IMAP_USER, IMAP_PASS)
    M.select(MAILBOX)

    since = (datetime.utcnow() - timedelta(days=LOOKBACK_DAYS)).strftime("%d-%b-%Y")
    typ, data = M.search(None, f'(SINCE {since})')
    if typ != "OK":
        raise RuntimeError("IMAP 搜索失败")

    ids = data[0].split()
    log(f"近 {LOOKBACK_DAYS} 天共 {len(ids)} 封邮件，按时间倒序筛选 Fastmarkets...")

    candidates = []  # (date, msg_id)
    for mid in reversed(ids):
        typ, msg_data = M.fetch(mid, "(RFC822)")
        if typ != "OK":
            continue
        msg = message_from_bytes(msg_data[0][1])
        frm  = _decode(msg.get("From", "")).lower()
        subj = _decode(msg.get("Subject", "")).lower()
        if SENDER_CONTAINS in frm or SUBJECT_CONTAINS in subj:
            try:
                dt = parsedate_to_datetime(msg.get("Date"))
            except Exception:
                dt = datetime.min
            # 该邮件是否带 PDF 附件
            pdf_bytes = _extract_pdf_attachment(msg)
            if pdf_bytes:
                candidates.append((dt, _decode(msg.get("Subject", "")), pdf_bytes))

    M.logout()

    if not candidates:
        return None, None
    candidates.sort(key=lambda x: x[0], reverse=True)
    dt, subj, pdf = candidates[0]
    log(f"选中邮件：{subj}  ({dt})")
    return subj, pdf


def _extract_pdf_attachment(msg):
    for part in msg.walk():
        fname = _decode(part.get_filename() or "")
        ctype = part.get_content_type()
        if ctype == "application/pdf" or fname.lower().endswith(".pdf"):
            payload = part.get_payload(decode=True)
            if payload:
                return payload
    return None


# ──────────────────────────────────────────────────────────────────────────
# 2) 抽取 PDF 文本（按左右双栏分别抽，避免串栏），并在价格表前截断
# ──────────────────────────────────────────────────────────────────────────
def extract_news_text(pdf_bytes):
    out = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            mid = page.width / 2
            left  = page.crop((0, 0, mid, page.height)).extract_text() or ""
            right = page.crop((mid, 0, page.width, page.height)).extract_text() or ""
            out.append(left + "\n" + right)
    full = "\n".join(out).replace("\x00", "")  # 剔除丢失的连字控制符，交给模型按上下文还原

    # 价格表部分对简报没用，截掉以省 token
    cut = len(full)
    for marker in ["All soybean oil prices", "Symbol Description Date Price"]:
        i = full.find(marker)
        if i != -1:
            cut = min(cut, i)
    news = full[:cut].strip()

    if "Vegoils commentary" not in news:
        log("⚠️ 未在新闻区找到 'Vegoils commentary'，将把整段新闻文本交给模型自行判断。")
    return news


# ──────────────────────────────────────────────────────────────────────────
# 3) Claude 翻译 + 总结成中文简报（输出 HTML 片段）
# ──────────────────────────────────────────────────────────────────────────
PROMPT_TEMPLATE = """你是一名大宗商品市场分析助理。下面是 Fastmarkets 每日植物油 newsletter 的文本（双栏 PDF 抽取，可能有少量排版/连字瑕疵，请结合上下文阅读）。

任务：
1. 从中找到**正文最完整的那篇 "Vegoils commentary" 评论**（通常是 Top stories 里最长的一篇）。
2. 把它翻译并**总结**成一份简洁的中文晨报，覆盖：核心结论、主要驱动因素、关键期货价格变动、现货/基差要点。
3. 只针对 Vegoils commentary 这一篇。**忽略**价格数据表、谷物(corn/wheat)评论、钢铁/金属等其它板块。
4. 保留具体数字（涨跌幅、价格、合约月份）。术语用中文，必要处保留英文缩写（CME、CPO、RINs、FOB 等）。

输出格式：直接输出 HTML 片段（不要 markdown、不要 ```、不要 <html>/<body> 外壳）。结构如下：
<h2>{date} 植物油评论简报</h2>
<p><strong>核心：</strong>……一两句……</p>
<h3>主要驱动</h3><ul><li>……</li>…</ul>
<h3>期货</h3><ul><li>……</li>…</ul>
<h3>现货 / 基差</h3><ul><li>……</li>…</ul>
<p><strong>一句话：</strong>……</p>

newsletter 原文如下：
----------
{body}
----------"""


def summarize_to_chinese(news_text, date_str):
    client = OpenAI(api_key=MINIMAX_API_KEY, base_url=MINIMAX_BASE_URL)
    prompt = PROMPT_TEMPLATE.format(date=date_str, body=news_text)
    log(f"调用 MiniMax（{MODEL}）生成简报 ...")
    resp = client.chat.completions.create(
        model=MODEL,
        max_tokens=4000,
        temperature=0.3,
        messages=[
            {"role": "system", "content": "你是一名严谨的大宗商品市场分析助理，输出简洁、数字准确。"},
            {"role": "user", "content": prompt},
        ],
    )
    html = (resp.choices[0].message.content or "").strip()
    # 去掉模型偶尔包的代码围栏
    html = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", html).strip()
    if not html:
        raise RuntimeError("MiniMax 返回空内容，请检查模型名/额度/base_url。")
    return html


# ──────────────────────────────────────────────────────────────────────────
# 4) 发邮件
# ──────────────────────────────────────────────────────────────────────────
def send_email(subject, html_body):
    full_html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;
             line-height:1.6;color:#1a1a1a;max-width:680px;margin:0 auto;padding:16px;">
{html_body}
<hr style="margin-top:24px;border:none;border-top:1px solid #e5e5e5;">
<p style="color:#999;font-size:12px;">由 Fastmarkets 每日 newsletter 自动翻译总结生成，仅供个人参考。原始数据版权归 Fastmarkets 所有。</p>
</body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr(("植物油简报", MAIL_FROM))
    msg["To"] = MAIL_TO
    msg.attach(MIMEText(re.sub(r"<[^>]+>", "", html_body), "plain", "utf-8"))  # 纯文本兜底
    msg.attach(MIMEText(full_html, "html", "utf-8"))

    log(f"发送邮件到 {MAIL_TO} ...")
    if SMTP_SSL:
        server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT)
        server.starttls(context=ssl.create_default_context())
    server.login(SMTP_USER, SMTP_PASS)
    server.sendmail(MAIL_FROM, [a.strip() for a in MAIL_TO.split(",")], msg.as_string())
    server.quit()
    log("✅ 已发送")


# ──────────────────────────────────────────────────────────────────────────
def main():
    subj, pdf_bytes = fetch_latest_pdf()
    if not pdf_bytes:
        log("没找到带 PDF 的 Fastmarkets 邮件（可能今天还没到）。正常退出。")
        return  # 不报错，等下次定时再跑

    news = extract_news_text(pdf_bytes)
    date_str = datetime.now().strftime("%Y-%m-%d")
    html = summarize_to_chinese(news, date_str)

    # 取简报里第一行 h2/strong 当邮件标题，取不到就用默认
    m = re.search(r"<h2>(.*?)</h2>", html)
    title = re.sub(r"<[^>]+>", "", m.group(1)) if m else f"{date_str} 植物油评论简报"
    send_email(f"【植物油简报】{title}", html)


if __name__ == "__main__":
    try:
        main()
    except KeyError as e:
        log(f"❌ 缺少环境变量 {e}，请检查 GitHub Secrets 是否填全。")
        sys.exit(1)
    except Exception as e:
        log(f"❌ 运行出错：{type(e).__name__}: {e}")
        sys.exit(1)
