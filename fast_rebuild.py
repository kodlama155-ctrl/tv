#!/usr/bin/env python3
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from channel_policy import CATEGORY_INDEX, normalize_meta

ROOT = Path(__file__).resolve().parent
TR = ROOT / "tr.m3u"
STATS = ROOT / "stats.json"


def _parse_entries(text: str) -> list[dict]:
    entries = []
    meta = None
    options = []

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue

        if line.startswith("#EXTINF"):
            meta = line
            options = []
            continue

        if meta and line.startswith("#EXTVLCOPT:"):
            options.append(line)
            continue

        if meta and line.startswith(("http://", "https://")):
            entries.append({
                "meta": meta,
                "options": list(options),
                "url": line,
                "index": len(entries),
            })
            meta = None
            options = []

    return entries


def _apply_category(meta: str) -> tuple[str, str]:
    normalized_meta, category, _ = normalize_meta(meta)
    return normalized_meta, category


def main() -> None:
    if not TR.exists():
        raise SystemExit("tr.m3u not found; run a full update first")

    entries = _parse_entries(TR.read_text(encoding="utf-8"))
    category_counts = Counter()

    for item in entries:
        item["meta"], item["category"] = _apply_category(item["meta"])
        category_counts[item["category"]] += 1

    entries.sort(
        key=lambda item: (
            CATEGORY_INDEX.get(item["category"], 999),
            item["index"],
        )
    )

    lines = ["#EXTM3U"]
    for item in entries:
        lines.append(item["meta"])
        lines.extend(item["options"])
        lines.append(item["url"])

    TR.write_text("\n".join(lines) + "\n", encoding="utf-8")

    if STATS.exists():
        try:
            stats = json.loads(STATS.read_text(encoding="utf-8"))
        except Exception:
            stats = {}
        stats["last_fast_rebuild_utc"] = datetime.now(timezone.utc).isoformat()
        stats["fast_rebuild_categories"] = dict(category_counts)
        STATS.write_text(
            json.dumps(stats, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    print(json.dumps({
        "mode": "fast",
        "channels": len(entries),
        "categories": dict(category_counts),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
