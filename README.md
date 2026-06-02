# 每日植物油评论简报（自动）

每天早上自动：读邮箱拿到 Fastmarkets newsletter 的下载链接 → 用无头浏览器登录 Fastmarkets 下载 PDF → 抽出 **Vegoils commentary** → 用 MiniMax 翻译总结成中文晨报 → 发到你邮箱。跑在 GitHub Actions 上，免费。

> ⚠️ 重要前提，先读：
> 1. **你的 Fastmarkets 登录必须无 2FA / 短信验证码**，否则自动登录会卡在验证码上，无法自动化。
> 2. Fastmarkets 有反爬（Imperva）。GitHub 跑在境外数据中心 IP，**首跑有被拦截的可能**。若被拦，见末尾「反爬兜底」。

---

## 一、准备工作

### 1. 开启企业邮箱 IMAP，拿授权码
脚本要读邮箱找那封 newsletter、提取下载链接。在邮箱网页端开启「IMAP/SMTP 服务」并生成**授权码 / 客户端专用密码**（不是登录密码）。常见服务商：

| 企业邮箱 | IMAP (993/SSL) | SMTP (465/SSL) |
|---|---|---|
| 腾讯企业邮 exmail | imap.exmail.qq.com | smtp.exmail.qq.com |
| 阿里企业邮 | imap.qiye.aliyun.com | smtp.qiye.aliyun.com |
| 网易企业邮 | imaphz.qiye.163.com | smtphz.qiye.163.com |
| Coremail / 自建 | 问你司 IT | 问你司 IT |

### 2. Fastmarkets 登录账号密码
就是你平时登 dashboard 用的邮箱 + 密码，分别填进 `FM_USERNAME` / `FM_PASSWORD`。

### 3. MiniMax API Key
登录 MiniMax 平台（国际站 https://platform.minimax.io ，国内站 https://platform.minimaxi.com），在「接口密钥」页新建 API Key。
**key 与 endpoint 要配套**：
- platform.minimax.io 的 key → base_url 用默认 `https://api.minimax.io/v1`
- platform.minimaxi.com（国内）的 key → 把 `MINIMAX_BASE_URL` 设为 `https://api.minimaxi.com/v1`

---

## 二、建仓库

1. GitHub 新建**私有**仓库。
2. 按原结构传：
   ```
   vegoil_brief.py
   requirements.txt
   README.md
   .github/workflows/daily-vegoil-brief.yml
   ```

---

## 三、填 Secrets

仓库 → Settings → Secrets and variables → Actions → New repository secret：

| 名称 | 值 | 必填 |
|---|---|---|
| `IMAP_HOST` | 如 imap.exmail.qq.com | ✅ |
| `IMAP_USER` | 你的完整邮箱地址 | ✅ |
| `IMAP_PASS` | 邮箱**授权码** | ✅ |
| `FM_USERNAME` | Fastmarkets 登录邮箱 | ✅ |
| `FM_PASSWORD` | Fastmarkets 登录密码 | ✅ |
| `SMTP_HOST` | 如 smtp.exmail.qq.com | ✅ |
| `MINIMAX_API_KEY` | MiniMax API Key | ✅ |
| `MAIL_TO` | 收件地址（多个逗号隔开），不填默认发回自己 | 选填 |
| `IMAP_PORT` / `SMTP_PORT` | 默认 993 / 465 | 选填 |
| `SMTP_SSL` | 默认 true；用 587 则填 false | 选填 |
| `MINIMAX_BASE_URL` | 国内 key 填 `https://api.minimaxi.com/v1` | 选填 |
| `MINIMAX_MODEL` | 默认 `MiniMax-M2.5` | 选填 |
| `SENDER_CONTAINS` | 识别发件人关键词，默认 `fastmarkets` | 选填 |
| `SUBJECT_CONTAINS` | 识别主题关键词，默认 `vegetable oils` | 选填 |
| `LINK_REGEX` | 提取下载链接的正则，默认抓 downloads.fastmarkets.com/newsletter | 选填 |

---

## 四、先手动测一次

Actions → 「每日植物油简报」→ Run workflow，看日志：

- `选中邮件` → `拿到 PDF` → `已发送` = 成功，去邮箱收简报。
- `没找到带下载链接的邮件` → 当天还没到，或发件人/主题关键词没匹配 → 调 `SENDER_CONTAINS`/`SUBJECT_CONTAINS`；若邮件里的链接不是 downloads.fastmarkets.com 开头，调 `LINK_REGEX`。
- `登录失败` / `返回内容不是 PDF` → 多半被反爬拦或登录选择器对不上。这时去本次运行页面底部下载 **debug-snapshots** 工件，里面有失败截图(.png)和页面源码(.html)，把它发我，我据此调登录选择器。

---

## 五、定时时间

默认 **00:45 UTC = 北京/新加坡 08:45** 每天触发。改时间：编辑工作流里的 `cron`，目标本地时间减 8 小时（格式：分 时 日 月 周）。GitHub 定时可能延迟几到十几分钟，正常。

---

## 六、反爬兜底（万一云端被拦）

如果 GitHub 的境外 IP 被 Fastmarkets 反爬挡住（debug 截图显示验证码/拦截页），最有效的办法是**换成用你自己的电脑跑**，住宅 IP 通过率高得多：

- **自托管 runner**：在你常开的电脑上装 GitHub self-hosted runner，工作流的 `runs-on` 改成 `self-hosted` 即可，其它不变。
- **或本地定时**：把 `vegoil_brief.py` 放本地，配 `.env` 后用 Mac 的 launchd / Windows 任务计划每天定时跑。

需要哪种我可以再给你具体步骤。

---

## 七、想调整

- 简报措辞/结构：改 `vegoil_brief.py` 里的 `PROMPT_TEMPLATE`。
- 登录页选择器对不上：改 `_do_login()` 里的 `email_sel` / `pass_sel`（拿 debug 截图给我我帮你改）。
- 邮件样式：改 `send_email()` 的 HTML 模板。
