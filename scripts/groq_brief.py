#!/usr/bin/env python3
"""AI 日报生成器：RSS 抓取 -> Groq 摘要 -> Groq 合成 -> JSON + Markdown。

替代 daybrief（只支持 Gemini）。Groq 免费额度大、速度快。
用法：python3 scripts/groq_brief.py [--config config.yaml] [--output output]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import yaml

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
FALLBACK_MODELS = ["openai/gpt-oss-20b"]
MAX_HIGHLIGHTS = 8


MIN_INTERVAL = float(os.environ.get("BRIEF_MIN_INTERVAL_S", "6"))
_last_call = [0.0]


def _throttle() -> None:
    wait = MIN_INTERVAL - (time.monotonic() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.monotonic()


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------- RSS

def _strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _parse_jsonfeed(raw: bytes, since: datetime) -> list[dict]:
    data = json.loads(raw.decode("utf-8"))
    out: list[dict] = []
    for it in data.get("items", []):
        v = it.get("date_published") or it.get("date_modified")
        if not v:
            continue
        try:
            d = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except Exception:
            continue
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        if d <= since:
            continue
        link = it.get("url") or it.get("external_url") or it.get("_id") or ""
        title = _strip_html(it.get("title", ""))
        if not link or not title:
            continue
        out.append({"title": title, "url": link, "published": d})
    return out


def parse_rss(url: str, since: datetime, timeout: int = 25) -> list[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; AI-Daily/1.0)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    head = raw.lstrip()[:1]
    if head in (b"{", b"["):  # JSON Feed
        return _parse_jsonfeed(raw, since)
    items: list[dict] = []
    # RSS 2.0 / Atom 通用解析
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return items

    def local(tag: str) -> str:
        return tag.split("}")[-1]

    def text_of(el, *names):
        for n in names:
            for child in el:
                if local(child.tag).lower() == n:
                    return (child.text or "").strip()
        return ""

    def link_of(el):
        for child in el:
            if local(child.tag).lower() == "link":
                href = child.attrib.get("href")
                if href:
                    return href.strip()
                if child.text and child.text.strip():
                    return child.text.strip()
        return ""

    def date_of(el):
        for n in ("pubdate", "published", "updated", "date"):
            v = text_of(el, n)
            if v:
                try:
                    d = parsedate_to_datetime(v)
                except Exception:
                    try:
                        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
                    except Exception:
                        continue
                if d.tzinfo is None:
                    d = d.replace(tzinfo=timezone.utc)
                return d
        return None

    channels = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
    for el in channels:
        d = date_of(el)
        if d is None or d <= since:
            continue
        link = link_of(el) or text_of(el, "guid", "id")
        title = _strip_html(text_of(el, "title"))
        if not link or not title:
            continue
        items.append({"title": title, "url": link, "published": d})
    return items


# ---------------------------------------------------------------- Groq

def groq_chat(messages: list[dict], json_mode: bool = True, temperature: float = 0.3) -> str:
    key = os.environ.get("GROQ_API_KEY", "")
    if not key:
        raise SystemExit("GROQ_API_KEY 未设置")
    models = [MODEL] + [m for m in FALLBACK_MODELS if m != MODEL]
    last_err = None
    budget = int(os.environ.get("BRIEF_TIME_BUDGET_S", "900"))
    t0 = time.monotonic()
    for model in models:
        payload = {"model": model, "messages": messages, "temperature": temperature}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        body_bytes = json.dumps(payload).encode()
        hdrs = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            # Cloudflare 会因默认 Python-urllib UA 返回 403/1010
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
            "Accept": "application/json",
        }
        for attempt in range(1, 3):
            _throttle()
            if time.monotonic() - t0 > budget:
                raise RuntimeError(f"超出时间预算 {budget}s: {last_err}")
            req = urllib.request.Request(GROQ_URL, data=body_bytes, headers=hdrs)
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    j = json.loads(resp.read().decode())
                return j["choices"][0]["message"]["content"]
            except urllib.error.HTTPError as e:
                body = e.read().decode()[:200]
                last_err = f"{model} HTTP {e.code}: {body}"
                if e.code == 429:
                    log(f"  429 on {model}, backoff {MIN_INTERVAL * 3}s")
                    time.sleep(MIN_INTERVAL * 3)
                    continue
                if e.code in (403, 404, 413):
                    break  # 权限/模型问题：换模型
                time.sleep(3 * attempt)
            except Exception as e:
                last_err = f"{model} {e}"
                time.sleep(3 * attempt)
        log(f"  warn: {last_err} -> next model")
    raise RuntimeError(f"所有模型均失败: {last_err}")
    raise RuntimeError(f"所有模型均失败: {last_err}")


def summarize_item(item: dict, source_name: str, language: str) -> dict | None:
    prompt = f"""你是内容分析编辑。阅读下面这篇资讯并输出结构化摘要。

