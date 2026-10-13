#!/usr/bin/env python3
"""把 daily-md/*.md 生成为静态站点：首页（介绍+教程+最新一期）、归档页、每期详情、RSS。

用法：python3 scripts/build_site.py [--md daily-md] [--out site] [--url https://xxx.github.io/repo]
"""
from __future__ import annotations

import argparse
import glob
import html
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_URL = os.environ.get("SITE_URL", "https://siweimidu.github.io/daybrief")
REPO_URL = os.environ.get("REPO_URL", "https://github.com/siweimidu/daybrief")

CSS = """
*{box-sizing:border-box}
body{margin:0;background:#f6f7f9;color:#1f2933;line-height:1.75;
 font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei',sans-serif}
a{color:#2563eb;text-decoration:none}
a:hover{text-decoration:underline}
.wrap{max-width:780px;margin:0 auto;padding:0 20px}
header.site{background:#111827;color:#fff;padding:14px 0}
header.site .wrap{display:flex;align-items:center;gap:18px;flex-wrap:wrap}
header.site .brand{font-weight:700;font-size:17px;color:#fff}
header.site nav a{color:#cbd5e1;font-size:14px;margin-right:14px}
header.site nav a:hover{color:#fff}
.hero{background:#fff;border-bottom:1px solid #e5e7eb;padding:32px 0 26px}
.hero h1{margin:0 0 10px;font-size:26px}
.hero p.lead{margin:0 0 16px;color:#4b5563;font-size:15px}
.badges span{display:inline-block;background:#eef2ff;color:#3730a3;border-radius:999px;
 padding:3px 10px;font-size:12px;margin:0 6px 6px 0}
main{padding:26px 0 60px}
.card{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:22px 24px;margin-bottom:18px}
.card h2{margin:0 0 12px;font-size:19px;border-bottom:1px solid #eef0f3;padding-bottom:8px}
.card h3{margin:18px 0 6px;font-size:16px}
.card p{margin:8px 0}
.card ul,.card ol{margin:8px 0;padding-left:22px}
.card li{margin:5px 0}
pre{background:#0f172a;color:#e2e8f0;padding:12px 14px;border-radius:8px;overflow-x:auto;font-size:13px}
code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace}
.meta{color:#6b7280;font-size:13px;margin:0 0 14px}
.tagline{color:#374151;background:#f3f4f6;border-left:3px solid #9ca3af;
 padding:8px 12px;border-radius:0 6px 6px 0;margin:10px 0}
.toc{display:flex;flex-wrap:wrap;gap:8px;margin-top:4px}
.toc a{background:#f3f4f6;border-radius:6px;padding:5px 10px;font-size:13px}
.issue-list{list-style:none;padding:0;margin:0}
.issue-list li{padding:9px 0;border-bottom:1px dashed #e5e7eb;display:flex;gap:10px}
.issue-list li:last-child{border-bottom:none}
.issue-list .d{color:#6b7280;font-size:13px;min-width:92px}
footer.site{border-top:1px solid #e5e7eb;background:#fff;padding:18px 0;color:#6b7280;font-size:13px}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{border:1px solid #e5e7eb;padding:7px 10px;text-align:left}
th{background:#f9fafb}
"""


def esc(s: str) -> str:
    return html.escape(s or "")


def md_inline(s: str) -> str:
    s = esc(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\[(.+?)\]\((.+?)\)", r'<a href="\2">\1</a>', s)
    return s


def md_to_html(md: str, drop_title: bool = False) -> tuple[str, str]:
    """返回 (标题, HTML 正文)。"""
    lines = md.splitlines()
    title = ""
    out: list[str] = []
    in_list = False
    for raw in lines:
        line = raw.rstrip()
        if line.startswith("# ") and not title:
            title = line[2:].strip()
            if not drop_title:
                out.append(f"<h1>{md_inline(title)}</h1>")
            continue
        if line.startswith("## "):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<h2>{md_inline(line[3:])}</h2>")
        elif line.startswith("### "):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<h3>{md_inline(line[4:])}</h3>")
        elif line.startswith("> "):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f'<p class="meta">{md_inline(line[2:])}</p>')
        elif line.startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{md_inline(line[2:])}</li>")
        elif not line.strip():
            if in_list:
                out.append("</ul>")
                in_list = False
        else:
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<p>{md_inline(line)}</p>")
    if in_list:
        out.append("</ul>")
    return title, "\n".join(out)


