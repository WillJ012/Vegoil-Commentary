#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每天自动：
  1) 读邮箱(IMAP)，找到最新一封 Fastmarkets 植物油 newsletter，提取其中的下载链接
  2) 用 Playwright 无头浏览器登录 Fastmarkets，下载该链接的 PDF
  3) 抽取 PDF 中的 "Vegoils commentary"（处理双栏排版）
  4) 用 MiniMax 翻译 + 总结成中文简报
  5) 通过 SMTP 把简报发到你的邮箱

⚠️ 前提：Fastmarkets 登录无 2FA；云端可能被反爬拦截，失败时看 debug/ 截图。
所有配置走环境变量（GitHub Secrets）。
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
from playwright.sync_api import sync_playwright

# ──────────────────────────────────────────────────────────────────────────
# 配置
# ──────────────────────────────────────────────────────────────────────────
# 收邮件 (IMAP)
IMAP_HOST = os.environ["IMAP_HOST"]
IMAP_PORT = int(os.environ.get("IMAP_PORT", "993"))
IMAP_USER = os.environ["IMAP_USER"]
IMAP_PASS = os.environ["IMAP_PASS"]               # 邮箱授权码
MAILBOX   = os.environ.get("MAILBOX", "INBOX")

# Fastmarkets 登录
FM_USERNAME = os.environ["FM_USERNAME"]           # 你的 Fastmarkets 登录邮箱
FM_PASSWORD = os.environ["FM_PASSWORD"]           # 你的 Fastmarkets 登录密码

# 发邮件 (SMTP)
SMTP_HOST = os.environ["SMTP_HOST"]
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SMTP_USER", IMAP_USER)
SMTP_PASS = os.environ.get("SMTP_PASS", IMAP_PASS)
SMTP_SSL  = os.environ.get("SMTP_SSL", "true").lower() == "true"
MAIL_TO   = os.environ.get("MAIL_TO", IMAP_USER)
MAIL_FROM = os.environ.get("MAIL_FROM", SMTP_USER)

# 识别 Fastmarkets 那封邮件
SENDER_CONTAINS  = os.environ.get("SENDER_CONTAINS", "fastmarkets").lower()
SUBJECT_CONTAINS = os.environ.get("SUBJECT_CONTAINS", "vegetable oils").lower()
LOOKBACK_DAYS    = int(os.environ.get("LOOKBACK_DAYS", "2"))
# 从邮件正文提取下载链接的正则（默认抓 downloads.fastmarkets.com/newsletter 链接）
LINK_REGEX = os.environ.get("LINK_REGEX", r'https://downloads\.fastmarkets\.com/newsletter/[^\s"\'<>]+')

# MiniMax（OpenAI 兼容）
MINIMAX_API_KEY  = os.environ["MINIMAX_API_KEY"]
MINIMAX_BASE_URL = os.environ.get("MINIMAX_BASE_URL", "https://api.minimax.io/v1")
MODEL = os.environ.get("MINIMAX_MODEL", "MiniMax-M2.5")

DEBUG_DIR = "debug"


def log(msg):
    print(f"[{datetime.utcnow():%Y-%m-%d %H:%M:%S}Z] {msg}", flush=True)


def _decode(s):
    if not s:
        return ""
    try:
        return str(make_header(decode_header(s)))
    except Exception:
        return s


# ──────────────────────────────────────────────────────────────────────────
# 1) 从邮箱拿到当天的下载链接
# ──────────────────────────────────────────────────────────────────────────
def fetch_newsletter_link():
    log(f"连接 IMAP {IMAP_HOST}:{IMAP_PORT} ...")
    M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    M.login(IMAP_USER, IMAP_PASS)
    M.select(MAILBOX)

    since = (datetime.utcnow() - timedelta(days=LOOKBACK_DAYS)).strftime("%d-%b-%Y")
    typ, data = M.search(None, f'(SINCE {since})')
    ids = data[0].split() if typ == "OK" else []
    log(f"近 {LOOKBACK_DAYS} 天 {len(ids)} 封邮件，倒序找 Fastmarkets...")

    best = None  # (date, link)
    for mid in reversed(ids):
        typ, msg_data = M.fetch(mid, "(RFC822)")
        if typ != "OK":
            continue
        msg = message_from_bytes(msg_data[0][1])
        frm  = _decode(msg.get("From", "")).lower()
        subj = _decode(msg.get("Subject", "")).lower()
        if SENDER_CONTAINS in frm or SUBJECT_CONTAINS in subj:
            link = _extract_link(msg)
            if link:
                try:
                    dt = parsedate_to_datetime(msg.get("Date"))
                except Exception:
                    dt = datetime.min
                if best is None or dt > best[0]:
                    best = (dt, link, _decode(msg.get("Subject", "")))
    M.logout()

    if not best:
        return None
    log(f"选中邮件：{best[2]} ({best[0]})")
    log(f"下载链接：{best[1][:80]}...")
    return best[1]


def _extract_link(msg):
    html = ""
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            payload = part.get_payload(decode=True)
            if payload:
                html += payload.decode(part.get_content_charset() or "utf-8", "ignore")
    html = html.replace("&amp;", "&")
    m = re.search(LINK_REGEX, html)
    if m:
        return m.group(0)
    # 兜底：任何含 newsletter 的 fastmarkets 链接
    m = re.search(r'https://[^\s"\'<>]*fastmarkets[^\s"\'<>]*newsletter[^\s"\'<>]*', html)
    return m.group(0) if m else None