标题：{item['title']}
链接：{item['url']}
来源：{source_name}
发布时间：{item['published'].strftime('%Y-%m-%d')}

只输出 JSON 对象，字段：
- title: 精炼中文标题（不超过 30 字）
- summary: 2-3 句中文摘要，说清发生了什么、为什么重要
- key_points: 3-5 条中文要点（字符串数组）
- category: 从 [模型发布, 开源项目, 行业动态, 技术解读, 投融资, 其他] 中选一个

不要编造链接里没有的信息。若链接内容无法访问，就基于标题谨慎概括。"""
    try:
        raw = groq_chat([{"role": "user", "content": prompt}])
        data = json.loads(_extract_json(raw))
    except Exception as e:
        log(f"  skip {item['title'][:30]} :: {e}")
        return None
    return {
        "title": data.get("title") or item["title"],
        "summary": _unquote_newlines(data.get("summary", "")),
        "key_points": data.get("key_points", [])[:5],
        "category": data.get("category", "其他"),
        "source_name": source_name,
        "source_url": item["url"],
        "published": item["published"].strftime("%Y-%m-%d"),
    }


def synthesize(summaries: list[dict], language: str) -> dict:
    compact = [
        {"title": s["title"], "source": s["source_name"], "category": s["category"],
         "summary": s["summary"][:90], "key_points": [k[:60] for k in s["key_points"][:3]], "url": s["source_url"]}
        for s in summaries
    ]
    prompt = f"""你是资深中文科技媒体主编。下面是今天的 {len(summaries)} 条资讯摘要（JSON）。

{json.dumps(compact, ensure_ascii=False, indent=1)}

请产出今日 AI 日报，只输出 JSON 对象：
- subject: 今日日报标题（一行，点出今天最重要的一件事，不超过 25 字）
- editorial: 4-6 段中文编者按。第 1 段点出今天最值得注意的趋势；中间段落串联多条资讯做判断（它们之间有什么关联、对从业者意味着什么）；最后一段给出可执行建议。观点要鲜明，不要罗列。
- highlights: 挑 {min(MAX_HIGHLIGHTS, len(summaries))} 条最重要的，每条 {{title, source_name, source_url, analysis}}，analysis 是 2-3 句中文点评（讲意义，不要复述摘要）。
- categories: 按 category 分组统计，如 {{"模型发布": 3, "开源项目": 5}}

