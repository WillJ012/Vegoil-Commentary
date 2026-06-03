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
import time
import imaplib
import smtplib
from datetime import datetime, timedelta, timezone
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
def _load_dotenv(path=".env"):
    """本地运行时，从同目录 .env 文件读取配置（云端用 Secrets，无此文件，跳过）。"""
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()


def env(key, default=None, required=False):
    """读取环境变量；空字符串视为未设置（回落到 default）。"""
    v = os.environ.get(key)
    if v is None or v.strip() == "":
        if required:
            raise KeyError(key)
        return default
    return v.strip()


# 收邮件 (IMAP)
IMAP_HOST = env("IMAP_HOST", required=True)
IMAP_PORT = int(env("IMAP_PORT", 993))
IMAP_USER = env("IMAP_USER", required=True)
IMAP_PASS = env("IMAP_PASS", required=True)               # 邮箱授权码
MAILBOX   = env("MAILBOX", "INBOX")

# Fastmarkets 登录（走 cookie 方案时这两个可不填）
FM_USERNAME = env("FM_USERNAME")                          # 你的 Fastmarkets 登录邮箱
FM_PASSWORD = env("FM_PASSWORD")                          # 你的 Fastmarkets 登录密码
# 本地 capture_session.py 抓到的会话 JSON（推荐方案：填了就用它，跳过登录）
FM_STORAGE_STATE = env("FM_STORAGE_STATE")

# 发邮件 (SMTP)
SMTP_HOST = env("SMTP_HOST", required=True)
SMTP_PORT = int(env("SMTP_PORT", 465))
SMTP_USER = env("SMTP_USER", IMAP_USER)
SMTP_PASS = env("SMTP_PASS", IMAP_PASS)
SMTP_SSL  = env("SMTP_SSL", "true").lower() == "true"
MAIL_TO   = env("MAIL_TO", IMAP_USER)
MAIL_FROM = env("MAIL_FROM", SMTP_USER)

# 识别 Fastmarkets 那封邮件
SENDER_CONTAINS  = env("SENDER_CONTAINS", "fastmarkets").lower()
SUBJECT_CONTAINS = env("SUBJECT_CONTAINS", "vegetable oils").lower()
LOOKBACK_DAYS    = int(env("LOOKBACK_DAYS", 2))
# 从邮件正文提取下载链接的正则（默认抓 downloads.fastmarkets.com/newsletter 链接）
LINK_REGEX = env("LINK_REGEX", r'https://downloads\.fastmarkets\.com/newsletter/[^\s"\'<>]+')

# MiniMax（OpenAI 兼容）
MINIMAX_API_KEY  = env("MINIMAX_API_KEY", required=True)
MINIMAX_BASE_URL = env("MINIMAX_BASE_URL", "https://api.minimax.io/v1")
MODEL = env("MINIMAX_MODEL", "MiniMax-M2.5")

DEBUG_DIR = "debug"


def log(msg):
    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z] {msg}", flush=True)


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

    since = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).strftime("%d-%b-%Y")
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

    # 调试：列出邮件里所有 newsletter 下载链接（去掉 utm 追踪参数便于看清期号）
    all_links = re.findall(r'https://downloads\.fastmarkets\.com/newsletter/[^\s"\'<>]+', html)
    seen = []
    for L in all_links:
        short = L.split("?")[0]
        if short not in seen:
            seen.append(short)
    log(f"邮件中找到 {len(seen)} 个不同的下载链接（已去重去追踪参数）：")
    for L in seen:
        # 拆成两行打印，避免界面把长链接截断
        log(f"  期号尾段 = {L.split('/')[-1]}")
        log(f"  完整 = {L}")

    m = re.search(LINK_REGEX, html)
    if m:
        chosen = m.group(0)
        log(f"最终选用尾段 = {chosen.split('?')[0].split('/')[-1]}")
        return chosen
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
        log("填入登录密码")
        page.fill(pass_sel, FM_PASSWORD)
    else:                                        # 分两步：先邮箱后密码
        _click_submit(page)
        page.wait_for_selector(pass_sel, timeout=30000)
        log("填入登录密码")
        page.fill(pass_sel, FM_PASSWORD)

    # 提交后，关键：不要去打断后续的 OAuth form_post 自动回跳，
    # 让浏览器自己把跳转链走完即可。
    log("提交登录，等待 OAuth 回跳完成 ...")
    _click_submit(page)
    try:
        page.wait_for_load_state("networkidle", timeout=60000)
    except Exception:
        pass
    page.wait_for_timeout(5000)   # 给 form_post 自动提交留出余量


