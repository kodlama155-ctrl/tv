#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import urllib.parse
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from channel_policy import (
    CATEGORY_INDEX,
    channel_key,
    channel_name,
    country_from_tvg_id,
    existing_group,
    fold,
    normalize_meta,
    tvg_id,
)

ROOT = Path(__file__).resolve().parent
TR = ROOT / "tr.m3u"
ALL = ROOT / "all.m3u"
STATS = ROOT / "stats.json"

JUNK_NAME_RE = re.compile(
    r"(?:^|[ _:/-])(?:test|vpn|backup|yedek|mac zamani|dusuk kalite)(?:$|[ _:/-])",
    re.I,
)
NON_CHANNEL_NAME_RE = re.compile(
    r"(?:\b(?:film|movie|polis|smackdown|fight pass)\b|[\u0400-\u04ff])",
    re.I,
)
PROVIDER_SUFFIX_RE = re.compile(
    r"(?:\s+|[-_/])(?:HQ|UHD|FHD|HD|SD|50FPS|TEST|VPN|BACKUP|YEDEK)\s*$",
    re.I,
)


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


def _split_extinf(meta: str) -> tuple[str, str]:
    quoted = False
    escaped = False
    for i, ch in enumerate(meta):
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"':
            quoted = not quoted
            continue
        if ch == "," and not quoted:
            return meta[:i], meta[i + 1:].strip()
    return meta, ""


def _clean_provider_label(meta: str) -> str:
    head, label = _split_extinf(meta)
    label = re.sub(r"^\s*TR\s*[:|_-]\s*", "", label, flags=re.I)
    label = PROVIDER_SUFFIX_RE.sub("", label).strip()
    return f"{head},{label}" if label else meta


def _host(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except Exception:
        return ""


def _is_noisy_provider(url: str) -> bool:
    host = _host(url)
    return host.endswith("europlayiptv.de") or host.endswith("prosto.tv")


def _admit(meta: str, category: str, url: str) -> tuple[bool, str]:
    name = channel_name(meta)

    if JUNK_NAME_RE.search(name) or NON_CHANNEL_NAME_RE.search(name):
        return False, "junk-name"
    if _is_noisy_provider(url):
        return False, "noisy-provider"

    # Anything the catalog/category engine recognizes is accepted.
    if category != "Diğer":
        return True, "recognized"

    # For new/uncatalogued channels, require an explicit Turkish tvg identity.
    # This keeps legitimate newly discovered/local stations while rejecting
    # anonymous provider aliases and unrelated streams.
    if country_from_tvg_id(meta) == "tr":
        return True, "new-tr-channel"

    return False, "unreviewed"


def _score(item: dict) -> tuple:
    return (
        1 if item["category"] != "Diğer" else 0,
        1 if item["url"].startswith("https://") else 0,
        1 if not _is_noisy_provider(item["url"]) else 0,
        -item["index"],
    )


def main() -> None:
    source = ALL if ALL.exists() else TR
    if not source.exists():
        raise SystemExit("No playlist source found")

    raw_entries = _parse_entries(source.read_text(encoding="utf-8"))

    accepted = []
    rejected = Counter()

    for item in raw_entries:
        cleaned_meta = _clean_provider_label(item["meta"])
        normalized_meta, category, _ = normalize_meta(cleaned_meta)
        ok, reason = _admit(normalized_meta, category, item["url"])
        if not ok:
            rejected[reason] += 1
            continue

        item = dict(item)
        item["meta"] = normalized_meta
        item["category"] = category
        item["key"] = channel_key(normalized_meta)
        item["admit_reason"] = reason
        accepted.append(item)

    # Semantic dedupe: one channel identity, one best stream.
    by_key: dict[str, list[dict]] = {}
    for item in accepted:
        key = item["key"] or ("url:" + item["url"])
        by_key.setdefault(key, []).append(item)

    entries = [max(rows, key=_score) for rows in by_key.values()]
    category_counts = Counter(item["category"] for item in entries)
    admit_counts = Counter(item["admit_reason"] for item in entries)

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

    try:
        stats = json.loads(STATS.read_text(encoding="utf-8")) if STATS.exists() else {}
    except Exception:
        stats = {}

    stats["last_fast_rebuild_utc"] = datetime.now(timezone.utc).isoformat()
    stats["fast_rebuild_source"] = source.name
    stats["fast_rebuild_raw_channels"] = len(raw_entries)
    stats["fast_rebuild_channels"] = len(entries)
    stats["fast_rebuild_duplicates_collapsed"] = len(accepted) - len(entries)
    stats["fast_rebuild_categories"] = dict(category_counts)
    stats["fast_rebuild_admission"] = dict(admit_counts)
    stats["fast_rebuild_rejected"] = dict(rejected)
    STATS.write_text(
        json.dumps(stats, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(json.dumps({
        "mode": "fast-channel-filter",
        "source": source.name,
        "raw_channels": len(raw_entries),
        "accepted_before_dedupe": len(accepted),
        "channels": len(entries),
        "duplicates_collapsed": len(accepted) - len(entries),
        "categories": dict(category_counts),
        "admission": dict(admit_counts),
        "rejected": dict(rejected),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
