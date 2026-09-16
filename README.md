# 每日植物油评论简报（自动）

**分工**：你每天（时间不限）在本地跑一下 `capture_session.py` 刷新登录 cookie，它会自动推送到 GitHub Secret；云端 GitHub Actions 每天 08:45 定时跑整套：读邮箱拿 newsletter 下载链接 → 用你刷新的 cookie 下载 PDF → 抽 **Vegoils commentary** → 大模型翻译总结 → 发到你邮箱。

> 这样定时的稳定性交给云端，登录态的新鲜度靠你每天刷一次。下载链接需要登录、且无头浏览器“冷启动登录”会被服务端 500，所以用你真浏览器登录后的 cookie 来绕过。

---

## 一、一次性准备

### 1. 企业邮箱 IMAP + 授权码
邮箱网页端开 IMAP，生成**授权码**（非登录密码）。常见：腾讯企业邮 imap/smtp.exmail.qq.com、阿里 imap/smtp.qiye.aliyun.com、网易 imaphz/smtphz.qiye.163.com。

### 2. 大模型 API Key

当前默认走 **OpenCode Zen**：`LLM_BASE_URL` 默认 `https://opencode.ai/zen/go/v1`，`LLM_MODEL` 默认 `deepseek-v4.1-flash`。
在 opencode.ai 登录后加账单、复制 API Key，填到仓库 Secret `LLM_API_KEY` 即可（另外两个有默认值，通常不用填）。

换别家（OpenAI 兼容的都行）：把 `LLM_BASE_URL` 和 `LLM_MODEL` 两个 Secret 覆盖成目标服务的值。
注意 `max_tokens` 上限各家不同，如果报 400 就调小 `LLM_MAX_TOKENS`。

### 3. GitHub Token（给本地脚本改 Secret 的权限）
GitHub → Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → 只授权你这个仓库 → Repository permissions → **Secrets: Read and write** → 生成并复制。

### 4. 本地装环境
```
pip install -r requirements.txt
python -m playwright install chromium
```

### 5. 本地建 `.env`
复制 `.env.example` 为 `.env`，至少填：
```
GH_TOKEN=github_pat_xxx
GH_REPO=你的用户名/仓库名
```

---

## 二、建仓库 & 传文件

GitHub 新建**私有**仓库，传（注意 `.env` 和 `fm_session.json` **不要**传上去）：
```
vegoil_brief.py
capture_session.py
requirements.txt
README.md
LOCAL_RUN.md
.env.example
.github/workflows/daily-vegoil-brief.yml
```

---

## 三、填云端 Secrets

仓库 Settings → Secrets and variables → Actions：

| 名称 | 值 | 必填 |
|---|---|---|
| `IMAP_HOST` | 如 imap.exmail.qq.com | ✅ |
| `IMAP_USER` | 你的完整邮箱地址 | ✅ |
| `IMAP_PASS` | 邮箱授权码 | ✅ |
| `SMTP_HOST` | 如 smtp.exmail.qq.com | ✅ |
| `LLM_API_KEY` | 大模型 API Key（当前 OpenCode Zen） | ✅ |
| `FM_STORAGE_STATE` | 留空即可——首次跑 capture_session.py 会自动创建/填充 | （自动） |
| `MAIL_TO` | 收件地址，默认发回自己 | 选填 |
| 其它 `IMAP_PORT`/`SMTP_PORT`/`SMTP_SSL`/`LLM_BASE_URL`/`LLM_MODEL`/`LLM_MAX_TOKENS`/`SENDER_CONTAINS`/`SUBJECT_CONTAINS`/`LINK_REGEX` | 见 `.env.example`，不填用默认 | 选填 |

---

## 四、每天的操作（你只做这一步）

```
python capture_session.py
```
弹出浏览器 → 登录 Fastmarkets → 回终端按回车。
脚本保存 `fm_session.json` 并**自动更新** GitHub Secret `FM_STORAGE_STATE`。
看到 `✅ 已自动更新 GitHub Secret` 就行了，剩下交给云端。

> 时间不限，早晚刷都行，只要在云端 08:45 跑之前刷过当天即可。其实 cookie 通常能撑几天，不一定每天都要刷——但每天刷最保险。

---

## 五、云端定时 & 手动测试

- 定时：工作流本身**没有 `cron` 了**，实际由 cron-job.org 用 GitHub API 每天 **00:20 UTC = 北京 08:20** 触发（仅工作日）。改时间要去 cron-job.org 那个任务上改。
- 手动测：Actions → Run workflow。日志出现 `已加载登录会话 cookie（来自 Secret）` → `拿到 PDF` → `已发送` = 成功。
- **测试时只想发给自己**：点 Run workflow 时在 `test_to` 输入框里填你自己的邮箱，这次运行就只发这个地址，不会打扰正常收件人；留空则按 `MAIL_TO` 正常发送（cron-job.org 定时走的也是这条）。
- 若 `保存的登录会话已失效`：cookie 过期或被反爬，重跑 capture_session.py 刷新；持续不行见下。

---

## 六、万一云端境外 IP 被反爬

cookie 在你住宅 IP 抓、云端用境外 IP 复用，极少数情况会被 Imperva 拦。若反复 `会话已失效` 但本地 cookie 明明新鲜，多半是 IP 问题 → 改本地跑整套（见 `LOCAL_RUN.md`）。

---

## 七、调整
- 简报措辞/结构：`vegoil_brief.py` 的 `PROMPT_TEMPLATE`。
- 邮件样式：`send_email()` 的 HTML 模板。