def _looks_logged_out(page):
    # 仍停在登录域名且还能看到密码框 = 登录没成功
    return ("auth.fastmarkets" in page.url) and bool(page.query_selector("input[type=password]"))


def _fetch_pdf(ctx, pdf_url, tries=3):
    for attempt in range(1, tries + 1):
        resp = ctx.request.get(pdf_url, headers={"Accept": "application/pdf"})
        if resp.status == 200:
            body = resp.body()
            if body[:4] == b"%PDF":
                return body
            log(f"第 {attempt} 次：返回 200 但不是 PDF（会话可能失效），重试 ...")
        else:
            log(f"第 {attempt} 次：HTTP {resp.status}，重试 ...")
        time.sleep(4)
    return None


def download_pdf_with_login(pdf_url):
    storage = None
    if FM_STORAGE_STATE:
        with open("fm_session.json", "w", encoding="utf-8") as f:
            f.write(FM_STORAGE_STATE)
        storage = "fm_session.json"
        log("已加载登录会话 cookie（来自 Secret）")
    elif os.path.exists("fm_session.json"):
        storage = "fm_session.json"
        log("已加载本地 fm_session.json 会话 cookie")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        ctx = browser.new_context(
            storage_state=storage,
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
            viewport={"width": 1366, "height": 900},
            locale="en-US",
        )

        # ───── 路线①：有保存的 cookie → 直接取 PDF，不走登录 ─────
        if storage:
            log("用保存的会话直接请求 PDF ...")
            data = _fetch_pdf(ctx, pdf_url)
            if data:
                log(f"✅ 拿到 PDF（{len(data)} 字节）")
                browser.close()
                return data
            page = ctx.new_page()
            try:
                page.goto(pdf_url, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass
            _dump(page, "session_expired")
            browser.close()
            raise RuntimeError(
                "保存的登录会话已失效（或被境外IP反爬拦截）。"
                "请在本地重新运行 capture_session.py 抓新 cookie，更新 GitHub Secret 中的 FM_STORAGE_STATE。"
            )

        # ───── 路线②：没有保存 cookie → 退回交互式登录（冷启动，可能 500）─────
        page = ctx.new_page()
        log("（未提供 FM_STORAGE_STATE）尝试交互式登录 ...")
        try:
            page.goto(pdf_url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            log(f"首次打开有跳转中断属正常：{e}")

        if "auth.fastmarkets" in page.url or page.query_selector("input[type=password]"):
            log(f"被重定向到登录页：{page.url}")
            try:
                _do_login(page)
            except Exception as e:
                _dump(page, "login_failed")
                browser.close()
                raise RuntimeError(f"登录步骤出错：{e}")

        if _looks_logged_out(page):
            _dump(page, "still_logged_out")
            browser.close()
            raise RuntimeError("提交后仍停在登录页：账号密码可能不对，或被反爬拦截。")

        data = _fetch_pdf(ctx, pdf_url)
        if data is None:
            _dump(page, "download_failed")
            browser.close()
            raise RuntimeError("交互式登录后仍拿不到 PDF（多半是 signin-oidc 冷启动 500）。"
                               "建议改用 cookie 方案：本地跑 capture_session.py。")
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
# 术语对照表：发现译得不对的词，照着格式往这里加一行即可（不用懂代码）。
# 左边是英文原文，右边是你要的中文译法。
GLOSSARY = {
    "down the curve": "在远期曲线上",
    "along the curve": "沿远期曲线",
    "front-month": "近月（首行）合约",
    "most-active contract": "主力合约",
    "basis": "基差",
    "premium": "升水",
    "discount": "贴水",
    "FOB": "FOB（离岸）",
    "CIF": "CIF（到岸）",
    "feedstock": "原料",
    "blending mandate": "掺混强制比例",
}

# 把术语表渲染成提示词里的一段
_GLOSSARY_LINES = "\n".join(f"- {en} → {zh}" for en, zh in GLOSSARY.items())

PROMPT_TEMPLATE = """你是一名专业的大宗商品市场翻译兼分析。下面是 Fastmarkets 每日植物油 newsletter 的文本（双栏 PDF 抽取，可能有少量排版/连字瑕疵，请结合上下文阅读）。已不含价格数据表。

请输出两部分：

【第一部分：完整翻译】
找到正文最完整的那篇 "Vegoils commentary" 评论（通常是 Top stories 里最长的一篇），把它**完整、忠实地逐段翻译成中文**——不要概括、不要省略，原文有几段就译几段，所有数字、价格、合约月份、机构与人名、引述都保留。

【第二部分：其他内容摘要】
把 newsletter 里**除这篇 Vegoils commentary 之外**的其他文字内容（其它品类评论如 Soybean/Corn commentary、生柴/EIA 等新闻报道、Palm Rotterdam closing 等），每条用一句中文概括要点。只总结文字类内容，忽略价格数据表。

术语用中文，必要处保留英文缩写（CME、CPO、RINs、FOB、RSO 等）。
**以下术语必须严格按此对照表翻译，不得自行改译：**
{glossary}

输出要求：**只输出最终 HTML 片段，不要任何思考过程、说明或前言**。不要 markdown、不要 ```、不要 <html>/<body> 外壳。严格按下面结构，第一行必须就是标题：
<h2>{date} FASTMARKETS 植物油评论简报</h2>
<p><strong>原文标题：</strong>（该篇评论的英文标题）</p>
<p>……逐段译文，每段一个 p……</p>
<hr>
<h3>其他内容摘要</h3>
<ul>
<li><strong>（标题/分类）：</strong>一句话要点</li>
……
</ul>

newsletter 原文如下：
----------
{body}
----------"""


def _clean_model_html(text):
    """去掉思考过程/代码围栏，只保留从 <h2> 开始的正式译文。"""
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    # 思考型模型可能在正文前加一段推理：从第一个 <h2> 起截取
    i = text.find("<h2")
    if i != -1:
        text = text[i:]
    return text.strip()


def summarize_to_chinese(news_text, date_str):
    client = OpenAI(api_key=MINIMAX_API_KEY, base_url=MINIMAX_BASE_URL)
    prompt = PROMPT_TEMPLATE.format(date=date_str, glossary=_GLOSSARY_LINES, body=news_text)
    log(f"调用 MiniMax（{MODEL}）翻译 ...")
    resp = client.chat.completions.create(
        model=MODEL,
        max_tokens=8000,
        temperature=0.2,
        messages=[
            {"role": "system", "content": "你是专业的大宗商品翻译，只输出忠实完整的中文译文，不输出任何思考过程或额外说明。"},
            {"role": "user", "content": prompt},
        ],
    )
    html = _clean_model_html(resp.choices[0].message.content or "")
    if not html or "<h2" not in html:
        raise RuntimeError("MiniMax 返回内容异常（无正文标题），请检查模型名/额度/base_url。")
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
<p style="color:#999;font-size:12px;">由 Fastmarkets 每日 newsletter 自动翻译生成，仅供个人参考。原始内容版权归 Fastmarkets 所有。</p>
</body></html>"""

    # 清洗收件人：去掉换行/空白，拆成干净的地址列表
    recipients = [a.strip() for a in re.split(r"[,\s]+", MAIL_TO) if a.strip()]
    to_header = ", ".join(recipients)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr(("植物油简报", MAIL_FROM.strip()))
    msg["To"] = to_header
    msg.attach(MIMEText(re.sub(r"<[^>]+>", "", html_body), "plain", "utf-8"))
    msg.attach(MIMEText(full_html, "html", "utf-8"))

    log(f"发送邮件到 {to_header} ...")
    if SMTP_SSL:
        server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT)
        server.starttls(context=ssl.create_default_context())
    server.login(SMTP_USER, SMTP_PASS)
    server.sendmail(MAIL_FROM.strip(), recipients, msg.as_string())
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
