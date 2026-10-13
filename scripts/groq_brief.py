#!/usr/bin/env python3
"""AI 日报生成器 v2：RSS/JSONFeed 抓取 -> 批量 LLM 摘要 -> 合成 -> JSON+MD+HTML。

双供应商：Groq（OpenAI 兼容）与 Gemini 原生 REST，谁有额度用谁；
任一供应商报「tokens per day」直接拉黑本次运行，绝不空等。
摘要按 8 条一批调用，28 条资讯只需约 4 次调用，省 token 也省时间。
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

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

RUN_DEADLINE = float(os.environ.get("BRIEF_DEADLINE_S", "1500"))
_deadline = time.monotonic() + RUN_DEADLINE


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


# ============================================================ providers

class Provider:
    name = "base"

    def __init__(self) -> None:
        self.down = False

    def chat(self, prompt: str) -> str:
        raise NotImplementedError

    def mark_down(self, reason: str) -> None:
        log(f"  provider {self.name} 拉黑：{reason[:160]}")
        self.down = True


class GroqProvider(Provider):
    name = "groq"

    def __init__(self) -> None:
        super().__init__()
        self.key = os.environ.get("GROQ_API_KEY", "")
        self.model = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
        self.fallback = [m for m in os.environ.get(
            "GROQ_FALLBACK_MODELS", "openai/gpt-oss-20b").split(",") if m]
        self.url = "https://api.groq.com/openai/v1/chat/completions"
        if not self.key:
            self.down = True

    def chat(self, prompt: str) -> str:
        models = [self.model] + [m for m in self.fallback if m != self.model]
        last = None
        for model in models:
            if time.monotonic() > _deadline:
                break
            payload = {"model": model, "messages": [{"role": "user", "content": prompt}],
                       "temperature": 0.3, "response_format": {"type": "json_object"},
                       "max_tokens": 3500}
            try:
                out = self._post(payload, self.key)
                return json.loads(out)["choices"][0]["message"]["content"]
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "ignore")[:400]
                last = f"HTTP {e.code}: {body}"
                if e.code == 429 and "per day" in body:
                    self.mark_down(last)
                    return ""  # 交给下一个 provider
                if e.code == 429:
                    time.sleep(20)
                    continue
                if e.code in (403, 404, 413):
                    continue  # 换模型
                time.sleep(4)
            except Exception as e:
                last = str(e)
                time.sleep(4)
        raise RuntimeError(f"groq 失败: {last}")

    @staticmethod
    def _post(payload: dict, key: str) -> str:
        req = urllib.request.Request(
            "https://api.groq.com/openai/v1/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                     "Accept": "application/json", "User-Agent": BROWSER_UA})
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.read().decode("utf-8")


class GeminiProvider(Provider):
    name = "gemini"

    def __init__(self) -> None:
        super().__init__()
        self.key = os.environ.get("GEMINI_API_KEY", "")
        self.model = os.environ.get("GEMINI_MODEL", "gemini-flash-lite-latest")
        if not self.key:
            self.down = True

    def chat(self, prompt: str) -> str:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                   "generationConfig": {"responseMimeType": "application/json",
                                        "temperature": 0.3}}
        last = None
        for attempt in range(3):
            if time.monotonic() > _deadline:
                break
            try:
                req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={
                    "x-goog-api-key": self.key, "Content-Type": "application/json",
                    "User-Agent": BROWSER_UA})
                with urllib.request.urlopen(req, timeout=120) as resp:
                    j = json.loads(resp.read().decode("utf-8"))
                return j["candidates"][0]["content"]["parts"][0]["text"]
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "ignore")[:400]
                last = f"HTTP {e.code}: {body}"
                if e.code == 429:
                    if re.search(r"retry in \d+h", body) or "per day" in body:
                        self.mark_down(last)
                        return ""
                    time.sleep(min(20 * (attempt + 1), 60))
                    continue
                if e.code in (400, 404):
                    break
                time.sleep(8 * (attempt + 1))
            except Exception as e:
                last = str(e)
                time.sleep(5)
        raise RuntimeError(f"gemini 失败: {last}")


_providers: list[Provider] = []


def llm(prompt: str) -> str:
    global _providers
    if not _providers:
        _providers = [GroqProvider(), GeminiProvider()]
    errors: list[str] = []
    for _round in range(5):
        if all(p.down for p in _providers):
            break
        for pr in _providers:
            if pr.down or time.monotonic() > _deadline:
                continue
            try:
                out = pr.chat(prompt)
                if out:
                    return out
            except RuntimeError as e:
                errors.append(f"{pr.name}: {str(e)[:120]}")
        time.sleep(5)
    raise RuntimeError("没有可用模型供应商: " + " | ".join(errors[-3:]))


# ============================================================ feeds

def _strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", s).strip()


def _parse_jsonfeed(raw: bytes, since: datetime) -> list[dict]:
    data = json.loads(raw.decode("utf-8"))
    out = []
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
        link = it.get("url") or it.get("external_url") or ""
        title = _strip_html(it.get("title", ""))
        if not link or not title:
            continue
        desc = _strip_html(it.get("content_text") or it.get("summary")
                           or it.get("content_html") or "")[:600]
        out.append({"title": title, "url": link, "published": d, "desc": desc})
    return out


def parse_feed(url: str, since: datetime, timeout: int = 25) -> list[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    if raw.lstrip()[:1] in (b"{", b"["):
        return _parse_jsonfeed(raw, since)

    items: list[dict] = []
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

    entries = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
    for el in entries:
        d = date_of(el)
        if d is None or d <= since:
            continue
        link = link_of(el) or text_of(el, "guid", "id")
        title = _strip_html(text_of(el, "title"))
        if not link or not title:
            continue
        desc = _strip_html(text_of(el, "description", "summary", "content") or "")[:600]
        items.append({"title": title, "url": link, "published": d, "desc": desc})
    return items


# ============================================================ pipeline

def _unquote_newlines(s: str) -> str:
    return s.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")


def _extract_json(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    m = re.search(r"\{.*\}", text, re.S)
    return m.group(0) if m else text


def batch_summarize(chunk: list[dict]) -> list[dict]:
    lines = []
    for i, it in enumerate(chunk, 1):
        lines.append(f"[{i}] 标题：{it['title']}\n    链接：{it['url']}\n"
                     f"    来源：{it['source_name']} · {it['published'].strftime('%Y-%m-%d')}\n"
                     f"    原文摘要：{it.get('desc') or '（无正文，仅凭标题谨慎概括，不要编造）'}")
    prompt = f"""你是中文科技资讯编辑。对下面 {len(chunk)} 条资讯逐条输出摘要。