def page(title: str, body: str, base: str, active: str = "") -> str:
    def nav(href: str, label: str, key: str) -> str:
        cls = ' style="color:#fff"' if key == active else ""
        return f'<a href="{href}"{cls}>{label}</a>'
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="AI 日报：每天自动聚合 5 个信息源，用大模型提炼要点与编者按，北京时间 07:00 发布。">
<style>{CSS}</style>
</head>
<body>
<header class="site"><div class="wrap">
  <span class="brand">🤖 AI 日报</span>
  <nav>{nav(base + '/', '首页', 'home')}{nav(base + '/archive.html', '全部归档', 'archive')}{nav(REPO_URL, '源码', '')}</nav>
</div></header>
{body}
<footer class="site"><div class="wrap">
  由 <a href="{REPO_URL}">siweimidu/daybrief</a> 自动生成 · 每天北京时间 07:00 更新 ·
  <a href="{base}/rss.xml">RSS</a> · <a href="{base}/archive.html">归档</a>
</div></footer>
</body>
</html>
"""


def read_issues(md_dir: Path) -> list[dict]:
    issues = []
    for path in sorted(glob.glob(str(md_dir / "*.md")), reverse=True):
        date = Path(path).stem
        md = Path(path).read_text(encoding="utf-8")
        title, body = md_to_html(md, drop_title=True)
        meta = ""
        m = re.search(r"^> (.+)$", md, re.M)
        if m:
            meta = m.group(1).strip()
        issues.append({"date": date, "title": title or date, "html": body, "meta": meta})
    return issues


def index_body(issues: list[dict], base: str) -> str:
    latest = issues[0] if issues else None
    recent = issues[:30]
    recent_lis = "\n".join(
        f'<li><span class="d">{i["date"]}</span><a href="{base}/{i["date"]}.html">{esc(i["title"])}</a></li>'
        for i in recent
    ) or "<li>暂无内容，等第一次运行后自动出现。</li>"

    latest_block = ""
    if latest:
        latest_block = f"""
<div class="card">
  <h2>最新一期 · {latest['date']}</h2>
  <p class="meta">{esc(latest['meta'])}</p>
  {latest['html']}
</div>"""

    return f"""
<section class="hero"><div class="wrap">
  <h1>AI 日报</h1>
  <p class="lead">每天自动抓取 5 个 AI 信息源，用大模型批量提炼要点、写出编者按与点评，
  北京时间 07:00 同时推送到 邮箱 · Telegram · 本站 · Markdown 归档。</p>
  <div class="badges">
    <span>全自动 · GitHub Actions</span><span>Groq + Gemini 双模型</span>
    <span>5 个信息源</span><span>RSS 可订阅</span><span>Markdown 可存档</span>
  </div>
</div></section>
<main class="wrap">

  <div class="card">
    <h2>怎么用（3 种方式）</h2>
    <table>
      <tr><th>方式</th><th>地址 / 操作</th><th>适合</th></tr>
      <tr><td>① 网页阅读</td><td>本页「最新一期」，或 <a href="{base}/archive.html">全部归档</a></td><td>随手看</td></tr>
      <tr><td>② RSS 订阅</td><td><code>{base}/rss.xml</code></td><td>阅读器 / n8n / 自动化</td></tr>
      <tr><td>③ 原始数据</td><td><code>{REPO_URL}/blob/main/daily-md/日期.md</code></td><td>二次加工</td></tr>
    </table>
    <h3>RSS 订阅示例</h3>
    <ul>
      <li>阅读器（Feedly / Inoreader /  Follow）：粘贴 <code>{base}/rss.xml</code></li>
      <li>n8n：<code>RSS Read</code> 节点 → URL 填 <code>{base}/rss.xml</code></li>
      <li>邮箱 / Telegram 推送：已由仓库 workflow 每天 07:00 自动发送</li>
    </ul>
  </div>

  <div class="card">
    <h2>它是怎么工作的</h2>
    <ol>
      <li><strong>抓取</strong>：拉取 5 个 RSS / JSON Feed（近 48 小时新内容）</li>
      <li><strong>去重</strong>：按链接去重，按发布时间倒序，取前 30 条</li>
      <li><strong>摘要</strong>：8 条一批交给大模型，输出标题 / 摘要 / 要点 / 分类</li>
      <li><strong>成稿</strong>：主编模型合成 4-6 段编者按、今日要点、分类统计</li>
      <li><strong>分发</strong>：写 Markdown+JSON → 邮件 → Telegram → 部署本站</li>
    </ol>
    <p class="meta">模型策略：Groq（主）→ Gemini（备），任一供应商日额度耗尽立即切换，绝不空等。</p>
    <h3>信息源</h3>
    <ul>
      <li>橘鸦AI早报 · AI Daily · DailyDawn 中文 · Simon Willison · Hacker News</li>
    </ul>
  </div>

  <div class="card">
    <h2>我要自己部署一份</h2>
    <ol>
      <li>Fork 或新建仓库，把本仓库的 <code>config.yaml</code> 拿过来</li>
      <li>改 <code>config.yaml</code> 的 <code>sources.rss</code> 换成你关注的信息源</li>
      <li>仓库 <code>Settings → Secrets and variables → Actions</code> 添加：
        <code>GROQ_API_KEY</code>（必填）、<code>GEMINI_API_KEY</code>（选填备用）、
        <code>SMTP_HOST/PORT/USERNAME/PASSWORD</code>、<code>MAIL_FROM_EMAIL</code>、
        <code>DAYBRIEF_RECIPIENTS</code>、<code>TELEGRAM_BOT_TOKEN</code>、<code>TELEGRAM_CHAT_ID</code></li>
      <li>Settings → Pages → Source 选 <code>GitHub Actions</code></li>
      <li>Actions → <em>AI Daily Brief</em> → Run workflow，跑一次即可</li>
    </ol>
    <p class="meta">定时任务写在 <code>.github/workflows/ai-daily-brief.yml</code>（cron 23:00 UTC = 北京 07:00），改这一行就能换时间。</p>
    <h3>本地跑</h3>
    <pre>pip install pyyaml
