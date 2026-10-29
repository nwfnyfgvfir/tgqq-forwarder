#!/usr/bin/env python
"""只读诊断：为什么某条 RSS 条目被关键词过滤跳过。

不修改数据库、不联网、不依赖 app 包，只用标准库 sqlite3。

用法：
    python scripts/diagnose_rss_keywords.py
    python scripts/diagnose_rss_keywords.py --db data/app.db
    python scripts/diagnose_rss_keywords.py --keyword 免费
    python scripts/diagnose_rss_keywords.py --status all

判定顺序与 app/rss/worker.py::_check_feed 保持一致：
    1. link 为空                       -> 跳过（缺有效链接）
    2. 命中 exclude_keywords           -> 跳过（命中排除词）
    3. 未命中 include_keywords         -> 跳过（关键词未命中）
    4. 订阅首次基线未建立              -> 跳过（只建立基线）
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path

DEFAULT_DB = Path("data/app.db")

# 常见「看起来一样但匹配不上」的字符
SUSPICIOUS = {
    "\u200b": "零宽空格 U+200B",
    "\u200c": "零宽非连接符 U+200C",
    "\u200d": "零宽连接符 U+200D",
    "\ufeff": "BOM U+FEFF",
    "\u00a0": "不换行空格 U+00A0",
    "\u3000": "全角空格 U+3000",
    "\u2002": "en space U+2002",
    "\u2003": "em space U+2003",
}


def setup_stdout() -> None:
    """Windows 控制台默认 GBK，中文/emoji 会直接抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def load_json_list(raw: object) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return [raw] if raw.strip() else []
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, str)]
    return []


def casefold(value: str) -> str:
    return value.casefold()


def hit_words(text: str, words: list[str]) -> list[str]:
    folded = casefold(text)
    return [word for word in words if word and casefold(word) in folded]


def odd_chars(text: str) -> list[str]:
    found: list[str] = []
    for char, label in SUSPICIOUS.items():
        if char in text:
            found.append(label)
    return found


def looks_traditional(text: str) -> bool:
    """粗判繁体：只要出现「免費」这类常见繁体写法就提示。"""
    traditional = ("免費", "優惠", "分享", "好處", "福利")
    simplified = ("免费", "优惠", "分享", "好处", "福利")
    return any(t in text and s not in text for t, s in zip(traditional, simplified, strict=True))


def classify(row: sqlite3.Row, include: list[str], exclude: list[str], initialized: bool) -> str:
    text = f"{row['title']}\n{row['summary']}"
    stored = row["skip_reason"] if "skip_reason" in row.keys() else None
    matches_now = (not include or hit_words(text, include)) and not hit_words(text, exclude)
    if stored and row["status"] == "skipped":
        now = "命中" if matches_now else "仍不命中"
        return f"{stored}（入库时记录；按当前关键词{now}）"
    if not (row["link"] or "").strip():
        return "缺有效链接"
    if hit_words(text, exclude):
        return "命中排除词：" + "、".join(hit_words(text, exclude))
    if include and not hit_words(text, include):
        if row["status"] == "sent":
            return "已发送，但按当前关键词不命中（发送时用的是旧关键词，或当时还没设关键词）"
        return "未命中包含关键词"
    if not initialized:
        return "订阅首次基线未建立"
    if row["status"] == "sent":
        return "已发送，命中当前关键词"
    return "当前关键词可命中，但状态仍为跳过（关键词是后改的，历史条目不重判）"


def snippet(text: str, limit: int = 120) -> str:
    flat = re.sub(r"\s+", " ", text or "").strip()
    return flat[:limit] + ("…" if len(flat) > limit else "")


