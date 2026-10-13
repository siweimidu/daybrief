# 🤖 AI 日报

每天自动聚合 5 个 AI 信息源，用大模型批量提炼要点、写出编者按与点评，**北京时间 07:00** 同时推送到 **邮箱 · Telegram · 网站 · Markdown 归档**。

> 站点：https://siweimidu.github.io/daybrief/ ｜ RSS：https://siweimidu.github.io/daybrief/rss.xml

---

## 一、它做了什么

| 环节 | 说明 |
|---|---|
| 抓取 | 拉取 5 个 RSS / JSON Feed，取近 48 小时新内容 |
| 去重 | 按链接去重，按发布时间倒序，最多取 30 条 |
| 摘要 | 8 条一批交给大模型，输出「标题 / 摘要 / 要点 / 分类」 |
| 成稿 | 主编模型合成 4-6 段编者按 + 今日要点 + 分类统计 |
| 分发 | 写 Markdown/JSON → 发邮件 → 发 Telegram → 部署网站 |

**信息源**：橘鸦AI早报 · AI Daily · DailyDawn 中文 · Simon Willison · Hacker News

**模型策略**：Groq（主，`openai/gpt-oss-120b`）→ Gemini（备，`gemini-flash-lite-latest`）。
任一供应商**日额度耗尽立即拉黑并切换**，绝不空等；批量摘要把 28 条资讯压缩到 4 次调用。

---

## 二、怎么用（读者视角）

| 方式 | 地址 |
|---|---|
| 网页阅读 | https://siweimidu.github.io/daybrief/ |
| 全部归档 | https://siweimidu.github.io/daybrief/archive.html |
| RSS 订阅 | https://siweimidu.github.io/daybrief/rss.xml |
| Markdown 原文 | `daily-md/YYYY-MM-DD.md` |
| 结构化 JSON | `output/YYYY-MM-DD.json` |

**RSS 用法**

- 阅读器（Feedly / Inoreader / Follow）：粘贴 `https://siweimidu.github.io/daybrief/rss.xml`
- n8n：`RSS Read` 节点 → URL 填上面的地址
- 邮箱 / Telegram：仓库 workflow 每天 07:00 自动推送，无需配置

---

## 三、自己部署一份

### 1. 拿代码

```bash
git clone https://github.com/siweimidu/daybrief.git
cd daybrief
```

### 2. 改信息源

编辑 `config.yaml`：

```yaml
sources:
  rss:
    - url: "https://daily.juya.uk/rss.xml"
      name: "橘鸦AI早报"
    - url: "https://你的源/rss.xml"
      name: "你的源"
```

支持 RSS 2.0 / Atom / JSON Feed 三种格式。

### 3. 配 Secrets

仓库 `Settings → Secrets and variables → Actions`：

| 名称 | 必填 | 说明 |
|---|---|---|
| `GROQ_API_KEY` | ✅ | https://console.groq.com/keys 免费申请 |
| `GEMINI_API_KEY` | 备 | https://aistudio.google.com/apikey，主模型额度用尽时兜底 |
| `SMTP_HOST` / `SMTP_PORT` | 邮件 | QQ 邮箱：`smtp.qq.com` / `465` |
| `SMTP_USERNAME` | 邮件 | 你的邮箱地址 |
| `SMTP_PASSWORD` | 邮件 | **授权码**（不是登录密码） |
| `MAIL_FROM_EMAIL` | 邮件 | 发件地址 |
| `DAYBRIEF_RECIPIENTS` | 邮件 | 收件地址，多个用逗号分隔 |
| `TELEGRAM_BOT_TOKEN` | 可选 | 找 @BotFather 创建 bot 获取 |
| `TELEGRAM_CHAT_ID` | 可选 | 给 bot 发一条消息后，从 `getUpdates` 里取 |

### 4. 开 Pages

`Settings → Pages → Source` 选 **GitHub Actions**。

### 5. 跑第一次

`Actions → AI Daily Brief → Run workflow`。

定时任务在 `.github/workflows/ai-daily-brief.yml`：

```yaml
schedule:
  - cron: "0 23 * * *"   # 23:00 UTC = 北京时间 07:00
```

---

## 四、本地运行

```bash
pip install pyyaml
export GROQ_API_KEY=xxx
export GEMINI_API_KEY=yyy

python3 scripts/groq_brief.py --config config.yaml --lookback 48h   # 生成 output/ 与 daily-md/
python3 scripts/build_site.py                                        # 本地预览站点 site/
```

常用参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--lookback` | `48h` | 抓取时间窗口 |
| `--max` | `30` | 最多处理多少条（省额度） |
| `--batch` | `8` | 每批摘要条数，越大越省调用次数 |
| `--config` | `config.yaml` | 配置文件路径 |

环境变量：`BRIEF_DEADLINE_S`（整体超时秒数）、`GEMINI_MODEL`、`GROQ_MODEL`。

---

## 五、目录结构

```
daybrief/
├── config.yaml                        # 信息源配置
├── scripts/
│   ├── groq_brief.py                  # 主引擎：抓取 → 摘要 → 合成 → 输出
│   └── build_site.py                  # 站点生成：首页 / 归档 / 详情 / RSS
├── daily-md/YYYY-MM-DD.md             # 每期 Markdown 存档
├── output/YYYY-MM-DD.json             # 结构化数据（供二次加工）
├── output/YYYY-MM-DD.html             # 邮件正文
├── site/                              # 生成的静态站点（Pages 部署源）
└── .github/workflows/ai-daily-brief.yml
```

---

## 六、常见问题

**Q：某天没生成？**
看 Actions 日志。常见原因是模型日额度用尽（Groq 免费层 20 万 token/天）——第二天 00:00 UTC 自动恢复；备用 Gemini 免费层约 20 次请求/天，批量化后够用。

**Q：想换模型？**
改 `vars.GROQ_MODEL` / `vars.GEMINI_MODEL`（仓库 Variables），或本地用环境变量覆盖。

**Q：想加信息源？**
在 `config.yaml` 的 `sources.rss` 里追加一行即可，无需改代码。

**Q：Telegram 收不到？**
先给 bot 发一条消息（否则 bot 不能主动找你），再确认 `TELEGRAM_CHAT_ID` 正确。

---

MIT License