export GROQ_API_KEY=xxx GEMINI_API_KEY=yyy
python3 scripts/groq_brief.py --config config.yaml --lookback 48h   # 生成 output/ 与 daily-md/
python3 scripts/build_site.py                                        # 本地预览站点 site/</pre>
  </div>

  <div class="card">
    <h2>最近更新</h2>
    <ul class="issue-list">{recent_lis}</ul>
  </div>
{latest_block}
</main>
"""


def archive_body(issues: list[dict], base: str) -> str:
    groups: dict[str, list[dict]] = {}
    for i in issues:
        groups.setdefault(i["date"][:7], []).append(i)
    blocks = []
    for month in sorted(groups, reverse=True):
        lis = "\n".join(
            f'<li><span class="d">{i["date"]}</span><a href="{base}/{i["date"]}.html">{esc(i["title"])}</a></li>'
            for i in groups[month]
        )
        blocks.append(f'<div class="card"><h2>{month}（{len(groups[month])} 期）</h2>'
                      f'<ul class="issue-list">{lis}</ul></div>')
    if not blocks:
        blocks.append('<div class="card"><p>暂无归档。</p></div>')
    return f'<main class="wrap"><div class="card"><h2>全部归档</h2>' \
           f'<p class="meta">共 {len(issues)} 期 · 每期一个独立页面，可直接分享</p></div>' \
           + "".join(blocks) + "</main>"


def rss(issues: list[dict], base: str) -> str:
    items = []
    for i in issues[:30]:
        try:
            dt = datetime.strptime(i["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            pub = dt.strftime("%a, %d %b %Y 00:00:00 +0000")
        except ValueError:
            pub = ""
        desc = re.sub(r"<[^>]+>", "", i["html"]).strip()[:400]
        items.append(f"""  <item>
    <title>{esc(i['title'])}</title>
    <link>{base}/{i['date']}.html</link>
    <guid isPermaLink="true">{base}/{i['date']}.html</guid>
    <pubDate>{pub}</pubDate>
    <description>{esc(desc)}</description>
  </item>""")
    now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>AI 日报</title>
  <link>{base}/</link>
  <description>每天自动聚合 5 个 AI 信息源并生成的中文日报</description>
  <language>zh-cn</language>
  <lastBuildDate>{now}</lastBuildDate>
{chr(10).join(items)}
</channel></rss>
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--md", default="daily-md")
    ap.add_argument("--out", default="site")
    ap.add_argument("--url", default=DEFAULT_URL)
    args = ap.parse_args()

    base = args.url.rstrip("/")
    md_dir = Path(args.md)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    # 静态资源目录清理（保留其它文件）
    issues = read_issues(md_dir)
    print(f"读取 {len(issues)} 期日报")

    for i in issues:
        body = (f'<main class="wrap"><div class="card">'
                f'<h1>{md_inline(i["title"])}</h1>'
                f'<p class="meta">{md_inline(i["meta"])}</p>'
                f'{i["html"]}</div></main>')
        (out / f'{i["date"]}.html').write_text(
            page(f'{i["title"]} · AI 日报', body, base), encoding="utf-8")

    (out / "index.html").write_text(
        page("AI 日报 · 每天自动聚合 AI 资讯", index_body(issues, base), base, "home"), encoding="utf-8")
    (out / "archive.html").write_text(
        page("全部归档 · AI 日报", archive_body(issues, base), base, "archive"), encoding="utf-8")
    (out / "rss.xml").write_text(rss(issues, base), encoding="utf-8")
    print(f"生成完成：{out}/index.html, archive.html, rss.xml, {len(issues)} 个详情页")


if __name__ == "__main__":
    main()
