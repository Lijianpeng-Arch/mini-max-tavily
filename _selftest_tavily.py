#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
_selftest_tavily.py —— tavily-search.py 跨 agent 自检脚本

任何 agent(claude / hermes / crow / 未来新人)在工作目录里跑一次,
就能确认:Tavily CLI 是否就绪、key 在不在、缓存能不能写、5 个子命令能不能跑。

设计原则:
  1. 不烧多余 key(只调一次最小 search + 一次最小 fetch 验证端点可达)
  2. 用 stderr 打 ❌/✅,exit code 0=全过,1=有失败
  3. 跨路径友好:不假设自己在 D:\claude work,从脚本父目录反推工作根

用法:
  python 工具区/脚本/_selftest_tavily.py           # 跑全套
  python 工具区/脚本/_selftest_tavily.py --quick    # 只跑前 3 项(不烧 key)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

# 让自检脚本也能用主脚本的内部函数
# 注意:主脚本文件名是 tavily-search.py(含连字符),Python 不能直接 import,
# 用 importlib + spec_from_file_location 直接按路径加载。
SCRIPT_DIR = Path(__file__).resolve().parent
MAIN_SCRIPT = SCRIPT_DIR / "tavily-search.py"
if not MAIN_SCRIPT.exists():
    sys.exit(f"❌ 找不到主脚本:{MAIN_SCRIPT}(自检脚本必须和它放在同一目录)")

import importlib.util
_spec = importlib.util.spec_from_file_location("tavily_search", MAIN_SCRIPT)
if _spec is None or _spec.loader is None:
    sys.exit(f"❌ 无法加载主脚本:{MAIN_SCRIPT}")
ts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ts)


# ---------------------------------------------------------------------------
# 检查项
# ---------------------------------------------------------------------------

def check_import() -> tuple[bool, str]:
    """检查主脚本能 import。"""
    return True, f"导入成功:{ts.__file__}"


def check_api_key() -> tuple[bool, str]:
    """检查 TAVILY_API_KEY 环境变量存在且非空。"""
    key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not key:
        return False, "缺少环境变量 TAVILY_API_KEY"
    # 不打印完整 key,只显示前 8 位
    return True, f"key 已配置({key[:8]}...)"


def check_work_root() -> tuple[bool, str]:
    """检查工作目录能被反推定位。

    工作根的标志:主人 CLAUDE.md 写的 AGENTS.md(六区协作规则)或 说明.md。
    注意:CLAUDE.md 在用户配置目录(C:\\Users\\test123\\.claude\\),不在工作根。
    """
    root = ts._work_root()
    if not root.exists():
        return False, f"工作根不存在:{root}"
    markers = ["AGENTS.md", "说明.md"]
    found = [m for m in markers if (root / m).exists()]
    if not found:
        return False, f"工作根={root},但找不到 {markers}(可能定位错了?)"
    return True, f"工作根:{root}(标记:{','.join(found)})"


def check_dirs_writable() -> tuple[bool, str]:
    """检查缓存目录能创建并写入。"""
    try:
        cache = ts.cache_dir()
        cache.mkdir(parents=True, exist_ok=True)
        # 写个探针文件
        probe = cache / "._selftest_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        notes = ts.notes_dir()
        notes.mkdir(parents=True, exist_ok=True)
        return True, f"缓存:{cache}  笔记:{notes}"
    except OSError as e:
        return False, f"目录不可写:{e}"


def check_search_endpoint() -> tuple[bool, str]:
    """最小 search 调用,验证端点可达 + 鉴权通过。"""
    try:
        data = ts.search("test", max_results=1, depth="basic")
        results = data.get("results") or []
        return True, f"search 端点通,返回 {len(results)} 条结果"
    except SystemExit as e:
        return False, str(e)


def check_fetch_endpoint() -> tuple[bool, str]:
    """最小 fetch 调用,验证 /extract 端点可达。"""
    try:
        # 用 Tavily 自己的文档站,小、稳定
        data = ts.fetch(["https://docs.tavily.com/"], query=None)
        results = data.get("results") or []
        return True, f"extract 端点通,返回 {len(results)} 条"
    except SystemExit as e:
        return False, str(e)


def check_crawl_endpoint() -> tuple[bool, str]:
    """最小 crawl 调用,验证 /crawl 端点可达(Bearer 认证)。

    Tavily 要求 max_depth >= 1,所以用 depth=1 + limit=1(只抓 1 页,省 key)。
    """
    try:
        data = ts.crawl("https://docs.tavily.com/", max_depth=1, limit=1)
        results = data.get("results") or []
        return True, f"crawl 端点通(Bearer 认证),返回 {len(results)} 条"
    except SystemExit as e:
        return False, str(e)


def check_cache_roundtrip() -> tuple[bool, str]:
    """验证缓存读写能跑通。"""
    try:
        cache = ts.cache_dir()
        # 写一个临时测试 namespace
        ns = "_selftest_tmp"
        params = {"hello": "world", "n": 42}
        ts.cache_write(ns, params, {"answer": 42})
        hit = ts.cache_read(ns, params, ttl_hours=1)
        if not hit or hit.get("answer") != 42:
            return False, "缓存写入成功但读取失败"
        # 清理
        ts.cache_clear(ns)
        return True, f"缓存 roundtrip OK(目录:{cache})"
    except Exception as e:
        return False, f"缓存异常:{e}"


# ---------------------------------------------------------------------------
# 跑测
# ---------------------------------------------------------------------------

# (名字, 函数, 是否烧 key)
CHECKS = [
    ("导入主脚本", check_import, False),
    ("TAVILY_API_KEY 环境变量", check_api_key, False),
    ("工作根定位", check_work_root, False),
    ("目录可写", check_dirs_writable, False),
    ("缓存 roundtrip", check_cache_roundtrip, False),
    ("search 端点", check_search_endpoint, True),
    ("extract 端点", check_fetch_endpoint, True),
    ("crawl 端点(Bearer)", check_crawl_endpoint, True),
]


def run(quick: bool = False) -> int:
    print("=" * 60)
    print("🩺 tavily-search.py 自检")
    print(f"   工作根:{ts._work_root()}")
    print(f"   模式:{'QUICK(不烧 key)' if quick else 'FULL(会调 3 个端点)'}")
    print("=" * 60)
    print()

    passed = 0
    failed = 0
    burn_count = 0

    for name, fn, burns_key in CHECKS:
        if quick and burns_key:
            print(f"⏭️  [跳过]{name}(会烧 key)")
            continue
        try:
            ok, msg = fn()
            mark = "✅" if ok else "❌"
            print(f"{mark} {name}:{msg}")
            if ok:
                passed += 1
            else:
                failed += 1
            if burns_key and ok:
                burn_count += 1
        except Exception as e:
            print(f"❌ {name}:异常 {type(e).__name__}: {e}")
            failed += 1

    print()
    print("=" * 60)
    print(f"📊 结果:{passed} 通过 / {failed} 失败 / 共 {passed + failed} 项")
    if burn_count:
        print(f"💸 烧了 {burn_count} 次 key")
    if failed == 0:
        print("🎉 全部就绪 —— 主人和 hermes/crow 都能放心用!")
    else:
        print("⚠️  有失败项,上面 ❌ 行就是原因")
    print("=" * 60)
    return 0 if failed == 0 else 1


def main() -> None:
    p = argparse.ArgumentParser(description="tavily-search.py 跨 agent 自检")
    p.add_argument("--quick", action="store_true", help="只跑前 5 项(不烧 key)")
    args = p.parse_args()
    sys.exit(run(quick=args.quick))


if __name__ == "__main__":
    main()