# ──────────────────────────────────────────────────────────────────────────
# 2) Playwright 登录并下载 PDF
# ──────────────────────────────────────────────────────────────────────────
def _dump(page, tag):
    os.makedirs(DEBUG_DIR, exist_ok=True)
    try:
        page.screenshot(path=f"{DEBUG_DIR}/{tag}.png", full_page=True)
        with open(f"{DEBUG_DIR}/{tag}.html", "w", encoding="utf-8") as f:
            f.write(page.content())
        log(f"已保存调试快照 debug/{tag}.png / .html（当前 URL: {page.url}）")
    except Exception as e:
        log(f"保存调试快照失败: {e}")


def _click_submit(page):
    for sel in ["button:has-text('Sign in')", "button:has-text('Log in')",
                "button:has-text('Continue')", "button:has-text('Next')",
                "button[type=submit]", "input[type=submit]"]:
        el = page.query_selector(sel)
        if el and el.is_visible():
            el.click()
            return True
    return False


def _do_login(page):
    email_sel = ("input[type=email], input[name=username], input[name=email], "
                 "input[name=Username], #username, #Username, #Email")
    pass_sel  = "input[type=password], input[name=password], #password, #Password"

    page.wait_for_selector(email_sel, timeout=30000)
    log("填入登录邮箱")
    page.fill(email_sel, FM_USERNAME)

    if page.query_selector(pass_sel):           # 邮箱+密码同屏
        page.fill(pass_sel, FM_PASSWORD)
        _click_submit(page)
    else:                                        # 分两步：先邮箱后密码
        _click_submit(page)
        page.wait_for_selector(pass_sel, timeout=30000)
        log("填入登录密码")
        page.fill(pass_sel, FM_PASSWORD)
        _click_submit(page)


def download_pdf_with_login(pdf_url):
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
            viewport={"width": 1366, "height": 900},
            locale="en-US",
        )
        page = ctx.new_page()
        log("打开下载链接，等待是否跳转登录 ...")
        page.goto(pdf_url, wait_until="domcontentloaded", timeout=60000)

        if "auth.fastmarkets" in page.url or page.query_selector("input[type=password]"):
            log(f"被重定向到登录页：{page.url}")
            try:
                _do_login(page)
                page.wait_for_url(lambda u: "auth.fastmarkets" not in u, timeout=60000)
                log(f"登录后跳转到：{page.url}")
            except Exception as e:
                _dump(page, "login_failed")
                raise RuntimeError(f"登录失败（可能被反爬拦截或选择器对不上）：{e}")

        # 用已登录会话直接请求 PDF 字节
        log("用已登录会话请求 PDF ...")
        resp = ctx.request.get(pdf_url, headers={"Accept": "application/pdf"})
        if resp.status != 200:
            _dump(page, "download_http_error")
            browser.close()
            raise RuntimeError(f"下载 PDF 失败 HTTP {resp.status}")
        data = resp.body()
        if data[:4] != b"%PDF":
            _dump(page, "not_a_pdf")
            browser.close()
            raise RuntimeError("返回内容不是 PDF（多半仍未登录成功 / 被反爬拦截）")
        log(f"✅ 拿到 PDF（{len(data)} 字节）")
        browser.close()
        return data


# ──────────────────────────────────────────────────────────────────────────
# 3) 抽取 PDF 文本（双栏分别抽），价格表前截断
# ──────────────────────────────────────────────────────────────────────────
def extract_news_text(pdf_bytes):
    out = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            mid = page.width / 2
            left  = page.crop((0, 0, mid, page.height)).extract_text() or ""
            right = page.crop((mid, 0, page.width, page.height)).extract_text() or ""
            out.append(left + "\n" + right)
    full = "\n".join(out).replace("\x00", "")

    cut = len(full)
    for marker in ["All soybean oil prices", "Symbol Description Date Price"]:
        i = full.find(marker)
        if i != -1:
            cut = min(cut, i)
    news = full[:cut].strip()
    if "Vegoils commentary" not in news:
        log("⚠️ 未找到 'Vegoils commentary'，交给模型自行判断。")
    return news


# ──────────────────────────────────────────────────────────────────────────
# 4) MiniMax 翻译总结
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
    html = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", html).strip()
    if not html:
        raise RuntimeError("MiniMax 返回空内容，请检查模型名/额度/base_url。")
    return html


# ──────────────────────────────────────────────────────────────────────────
# 5) 发邮件
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
    msg.attach(MIMEText(re.sub(r"<[^>]+>", "", html_body), "plain", "utf-8"))
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
    link = fetch_newsletter_link()
    if not link:
        log("没找到带下载链接的 Fastmarkets 邮件（可能今天还没到）。正常退出。")
        return
    pdf_bytes = download_pdf_with_login(link)
    news = extract_news_text(pdf_bytes)
    date_str = datetime.now().strftime("%Y-%m-%d")
    html = summarize_to_chinese(news, date_str)

    m = re.search(r"<h2>(.*?)</h2>", html)
    title = re.sub(r"<[^>]+>", "", m.group(1)) if m else f"{date_str} 植物油评论简报"
    send_email(f"【植物油简报】{title}", html)


if __name__ == "__main__":
    try:
        main()
    except KeyError as e:
        log(f"❌ 缺少环境变量 {e}，请检查 GitHub Secrets。")
        sys.exit(1)
    except Exception as e:
        log(f"❌ 运行出错：{type(e).__name__}: {e}")
        sys.exit(1)
