#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tavily-search.py —— 联网搜索 / 正文抓取 / 调研报告 CLI

子命令:
  search    关键词搜索
  fetch     抓 URL 正文(可并发 + 去重)
  report    多查询拼成调研报告,可一键保存到笔记区
  cache     缓存管理(list / clear / dir)

用法:
  python tavily-search.py search "Godot 4.7"
  python tavily-search.py search "q1" "q2" "q3" --concurrent --cache
  python tavily-search.py fetch URL1 URL2 --concurrent --unique
  python tavily-search.py report "Godot 4.7" "Minimax M3" --save
  python tavily-search.py cache list

环境变量:
  TAVILY_API_KEY(必填)
  CLAUDE_WORK_ROOT(可选,默认从脚本路径反推;用于定位缓存/笔记目录)

依赖: 仅 Python 标准库
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

TAVILY_SEARCH = "https://api.tavily.com/search"
TAVILY_EXTRACT = "https://api.tavily.com/extract"
TAVILY_CRAWL = "https://api.tavily.com/crawl"
TIMEOUT_SEC = 30
TIMEOUT_CRAWL_SEC = 120  # crawl 抓整站,默认给 2 分钟
DEFAULT_CACHE_TTL_HOURS = 24


# ---------------------------------------------------------------------------
# 路径 / 工作目录定位
# ---------------------------------------------------------------------------

def _work_root() -> Path:
    """定位 D:\\claude work\\ 根目录。

    优先级:环境变量 CLAUDE_WORK_ROOT > 脚本父目录(工具区/脚本/)的祖辈。
    """
    env = os.environ.get("CLAUDE_WORK_ROOT", "").strip()
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    # 工具区/脚本/foo.py -> 工具区/ -> 工作根
    for parent in here.parents:
        if parent.name == "工具区":
            return parent.parent
    # 兜底:当前目录
    return Path.cwd()


def cache_dir() -> Path:
    return _work_root() / "共享区" / "缓存" / "tavily"


def notes_dir() -> Path:
    return _work_root() / "笔记区" / "知识"


# ---------------------------------------------------------------------------
# HTTP 层
# ---------------------------------------------------------------------------

def _api_key() -> str:
    key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not key:
        sys.exit("❌ 缺少环境变量 TAVILY_API_KEY")
    return key


