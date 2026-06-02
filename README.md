# 每日植物油评论简报（自动）

每天早上自动：从邮箱抓 Fastmarkets 植物油 newsletter PDF → 抽出 **Vegoils commentary** → 用 Claude 翻译总结成中文晨报 → 发到你邮箱。全程跑在 GitHub Actions 上，电脑关着也不影响，免费。

---

## 一、准备工作

### 1. 开启企业邮箱的 IMAP/SMTP，拿「授权码」
登录密码通常不能直接用于第三方程序，需要邮箱里单独生成的**授权码 / 客户端专用密码**。在邮箱网页端设置里找「IMAP/SMTP 服务」开启并生成授权码。常见服务商的服务器地址：

| 企业邮箱 | IMAP 主机 (993/SSL) | SMTP 主机 (465/SSL) |
|---|---|---|
| 腾讯企业邮 exmail | imap.exmail.qq.com | smtp.exmail.qq.com |
| 阿里企业邮 | imap.qiye.aliyun.com | smtp.qiye.aliyun.com |
| 网易企业邮 | imaphz.qiye.163.com | smtphz.qiye.163.com |
| Coremail / 自建 | 问你司 IT | 问你司 IT |

> 不确定主机地址或是否允许海外 IP（GitHub 跑在境外）连接，问一句你司 IT 最快。若企业邮箱完全禁止外部 IMAP，再回来找我，我把它改成「转发到 Gmail 再读」的版本。

### 2. 准备 MiniMax API Key
登录 MiniMax 平台（国际站 https://platform.minimax.io ，国内站 https://platform.minimaxi.com），在「接口密钥 / interface-key」页面新建一个 API Key。
注意 **key 和 endpoint 要配套**：
- 在 **platform.minimax.io** 申请的 key → base_url 用 `https://api.minimax.io/v1`（脚本默认值）
- 在 **platform.minimaxi.com（国内）** 申请的 key → 需把 `MINIMAX_BASE_URL` 设为 `https://api.minimaxi.com/v1`

---

## 二、建仓库

1. 在 GitHub 新建一个**私有**仓库（保护隐私）。
2. 把这三样按原结构传上去：
   ```
   vegoil_brief.py
   requirements.txt
   .github/workflows/daily-vegoil-brief.yml
   ```

---

## 三、填 Secrets

仓库 → **Settings → Secrets and variables → Actions → New repository secret**，逐个添加：

| 名称 | 值 | 必填 |
|---|---|---|
| `IMAP_HOST` | 如 imap.exmail.qq.com | ✅ |
| `IMAP_USER` | 你的完整邮箱地址 | ✅ |
| `IMAP_PASS` | 邮箱**授权码**（非登录密码） | ✅ |
| `SMTP_HOST` | 如 smtp.exmail.qq.com | ✅ |
| `MINIMAX_API_KEY` | MiniMax 的 API Key | ✅ |
| `MAIL_TO` | 简报收件地址（多个用逗号隔开），不填默认发回自己 | 选填 |
| `IMAP_PORT` | 默认 993，一般不用填 | 选填 |
| `SMTP_PORT` | 默认 465，一般不用填 | 选填 |
| `SMTP_SSL` | 默认 true（465）；若用 587 端口填 false | 选填 |
| `MINIMAX_BASE_URL` | 默认 `https://api.minimax.io/v1`；国内 key 填 `https://api.minimaxi.com/v1` | 选填 |
| `MINIMAX_MODEL` | 默认 `MiniMax-M2.5` | 选填 |
| `SENDER_CONTAINS` | 识别发件人关键词，默认 `fastmarkets` | 选填 |
| `SUBJECT_CONTAINS` | 识别主题关键词，默认 `vegetable oils` | 选填 |

> 收发同一个邮箱时，`SMTP_USER`/`SMTP_PASS` 会自动复用 `IMAP_USER`/`IMAP_PASS`，不用单独填。

---

## 四、先手动测一次

仓库 → **Actions → 「每日植物油简报」→ Run workflow**。
跑完看日志：

- 看到 `选中邮件：...` 和 `✅ 已发送` → 成功，去邮箱收简报。
- `没找到带 PDF 的 Fastmarkets 邮件` → 当天 newsletter 还没到，或发件人/主题关键词没匹配上 → 调 `SENDER_CONTAINS` / `SUBJECT_CONTAINS`。
- `缺少环境变量` → 有 Secret 没填。
- IMAP/SMTP 登录报错 → 多半是用了登录密码而非授权码，或 IMAP 服务没开。

---

## 五、定时时间

工作流默认 **00:45 UTC = 北京/新加坡 08:45** 每天触发。
改时间：编辑 `.github/workflows/daily-vegoil-brief.yml` 里的 `cron`，把目标**本地时间减 8 小时**填进去（格式：分 时 日 月 周）。没有 newsletter 的日子脚本会自动空跑退出，不会报错。
> 注：GitHub Actions 定时任务在高峰期可能延迟几分钟到十几分钟触发，属正常现象。

---

## 六、想调整

- **简报的措辞 / 板块结构**：改 `vegoil_brief.py` 里的 `PROMPT_TEMPLATE`。
- **想多保留几篇评论**（如 Soybean commentary）：在 prompt 里把「只针对 Vegoils commentary」放宽。
- **邮件样式**：改 `send_email()` 里的 HTML 模板。