排序规则：重要性优先，同来源不超过 2 条。"""
    raw = groq_chat([{"role": "user", "content": prompt}])
    data = json.loads(_extract_json(raw))
    hl = data.get("highlights", [])[:MAX_HIGHLIGHTS]
    for h in hl:
        h["analysis"] = _unquote_newlines(h.get("analysis", ""))
    editorial = _unquote_newlines(data.get("editorial", ""))
    return {
        "subject": data.get("subject", "AI 日报"),
        "editorial": editorial,
        "highlights": hl,
        "categories": data.get("categories", {}),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": MODEL,
        "source_count": len(summaries),
    }


def _unquote_newlines(s: str) -> str:
    """模型有时把换行写成字面 \\n，统一还原成真换行。"""
    return s.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")


def _extract_json(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    m = re.search(r"\{.*\}", text, re.S)
    return m.group(0) if m else text


# ---------------------------------------------------------------- render

def to_markdown(nl: dict, summaries: list[dict], date: str) -> str:
    L: list[str] = [f"# {nl['subject']}", "", f"> 生成时间：{date} · 模型：{nl['model']} · 资讯 {nl['source_count']} 条", ""]
    if nl.get("categories"):
        cats = " · ".join(f"{k} {v}" for k, v in nl["categories"].items())
        L += [f"**分类**：{cats}", ""]
    L += ["## 编者按", ""]
    for p in re.split(r"\n\s*\n", nl.get("editorial", "")):
        p = p.strip()
        if p:
            L += [p, ""]
    L += ["## 今日要点", ""]
    for i, h in enumerate(nl["highlights"], 1):
        L += [f"### {i}. {h.get('title', '')}", "",
              f"- 来源：[{h.get('source_name', '')}]({h.get('source_url', '')})"]
        if h.get("analysis"):
            L += [f"- 点评：{h['analysis']}"]
        L += [""]
    others = [s for s in summaries if all(s["title"] != h.get("title") for h in nl["highlights"])]
    if others:
        L += ["## 其他资讯", ""]
        for s in others:
            kp = "；".join(s["key_points"][:3])
            L += [f"- **[{s['title']}]({s['source_url']})**（{s['source_name']} · {s['category']}）：{s['summary']}" + (f"  {kp}" if kp else "")]
        L += [""]
    return "\n".join(L)


def to_html(nl: dict, summaries: list[dict], date: str) -> str:
    md = to_markdown(nl, summaries, date)
    body = []
    for line in md.splitlines():
        if line.startswith("### "):
            body.append(f'<h3 style="margin:18px 0 6px;font-size:17px;color:#111">{line[4:]}</h3>')
        elif line.startswith("## "):
            body.append(f'<h2 style="margin:24px 0 8px;font-size:19px;color:#111;border-bottom:1px solid #eee;padding-bottom:6px">{line[3:]}</h2>')
        elif line.startswith("# "):
            body.append(f'<h1 style="font-size:22px;margin:0 0 8px">{line[2:]}</h1>')
        elif line.startswith("> "):
            body.append(f'<p style="color:#888;font-size:13px;margin:0 0 16px">{line[2:]}</p>')
        elif line.startswith("- "):
            body.append(f'<li style="margin:4px 0;line-height:1.6">{_inline(line[2:])}</li>')
        elif line.strip():
            body.append(f'<p style="line-height:1.75;margin:8px 0;color:#333">{_inline(line)}</p>')
    lis = "".join(body).replace("<li", "<ul style='padding-left:20px'><li").replace("</li>", "</li></ul>")
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{nl['subject']}</title></head>
<body style="max-width:680px;margin:24px auto;padding:0 16px;font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif">
{lis}
</body></html>"""


def _inline(s: str) -> str:
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"\[(.+?)\]\((.+?)\)", r'<a href="\2" style="color:#2563eb">\1</a>', s)
    return s


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--output", default="output")
    ap.add_argument("--lookback", default="48h")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    hours = int(re.sub(r"\D", "", args.lookback) or 48)
    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    log(f"抓取窗口：{since.isoformat()} 之后")
    raw_items: list[tuple[dict, str]] = []
    for src in cfg.get("sources", {}).get("rss", []):
        url = src["url"]
        name = src.get("name", url)
        try:
            items = parse_rss(url, since)
        except Exception as e:
            log(f"  ✗ {name}: {e}")
            continue
        for it in items:
            raw_items.append((it, name))
        log(f"  ✓ {name}: {len(items)} 条")
    if not raw_items:
        log("没有新内容，退出")
        Path(args.output).mkdir(parents=True, exist_ok=True)
        (Path(args.output) / "empty.flag").write_text(datetime.now(timezone.utc).isoformat())
        return

    # 去重（同 URL 只保留一次），按时间倒序，限量控制成本
    seen: set[str] = set()
    uniq: list[tuple[dict, str]] = []
    for it, name in sorted(raw_items, key=lambda x: x[0]["published"], reverse=True):
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        uniq.append((it, name))
    limit = int(os.environ.get("BRIEF_MAX_ITEMS", "30"))
    log(f"去重后 {len(uniq)} 条，本次处理 {min(limit, len(uniq))} 条")

    summaries: list[dict] = []
    for it, name in uniq[:limit]:
        s = summarize_item(it, name, "zh")
        if s:
            summaries.append(s)
    if not summaries:
        log("摘要全部失败，退出")
        sys.exit(1)
    log(f"摘要完成 {len(summaries)} 条，开始合成日报")

    nl = synthesize(summaries, "zh")
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{date}.json").write_text(json.dumps(nl, ensure_ascii=False, indent=2), encoding="utf-8")
    md_dir = Path("daily-md")
    md_dir.mkdir(exist_ok=True)
    (md_dir / f"{date}.md").write_text(to_markdown(nl, summaries, date), encoding="utf-8")
    (out / f"{date}.html").write_text(to_html(nl, summaries, date), encoding="utf-8")
    (out / f"{date}.summaries.json").write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"完成 → {out}/{date}.json · daily-md/{date}.md")


if __name__ == "__main__":
    main()