def _request(url: str, payload: dict[str, Any], timeout: int = TIMEOUT_SEC,
             use_bearer: bool = False) -> dict[str, Any]:
    """统一 HTTP 入口。

    use_bearer=True: 走 Authorization: Bearer 头(/crawl、/research 端点要求)
    use_bearer=False: 把 api_key 塞 body(/search、/extract 端点)
    """
    headers = {"Content-Type": "application/json"}
    if use_bearer:
        headers["Authorization"] = f"Bearer {_api_key()}"
        body = json.dumps(payload).encode("utf-8")
    else:
        body = json.dumps({"api_key": _api_key(), **payload}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8")).get("detail", str(e))
        except Exception:
            detail = str(e)
        sys.exit(f"❌ Tavily HTTP {e.code}: {detail}")
    except urllib.error.URLError as e:
        sys.exit(f"❌ 网络错误: {e.reason}")


# ---------------------------------------------------------------------------
# search / extract
# ---------------------------------------------------------------------------

def search(
    query: str,
    max_results: int = 5,
    depth: str = "basic",
    include_raw: bool = False,
) -> dict[str, Any]:
    payload = {
        "query": query,
        "max_results": max_results,
        "search_depth": depth,
        "include_answer": True,
        "include_raw_content": include_raw,
    }
    return _request(TAVILY_SEARCH, payload)


def fetch(urls: list[str], query: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"urls": urls}
    if query:
        payload["query"] = query
    return _request(TAVILY_EXTRACT, payload)


def crawl(
    url: str,
    max_depth: int = 2,
    limit: int = 10,
    query: str | None = None,
) -> dict[str, Any]:
    """整站爬取。/crawl 是 Bearer 认证,同步返回(但内容大,timeout 给 120s)。"""
    payload: dict[str, Any] = {
        "url": url,
        "max_depth": max_depth,
        "limit": limit,
    }
    if query:
        payload["query"] = query
    return _request(TAVILY_CRAWL, payload, timeout=TIMEOUT_CRAWL_SEC, use_bearer=True)


# ---------------------------------------------------------------------------
# 缓存层
# ---------------------------------------------------------------------------

def _cache_key(namespace: str, params: dict[str, Any]) -> str:
    """基于 namespace + 规范化参数生成 SHA1 文件名。"""
    norm = json.dumps(params, sort_keys=True, ensure_ascii=False)
    h = hashlib.sha1(f"{namespace}|{norm}".encode("utf-8")).hexdigest()[:16]
    return h


def cache_read(namespace: str, params: dict[str, Any], ttl_hours: int) -> dict[str, Any] | None:
    """命中且未过期返回数据,否则 None。"""
    key = _cache_key(namespace, params)
    path = cache_dir() / namespace / f"{key}.json"
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            record = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    ts = record.get("_cached_at")
    if not ts:
        return None
    age = datetime.now() - datetime.fromisoformat(ts)
    if age > timedelta(hours=ttl_hours):
        return None
    data = dict(record.get("data") or {})
    data["_cache_hit"] = True
    data["_cache_age_hours"] = round(age.total_seconds() / 3600, 2)
    return data


def cache_write(namespace: str, params: dict[str, Any], data: dict[str, Any]) -> None:
    key = _cache_key(namespace, params)
    dir_path = cache_dir() / namespace
    dir_path.mkdir(parents=True, exist_ok=True)
    record = {
        "_cached_at": datetime.now().isoformat(timespec="seconds"),
        "params": params,
        "data": data,
    }
    path = dir_path / f"{key}.json"
    # 原子写:先 .tmp 再 rename,避免中断留下半截文件
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def cache_clear(namespace: str | None = None) -> int:
    """返回删除的条目数。namespace=None 清空所有。"""
    base = cache_dir()
    if not base.exists():
        return 0
    targets = [base / namespace] if namespace else [p for p in base.iterdir() if p.is_dir()]
    removed = 0
    for d in targets:
        if not d.exists():
            continue
        for p in d.glob("*.json"):
            p.unlink()
            removed += 1
        # 空 namespace 目录也清掉
        if not any(d.iterdir()):
            d.rmdir()
    return removed


def cache_list() -> list[dict[str, Any]]:
    base = cache_dir()
    if not base.exists():
        return []
    out: list[dict[str, Any]] = []
    for ns_dir in sorted(base.iterdir()):
        if not ns_dir.is_dir():
            continue
        for p in sorted(ns_dir.glob("*.json")):
            try:
                with p.open("r", encoding="utf-8") as f:
                    rec = json.load(f)
                params = rec.get("params") or {}
                ts = rec.get("_cached_at", "")
                # search 的 query / fetch 的 urls 抽出来当摘要
                summary = params.get("query") or params.get("urls") or ""
                if isinstance(summary, list):
                    summary = ", ".join(summary)[:60]
                out.append({
                    "namespace": ns_dir.name,
                    "key": p.stem,
                    "cached_at": ts,
                    "summary": str(summary)[:80],
                    "size_kb": round(p.stat().st_size / 1024, 1),
                })
            except (json.JSONDecodeError, OSError):
                continue
    return out


def cache_status_line(namespace: str, params: dict[str, Any], hit: bool, age: float | None) -> str:
    if hit and age is not None:
        return f"💾 缓存命中(剩余 {DEFAULT_CACHE_TTL_HOURS - age:.1f}h 过期)"
    return "🌐 远程调用"


# ---------------------------------------------------------------------------
# 渲染:search
# ---------------------------------------------------------------------------

def _truncate(text: str, limit: int = 300) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def render_search_plain(data: dict[str, Any], query: str | None = None) -> str:
    lines: list[str] = []
    if query:
        lines.append(f"🔍 关键词:{query}")
        lines.append("─" * 40)
    answer = data.get("answer")
    if answer:
        lines.append("💡 AI 摘要")
        lines.append(answer.strip())
        lines.append("")
    results = data.get("results") or []
    if not results:
        lines.append("(无搜索结果)")
        return "\n".join(lines)
    lines.append(f"📚 搜索结果({len(results)} 条)")
    lines.append("─" * 40)
    for i, item in enumerate(results, 1):
        lines.append(f"[{i}] {item.get('title', '(无标题)')}")
        url = item.get("url", "")
        if url:
            lines.append(f"    🔗 {url}")
        content = item.get("content")
        if content:
            lines.append(f"    📝 {_truncate(content)}")
        score = item.get("score")
        if score is not None:
            lines.append(f"    ⭐ 相关度:{score:.2f}")
        lines.append("")
    if data.get("_cache_hit"):
        lines.append(cache_status_line("search", {}, True, data.get("_cache_age_hours")))
    return "\n".join(lines).rstrip()


def render_search_md(data: dict[str, Any], query: str | None = None) -> str:
    lines: list[str] = []
    if query:
        lines.append(f"## 🔍 {query}")
        lines.append("")
    answer = data.get("answer")
    if answer:
        lines.append("### 💡 AI 摘要")
        lines.append("> " + answer.strip().replace("\n", "\n> "))
        lines.append("")
    results = data.get("results") or []
    if not results:
        lines.append("_(无搜索结果)_")
        return "\n".join(lines)
    lines.append(f"### 📚 搜索结果({len(results)} 条)")
    lines.append("")
    for i, item in enumerate(results, 1):
        title = item.get("title", "(无标题)")
        url = item.get("url", "")
        score = item.get("score")
        suffix = f"  _(相关度 {score:.2f})_" if score is not None else ""
        if url:
            lines.append(f"{i}. [{title}]({url}){suffix}")
        else:
            lines.append(f"{i}. {title}{suffix}")
        content = item.get("content")
        if content:
            lines.append(f"   - {_truncate(content, 250)}")
    lines.append("")
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# 渲染:fetch
# ---------------------------------------------------------------------------

def render_fetch_plain(data: dict[str, Any]) -> str:
    lines: list[str] = []
    results = data.get("results") or []
    failed = data.get("failed_results") or []
    if not results:
        lines.append("(无抓取结果)")
    for i, item in enumerate(results, 1):
        url = item.get("url", "")
        title = item.get("title") or url
        lines.append(f"[{i}] {title}")
        if url and url != title:
            lines.append(f"    🔗 {url}")
        content = item.get("raw_content") or item.get("content") or ""
        if content:
            lines.append("")
            lines.append(content.strip())
        lines.append("")
    if failed:
        lines.append("⚠️ 失败 URL:")
        for f in failed:
            lines.append(f"  - {f}")
    return "\n".join(lines).rstrip()


def render_fetch_md(data: dict[str, Any]) -> str:
    lines: list[str] = ["## 📄 抓取正文", ""]
    results = data.get("results") or []
    failed = data.get("failed_results") or []
    if not results:
        lines.append("_(无抓取结果)_")
    for i, item in enumerate(results, 1):
        url = item.get("url", "")
        title = item.get("title") or url
        lines.append(f"### {i}. [{title}]({url})")
        content = item.get("raw_content") or item.get("content") or ""
        if content:
            lines.append("")
            lines.append(content.strip())
        lines.append("")
    if failed:
        lines.append("### ⚠️ 失败 URL")
        for f in failed:
            lines.append(f"- {f}")
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# 渲染:crawl
# ---------------------------------------------------------------------------

def render_crawl_plain(data: dict[str, Any], base_url: str | None = None) -> str:
    lines: list[str] = []
    if base_url:
        lines.append(f"🕷️ 起始 URL:{base_url}")
        lines.append("─" * 40)
    results = data.get("results") or []
    lines.append(f"📑 抓取页面:{len(results)} 个")
    lines.append("─" * 40)
    for i, item in enumerate(results, 1):
        url = item.get("url", "(无 URL)")
        content = item.get("raw_content") or ""
        snippet = _truncate(content, 200)
        lines.append(f"[{i}] {url}")
        if snippet:
            lines.append(f"    📝 {snippet}")
        lines.append("")
    return "\n".join(lines).rstrip()


def render_crawl_md(data: dict[str, Any], base_url: str | None = None) -> str:
    lines: list[str] = []
    if base_url:
        lines.append(f"# 🕷️ {base_url}")
        lines.append("")
    results = data.get("results") or []
    lines.append(f"## 📑 抓取页面({len(results)} 个)")
    lines.append("")
    for i, item in enumerate(results, 1):
        url = item.get("url", "(无 URL)")
        content = item.get("raw_content") or ""
        lines.append(f"### {i}. [{url}]({url})")
        if content:
            # MD 模式给完整正文,不截断
            lines.append("")
            lines.append(content.strip())
        lines.append("")
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# 渲染:report
# ---------------------------------------------------------------------------

def _slugify(text: str) -> str:
    """把 'Godot 4.7 release notes' 变成 'godot-4-7-release-notes'。"""
    s = re.sub(r"[^\w\s-]", "", text.lower(), flags=re.UNICODE)
    s = re.sub(r"[\s_]+", "-", s).strip("-")
    return s or "report"


def render_report(results: list[dict[str, Any]], queries: list[str]) -> str:
    """调研报告:H1 标题 + 每个查询一节(H2 + 摘要 + 来源) + 元信息尾巴。"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines: list[str] = []
    title = " · ".join(queries) if queries else "调研报告"
    lines.append(f"# 📑 {title}")
    lines.append("")
    lines.append(f"> 生成时间:{ts}  ·  查询数:{len(results)}  ·  来源:Tavily")
    lines.append("")
    lines.append("---")
    lines.append("")

    cache_hits = sum(1 for r in results if r.get("_cache_hit"))
    if cache_hits:
        lines.append(f"💾 缓存命中:{cache_hits}/{len(results)}")
        lines.append("")

    for r in results:
        q = r.get("query", "(无查询词)")
        lines.append(f"## {q}")
        lines.append("")
        answer = r.get("answer")
        if answer:
            lines.append("### 要点")
            lines.append("")
            lines.append("> " + answer.strip().replace("\n", "\n> "))
            lines.append("")
        results_list = r.get("results") or []
        if not results_list:
            lines.append("_(无来源)_")
            lines.append("")
            continue
        lines.append(f"### 来源({len(results_list)} 条)")
        lines.append("")
        for i, item in enumerate(results_list, 1):
            title_i = item.get("title", "(无标题)")
            url_i = item.get("url", "")
            score = item.get("score")
            suffix = f"  _(相关度 {score:.2f})_" if score is not None else ""
            if url_i:
                lines.append(f"{i}. [{title_i}]({url_i}){suffix}")
            else:
                lines.append(f"{i}. {title_i}{suffix}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def report_save(content: str, queries: list[str]) -> Path:
    notes = notes_dir()
    notes.mkdir(parents=True, exist_ok=True)
    date = datetime.now().strftime("%Y-%m-%d")
    # 文件名:<date>_<slug>.md,slug 用主查询词
    primary = queries[0] if queries else "report"
    slug = _slugify(primary)
    path = notes / f"{date}_{slug}.md"
    # 同名避免覆盖,加序号
    if path.exists():
        n = 2
        while True:
            candidate = notes / f"{date}_{slug}_{n}.md"
            if not candidate.exists():
                path = candidate
                break
            n += 1
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 并发 / 缓存编排
# ---------------------------------------------------------------------------

def search_many(
    queries: list[str],
    max_results: int,
    depth: str,
    include_raw: bool,
    concurrent: bool,
    use_cache: bool,
    ttl_hours: int,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not concurrent:
        for q in queries:
            params = {
                "query": q, "max_results": max_results,
                "depth": depth, "include_raw": include_raw,
            }
            if use_cache:
                hit = cache_read("search", params, ttl_hours)
                if hit is not None:
                    out.append({"query": q, **hit})
                    continue
            data = search(q, max_results, depth, include_raw)
            if use_cache:
                cache_write("search", params, data)
            out.append({"query": q, **data})
        return out

    # 并发
    results: list[dict[str, Any] | None] = [None] * len(queries)

    def worker(idx: int, q: str) -> dict[str, Any]:
        params = {
            "query": q, "max_results": max_results,
            "depth": depth, "include_raw": include_raw,
        }
        if use_cache:
            hit = cache_read("search", params, ttl_hours)
            if hit is not None:
                return {"query": q, **hit}
        data = search(q, max_results, depth, include_raw)
        if use_cache:
            cache_write("search", params, data)
        return {"query": q, **data}

    with ThreadPoolExecutor(max_workers=min(8, len(queries))) as pool:
        futures = {pool.submit(worker, i, q): i for i, q in enumerate(queries)}
        for fut in as_completed(futures):
            idx = futures[fut]
            try:
                results[idx] = fut.result()
            except SystemExit as e:
                results[idx] = {"query": queries[idx], "error": str(e)}
    return [r for r in results if r is not None]


def fetch_many(
    urls: list[str],
    query: str | None,
    concurrent: bool,
    unique: bool,
    use_cache: bool,
    ttl_hours: int,
) -> dict[str, Any]:
    """fetch:支持并发(分批 + 合并)+ URL 去重 + 缓存。"""
    if unique:
        seen: set[str] = set()
        deduped: list[str] = []
        for u in urls:
            if u not in seen:
                seen.add(u)
                deduped.append(u)
        if len(deduped) != len(urls):
            print(f"♻️  去重:{len(urls)} → {len(deduped)}", file=sys.stderr)
        urls = deduped

    params = {"urls": urls, "query": query or ""}
    if use_cache:
        hit = cache_read("fetch", params, ttl_hours)
        if hit is not None:
            return hit

    if not concurrent or len(urls) == 1:
        data = fetch(urls, query=query)
    else:
        # Tavily /extract 本身支持 batch,但 URL 多时拆批更稳
        BATCH = 5
        merged: dict[str, Any] = {"results": [], "failed_results": []}
        batches = [urls[i:i + BATCH] for i in range(0, len(urls), BATCH)]

        def call(batch: list[str]) -> dict[str, Any]:
            return fetch(batch, query=query)

        with ThreadPoolExecutor(max_workers=min(4, len(batches))) as pool:
            for fut in as_completed(pool.submit(call, b) for b in batches):
                part = fut.result()
                merged["results"].extend(part.get("results") or [])
                merged["failed_results"].extend(part.get("failed_results") or [])
        data = merged

    if use_cache:
        cache_write("fetch", params, data)
    return data


# ---------------------------------------------------------------------------
# CLI 工具
# ---------------------------------------------------------------------------

def _split_queries(values: list[str]) -> list[str]:
    out: list[str] = []
    for v in values:
        if "," in v:
            out.extend(s.strip() for s in v.split(",") if s.strip())
        else:
            out.append(v)
    return out


def _sep(total: int) -> str:
    if total > 1:
        return "\n" + "═" * 40 + "\n"
    return ""


# ---------------------------------------------------------------------------
# 子命令入口
# ---------------------------------------------------------------------------

def cmd_search(args: argparse.Namespace) -> None:
    queries = _split_queries(args.queries)
    results = search_many(
        queries,
        max_results=args.max,
        depth=args.depth,
        include_raw=args.raw,
        concurrent=args.concurrent,
        use_cache=args.cache,
        ttl_hours=args.ttl,
    )
    if args.json:
        json.dump(results, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return
    renderer = render_search_md if args.md else render_search_plain
    for i, r in enumerate(results):
        print(renderer(r, query=r.get("query")))
        if i < len(results) - 1:
            print(_sep(len(results)))


def cmd_fetch(args: argparse.Namespace) -> None:
    data = fetch_many(
        urls=args.urls,
        query=args.query,
        concurrent=args.concurrent,
        unique=args.unique,
        use_cache=args.cache,
        ttl_hours=args.ttl,
    )
    if args.json:
        json.dump(data, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return
    renderer = render_fetch_md if args.md else render_fetch_plain
    print(renderer(data))


def cmd_crawl(args: argparse.Namespace) -> None:
    """整站爬取。/crawl 是 Bearer 认证且返回大,自带缓存防重复烧 key。"""
    params = {
        "url": args.url,
        "max_depth": args.depth,
        "limit": args.limit,
        "query": args.query or "",
    }
    if args.cache:
        hit = cache_read("crawl", params, args.ttl)
        if hit is not None:
            data = hit
            print("💾 crawl 缓存命中", file=sys.stderr)
        else:
            data = crawl(args.url, args.depth, args.limit, args.query)
            cache_write("crawl", params, data)
    else:
        data = crawl(args.url, args.depth, args.limit, args.query)

    if args.json:
        json.dump(data, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return
    base = data.get("base_url") or args.url
    renderer = render_crawl_md if args.md else render_crawl_plain
    print(renderer(data, base_url=base))


def cmd_report(args: argparse.Namespace) -> None:
    queries = _split_queries(args.queries)
    results = search_many(
        queries,
        max_results=args.max,
        depth=args.depth,
        include_raw=False,
        concurrent=True,  # 报告天然就该并发
        use_cache=args.cache,
        ttl_hours=args.ttl,
    )
    content = render_report(results, queries)
    if args.save:
        path = report_save(content, queries)
        print(f"✅ 已保存:{path}", file=sys.stderr)
        if not args.print:
            return
    sys.stdout.write(content)


def cmd_cache(args: argparse.Namespace) -> None:
    if args.action == "list":
        items = cache_list()
        if not items:
            print(f"(空)缓存目录:{cache_dir()}")
            return
        print(f"📦 缓存目录:{cache_dir()}  共 {len(items)} 条")
        print("─" * 60)
        for it in items:
            print(f"[{it['namespace']}] {it['key']}  {it['size_kb']}KB  {it['cached_at']}")
            if it["summary"]:
                print(f"    {it['summary']}")
    elif args.action == "clear":
        n = cache_clear(args.namespace)
        print(f"🗑️  已清理 {n} 条缓存(namespace={args.namespace or 'ALL'})")
    elif args.action == "dir":
        print(cache_dir())


def main() -> None:
    parser = argparse.ArgumentParser(description="Tavily 联网搜索 / 正文抓取 / 调研报告")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # search -----------------------------------------------------------------
    sp = sub.add_parser("search", help="关键词搜索")
    sp.add_argument("queries", nargs="+", help="一个或多个查询词")
    sp.add_argument("--max", type=int, default=5)
    sp.add_argument("--depth", choices=["basic", "advanced"], default="basic")
    sp.add_argument("--raw", action="store_true", help="包含原文")
    sp.add_argument("--concurrent", action="store_true", help="多查询并发")
    sp.add_argument("--cache", action="store_true", help="启用缓存(命中零烧 key)")
    sp.add_argument("--ttl", type=int, default=DEFAULT_CACHE_TTL_HOURS,
                    help=f"缓存 TTL(小时,默认 {DEFAULT_CACHE_TTL_HOURS})")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--md", action="store_true")
    sp.set_defaults(func=cmd_search)

    # fetch ------------------------------------------------------------------
    fp = sub.add_parser("fetch", help="抓取 URL 正文")
    fp.add_argument("urls", nargs="+")
    fp.add_argument("--query", help="定向抽取的引导词")
    fp.add_argument("--concurrent", action="store_true", help="多 URL 拆批并发")
    fp.add_argument("--unique", action="store_true", help="URL 去重")
    fp.add_argument("--cache", action="store_true")
    fp.add_argument("--ttl", type=int, default=DEFAULT_CACHE_TTL_HOURS)
    fp.add_argument("--json", action="store_true")
    fp.add_argument("--md", action="store_true")
    fp.set_defaults(func=cmd_fetch)

    # crawl ------------------------------------------------------------------
    cp = sub.add_parser("crawl", help="整站深度爬取(走 /crawl,Bearer 认证)")
    cp.add_argument("url", help="起始 URL")
    cp.add_argument("--depth", type=int, default=2, help="爬取深度(默认 2)")
    cp.add_argument("--limit", type=int, default=10, help="最多抓取页面数(默认 10)")
    cp.add_argument("--query", help="引导词,过滤抓取方向")
    cp.add_argument("--cache", action="store_true", help="启用缓存(整站爬取烧 key 多,强烈建议开)")
    cp.add_argument("--ttl", type=int, default=DEFAULT_CACHE_TTL_HOURS)
    cp.add_argument("--json", action="store_true")
    cp.add_argument("--md", action="store_true")
    cp.set_defaults(func=cmd_crawl)

    # report -----------------------------------------------------------------
    rp = sub.add_parser("report", help="多查询拼成调研报告")
    rp.add_argument("queries", nargs="+", help="多个查询词(报告的章节)")
    rp.add_argument("--max", type=int, default=5)
    rp.add_argument("--depth", choices=["basic", "advanced"], default="basic")
    rp.add_argument("--cache", action="store_true", help="启用缓存")
    rp.add_argument("--ttl", type=int, default=DEFAULT_CACHE_TTL_HOURS)
    rp.add_argument("--save", action="store_true",
                    help=f"保存到 {notes_dir()}/<date>_<slug>.md")
    rp.add_argument("--print", action="store_true",
                    help="和 --save 同时:保存后还打印到 stdout")
    rp.set_defaults(func=cmd_report)

    # cache ------------------------------------------------------------------
    cp = sub.add_parser("cache", help="缓存管理")
    cp.add_argument("action", choices=["list", "clear", "dir"])
    cp.add_argument("--namespace", help="clear 时指定 namespace(search/fetch)")
    cp.set_defaults(func=cmd_cache)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()