{chr(10).join(lines)}

只输出 JSON：{{"items": [{{"id": 1, "title": "精炼中文标题(≤30字)",
"summary": "1-2句中文，说明发生了什么、为什么重要",
"key_points": ["要点1", "要点2"], "category": "模型发布|开源项目|行业动态|技术解读|投融资|其他"}}]}}
items 数组顺序与输入一致，id 从 1 开始。只依据给出的原文摘要与标题，不得编造。"""
    try:
        raw = llm(prompt)
        data = json.loads(_extract_json(raw))
    except Exception as e:
        log(f"  批量失败({len(chunk)}条): {str(e)[:120]}")
        return []
    by_id: dict[int, dict] = {}
    for rec in data.get("items", []):
        try:
            by_id[int(rec.get("id", 0))] = rec
        except (TypeError, ValueError):
            continue
    out = []
    for i, it in enumerate(chunk, 1):
        rec = by_id.get(i)
        if not rec:
            continue
        out.append({
            "title": (rec.get("title") or it["title"])[:80],
            "summary": _unquote_newlines(rec.get("summary", ""))[:300],
            "key_points": [str(k)[:80] for k in (rec.get("key_points") or [])[:3]],
            "category": rec.get("category", "其他"),
            "source_name": it["source_name"],
            "source_url": it["url"],
            "published": it["published"].strftime("%Y-%m-%d"),
        })
    return out


def synthesize(summaries: list[dict]) -> dict:
    compact = [{"t": s["title"], "s": s["source_name"], "c": s["category"],
                "d": s["summary"][:90], "u": s["source_url"]} for s in summaries]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    n_hl = min(8, len(summaries))
    prompt = f"""你是资深中文科技媒体主编。今天是 {today}，下面是今日 {len(summaries)} 条资讯（JSON 数组）。

{json.dumps(compact, ensure_ascii=False)}