def main() -> int:
    setup_stdout()
    parser = argparse.ArgumentParser(description="诊断 RSS 订阅关键词过滤")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 数据库路径")
    parser.add_argument("--keyword", default=None, help="只关心含该词（原文子串）的条目")
    parser.add_argument(
        "--status",
        default="skipped",
        choices=["skipped", "sent", "pending", "failed", "all"],
        help="检查哪些状态的条目",
    )
    parser.add_argument("--limit", type=int, default=20, help="每个订阅最多列多少条")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"找不到数据库：{db_path}")
        print("容器里通常挂在 /app/data/app.db，可用 --db 指定。")
        return 2

    # 只读打开，避免误改线上库。
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    # 老库没有 skip_reason 列（2026-10 之后才有），缺失时用 NULL 占位。
    entry_columns = {row[1] for row in conn.execute("PRAGMA table_info(rss_entry)")}
    skip_column = "skip_reason" if "skip_reason" in entry_columns else "NULL AS skip_reason"

    subs = conn.execute(
        "SELECT id, name, feed_url, enabled, initialized, initial_policy,"
        " include_keywords, exclude_keywords FROM rss_subscription ORDER BY id"
    ).fetchall()
    if not subs:
        print("数据库里没有订阅记录。")
        return 1

    print(f"数据库：{db_path}")
    print(f"订阅数：{len(subs)}\n")

    for sub in subs:
        include = load_json_list(sub["include_keywords"])
        exclude = load_json_list(sub["exclude_keywords"])
        print("=" * 72)
        print(f"[{sub['id']}] {sub['name']}")
        print(f"  feed          : {sub['feed_url']}")
        print(f"  enabled       : {bool(sub['enabled'])}   首次基线: {bool(sub['initialized'])}"
              f"   首次策略: {sub['initial_policy']}")
        print(f"  包含关键词({len(include)}) : {include}")
        print(f"  排除关键词({len(exclude)}) : {exclude}")
        if not include:
            print("  ⚠ 包含关键词为空 = 不做包含过滤，任何带链接的新条目都会发。")
        for word in include:
            if unicodedata.normalize("NFKC", word) != word:
                print(f"  ⚠ 关键词「{word}」含兼容字符（NFKC 归一化后不同），可能匹配不上。")
            if odd_chars(word):
                print(f"  ⚠ 关键词「{word}」含不可见字符：{'、'.join(odd_chars(word))}")

        if args.status == "all":
            where, params = "", ()
        else:
            where, params = " AND status = ?", (args.status,)
        rows = conn.execute(
            "SELECT id, title, summary, link, status, published_at, last_error,"
            f" translated_title, translated_summary, {skip_column} FROM rss_entry"
            f" WHERE subscription_id = ?{where} ORDER BY id DESC",
            (sub["id"], *params),
        ).fetchall()

        if args.keyword:
            rows = [
                row
                for row in rows
                if args.keyword in f"{row['title']}\n{row['summary']}"
                or args.keyword in (row["translated_summary"] or "")
            ]

        print(f"\n  条目（{args.status}）：{len(rows)} 条"
              + (f"，已按「{args.keyword}」过滤" if args.keyword else ""))

        if not rows:
            if args.keyword:
                print(f"  ⚠ 没有任何 {args.status} 条目的标题/摘要里出现「{args.keyword}」。")
                print("    → 说明这条内容本来就没进库，或者词被拆开/换成了别的写法。")
            continue

        for row in rows[: args.limit]:
            text = f"{row['title']}\n{row['summary']}"
            reason = classify(row, include, exclude, bool(sub["initialized"]))
            print(f"\n  #{row['id']} [{row['status']}] {snippet(row['title'], 60)}")
            print(f"     判定: {reason}")
            matched_include = hit_words(text, include)
            print(f"     命中包含词: {'、'.join(matched_include) or '无'}")
            if odd_chars(text):
                print(f"     ⚠ 正文含不可见字符：{'、'.join(odd_chars(text))}")
            if looks_traditional(text):
                print("     ⚠ 正文疑似繁体（如「免費」），与简体关键词「免费」匹配不上")
            if args.keyword and args.keyword in text:
                idx = text.index(args.keyword)
                print(f"     原文片段: …{snippet(text[max(0, idx - 30): idx + 40], 90)}…")
            if row["last_error"]:
                print(f"     last_error: {row['last_error']}")
        if len(rows) > args.limit:
            print(f"\n  …还有 {len(rows) - args.limit} 条未列出（--limit 可调）")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