产出今日 AI 日报，只输出 JSON 对象：
- subject: 一行标题，点出今天最重要的事（≤25字）
- editorial: 4-6段中文编者按，段落之间用真正的换行符分隔（JSON 字符串内写 \\n）。第一段点出最值得关注趋势；中间段落串联多条资讯做判断；最后一段给可执行建议。观点鲜明，不要罗列。
- highlights: 最重要的 {n_hl} 条，每条 {{"title","source_name","source_url","analysis"}}，analysis 为 2-3 句中文点评其意义。同来源最多 2 条。
- categories: 分类计数对象，如 {{"模型发布":3,"开源项目":5}}"""
    raw = llm(prompt)
    data = json.loads(_extract_json(raw))
    hl = data.get("highlights", [])[:n_hl]
    for h in hl:
        h["analysis"] = _unquote_newlines(str(h.get("analysis", "")))
    return {
        "subject": data.get("subject", "AI 日报"),
        "editorial": _unquote_newlines(str(data.get("editorial", ""))),
        "highlights": hl,
        "categories": data.get("categories", {}),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_count": len(summaries),
    }


# ============================================================ render

def to_markdown(nl: dict, summaries: list[dict], date: str) -> str:
    L = [f"# {nl['subject']}", "", f"> 生成时间：{date} · 资讯 {nl['source_count']} 条", ""]
    if nl.get("categories"):
        L += ["**分类**：" + " · ".join(f"{k} {v}" for k, v in nl["categories"].items()), ""]
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
    hl_titles = {h.get("title") for h in nl["highlights"]}
    others = [s for s in summaries if s["title"] not in hl_titles]
    if others:
        L += ["## 其他资讯", ""]
        for s in others:
            L += [f"- **[{s['title']}]({s['source_url']})**（{s['source_name']} · "
                  f"{s['category']}）：{s['summary']}"]
        L += [""]
    return "\n".join(L)


def to_html(nl: dict, summaries: list[dict], date: str) -> str:
    import html as _html
    parts: list[str] = []
    for line in to_markdown(nl, summaries, date).splitlines():
        e = _html.escape(line)
        e = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", e)
        e = re.sub(r"\[(.+?)\]\((.+?)\)", r'<a href="\2" style="color:#2563eb">\1</a>', e)
        if line.startswith("### "):
            parts.append("<h3>" + e[4:] + "</h3>")
        elif line.startswith("## "):
            parts.append('<h2 style="border-bottom:1px solid #eee;padding-bottom:6px">'
                         + e[3:] + "</h2>")
        elif line.startswith("# "):
            parts.append("<h1>" + e[2:] + "</h1>")
        elif line.startswith("> "):
            parts.append('<p style="color:#888;font-size:13px">' + e[2:] + "</p>")
        elif line.startswith("- "):
            parts.append("<li>" + e[2:] + "</li>")
        elif line.strip():
            parts.append('<p style="line-height:1.75;margin:8px 0">' + e + "</p>")
    return ('<!doctype html><html lang="zh"><head><meta charset="utf-8"><meta name="viewport" '
            'content="width=device-width,initial-scale=1"><title>' + _html.escape(nl["subject"])
            + '</title></head><body style="max-width:680px;margin:24px auto;padding:0 16px;'
            "font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;color:#222;"
            'line-height:1.75">' + "".join(parts) + "</body></html>")


# ============================================================ main

def main() -> None:
    global _deadline
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--output", default="output")
    ap.add_argument("--lookback", default="48h")
    ap.add_argument("--max", default=os.environ.get("BRIEF_MAX_ITEMS", "30"))
    ap.add_argument("--batch", default=os.environ.get("BRIEF_BATCH", "8"))
    args = ap.parse_args()

    _deadline = time.monotonic() + RUN_DEADLINE
    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    hours = int(re.sub(r"\D", "", args.lookback) or 48)
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    log(f"抓取窗口：{since.isoformat()} 之后")

    raw_items: list[dict] = []
    for src in cfg.get("sources", {}).get("rss", []):
        url = src["url"]
        name = src.get("name", url)
        try:
            items = parse_feed(url, since)
        except Exception as e:
            log(f"  ✗ {name}: {e}")
            continue
        for it in items:
            it["source_name"] = name
        raw_items.extend(items)
        log(f"  ✓ {name}: {len(items)} 条")
    if not raw_items:
        log("没有新内容，退出")
        return

    seen, uniq = set(), []
    for it in sorted(raw_items, key=lambda x: x["published"], reverse=True):
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        uniq.append(it)
    limit = min(int(args.max), len(uniq))
    batch = max(1, int(args.batch))
    log(f"去重后 {len(uniq)} 条，处理 {limit} 条，每批 {batch} 条")

    summaries: list[dict] = []
    for start in range(0, limit, batch):
        chunk = uniq[start:start + batch]
        got = batch_summarize(chunk)
        summaries.extend(got)
        log(f"  批次 {start // batch + 1}: +{len(got)}（累计 {len(summaries)}）")
        if time.monotonic() > _deadline:
            log("  触及运行截止时间，用已得摘要继续")
            break
    if not summaries:
        log("摘要全部失败，退出")
        sys.exit(1)

    log(f"摘要完成 {len(summaries)} 条，开始合成")
    nl = synthesize(summaries)
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    Path("daily-md").mkdir(exist_ok=True)
    (out / f"{date}.json").write_text(json.dumps(nl, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / f"{date}.summaries.json").write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / f"{date}.html").write_text(to_html(nl, summaries, date), encoding="utf-8")
    (Path("daily-md") / f"{date}.md").write_text(to_markdown(nl, summaries, date), encoding="utf-8")
    log(f"完成 → {out}/{date}.json · daily-md/{date}.md · {out}/{date}.html")


if __name__ == "__main__":
    main()
