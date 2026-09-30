#!/usr/bin/env python3
from __future__ import annotations

import base64
import datetime as dt
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from channel_catalog import CHANNELS
from channel_policy import normalize_identity, split_extinf, tvg_id

ROOT = Path(__file__).resolve().parent
TR = ROOT / "tr.m3u"
OUTPUT = ROOT / "missing_discovered.m3u"
STATS = ROOT / "missing_discovery_stats.json"

API = "https://api.github.com"
TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
UA = "EmirTV-Missing-Discovery/1.0"

# GitHub's authenticated code-search bucket is intentionally small.
# Search a rotating subset on each 6-hour full run instead of slowing every
# update with minute-long rate-limit sleeps.
MAX_TARGET_SEARCHES = 8
MAX_RESULTS_PER_TARGET = 10
MAX_FILE_BYTES = 2_000_000

M3U8_RE = re.compile(
    r'https?://[^\s"\'<>]+?\.m3u8(?:\?[^\s"\'<>]*)?',
    re.I,
)
SENSITIVE_QUERY_KEYS = {
    "token", "auth", "authorization", "password", "passwd", "username",
    "user", "key", "sig", "signature", "jwt", "session", "hdnts", "hdnea",
}
CREDENTIAL_PATH_RE = re.compile(
    r"/(?:iptv|live)/[A-Za-z0-9_-]{6,}/[A-Za-z0-9_-]{6,}/",
    re.I,
)


def request_json(url: str):
    headers = {
        "User-Agent": UA,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=25) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def safe_candidate(url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    if parsed.username or parsed.password:
        return False
    if CREDENTIAL_PATH_RE.search(parsed.path):
        return False
    keys = {
        key.lower()
        for key, _ in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    }
    return not bool(keys & SENSITIVE_QUERY_KEYS)


def canonical_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query, "")
    )


def parse_playlist(text: str):
    entries = []
    meta = None
    options = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            meta = line
            options = {}
            continue
        if line.startswith("#EXTVLCOPT:") and meta:
            payload = line.split(":", 1)[1]
            if "=" in payload:
                key, value = payload.split("=", 1)
                key = key.strip().lower()
                value = value.strip()
                if key == "http-user-agent":
                    options["user_agent"] = value
                elif key in {"http-referrer", "http-referer"}:
                    options["referrer"] = value
            continue
        if line.startswith("#"):
            continue
        if not meta:
            continue
        match = M3U8_RE.search(line)
        if match:
            url = match.group(0).rstrip("),;")
            if safe_candidate(url):
                entries.append({
                    "meta": meta,
                    "url": url,
                    "user_agent": options.get("user_agent"),
                    "referrer": options.get("referrer"),
                })
        meta = None
        options = {}
    return entries


def playlist_keys(meta: str) -> set[str]:
    _, label = split_extinf(meta)
    return {
        key
        for key in (
            normalize_identity(tvg_id(meta)),
            normalize_identity(label),
        )
        if key
    }


def target_keys(row: dict) -> set[str]:
    return {
        key
        for key in (
            normalize_identity(row.get("id", "")),
            normalize_identity(row.get("name", "")),
        )
        if key
    }


def existing_keys() -> set[str]:
    if not TR.exists():
        return set()
    keys = set()
    for line in TR.read_text(encoding="utf-8").splitlines():
        if line.startswith("#EXTINF"):
            keys.update(playlist_keys(line))
    return keys


def source_is_turkish(item: dict, meta: str) -> bool:
    repo = item.get("repository", {}) or {}
    text = " ".join([
        str(repo.get("full_name") or ""),
        str(repo.get("description") or ""),
        str(item.get("path") or ""),
        str(meta or ""),
    ]).lower()
    hints = (
        "turkey", "turkiye", "türkiye", "turkish", "türk",
        "tr.m3u", "tr.m3u8", "/tr/", "_tr.", "-tr.",
        'tvg-country="tr"', ".tr@",
    )
    return any(token in text for token in hints)


def strong_match(row: dict, entry: dict, item: dict) -> bool:
    keys = playlist_keys(entry["meta"])
    target = target_keys(row)
    if not keys or not target or not (keys & target):
        return False

    source_id = normalize_identity(tvg_id(entry["meta"]))
    target_id = normalize_identity(row.get("id", ""))
    if source_id and target_id and source_id == target_id:
        return True

    # Name-only matches are accepted only from Turkish-context playlists.
    return source_is_turkish(item, entry["meta"])


def search_target(row: dict):
    name = str(row.get("name") or "").strip()
    query = f'"{name}" extension:m3u'
    encoded = urllib.parse.urlencode({
        "q": query,
        "per_page": str(MAX_RESULTS_PER_TARGET),
    })
    data = request_json(f"{API}/search/code?{encoded}")
    return query, data.get("items", [])


def fetch_search_file(item: dict):
    data = request_json(item["url"])
    if data.get("encoding") != "base64":
        return None
    payload = str(data.get("content") or "").replace("\n", "")
    raw = base64.b64decode(payload)
    if len(raw) > MAX_FILE_BYTES:
        return None
    return raw.decode("utf-8", errors="replace")


def main():
    now = dt.datetime.now(dt.timezone.utc)
    present = existing_keys()
    missing = [
        row for row in CHANNELS
        if not (target_keys(row) & present)
    ]

    previous = {}
    if STATS.exists():
        try:
            previous = json.loads(STATS.read_text(encoding="utf-8"))
        except Exception:
            previous = {}

    previous_cursor = int(previous.get("cursor") or 0)
    if missing:
        start = previous_cursor % len(missing)
        ordered = missing[start:] + missing[:start]
    else:
        start = 0
        ordered = []

    targets = ordered[:MAX_TARGET_SEARCHES]
    next_cursor = (
        (start + len(targets)) % len(missing)
        if missing else 0
    )

    found = {}
    target_report = []
    errors = 0

    for row in targets:
        report = {
            "name": row["name"],
            "category": row["category"],
            "search_results": 0,
            "files_scanned": 0,
            "candidates": 0,
        }
        try:
            query, items = search_target(row)
            report["query"] = query
            report["search_results"] = len(items)

            for item in items:
                try:
                    text = fetch_search_file(item)
                    if not text:
                        continue
                    report["files_scanned"] += 1
                    for entry in parse_playlist(text):
                        if not strong_match(row, entry, item):
                            continue
                        key = canonical_url(entry["url"])
                        if key in found:
                            continue
                        found[key] = {
                            **entry,
                            "target": row,
                            "repo": (item.get("repository") or {}).get("full_name", ""),
                            "path": item.get("path", ""),
                        }
                        report["candidates"] += 1
                except Exception:
                    errors += 1

        except urllib.error.HTTPError as exc:
            report["error"] = f"HTTP {exc.code}"
            errors += 1
            if exc.code in {403, 429}:
                # Avoid hammering the search bucket after rate limiting.
                target_report.append(report)
                break
        except Exception as exc:
            report["error"] = type(exc).__name__
            errors += 1

        target_report.append(report)

    lines = ["#EXTM3U"]
    for item in sorted(found.values(), key=lambda row: (row["target"]["name"], row["url"])):
        target = item["target"]
        target_id = target.get("id", "")
        id_attr = f' tvg-id="{target_id}"' if target_id else ""
        lines.append(f'# SOURCE: github-targeted:{item["repo"]}/{item["path"]}')
        lines.append(
            f'#EXTINF:-1{id_attr} group-title="{target["category"]}",{target["name"]}'
        )
        if item.get("referrer"):
            lines.append(f'#EXTVLCOPT:http-referrer={item["referrer"]}')
        if item.get("user_agent"):
            lines.append(f'#EXTVLCOPT:http-user-agent={item["user_agent"]}')
        lines.append(item["url"])

    OUTPUT.write_text("\n".join(lines) + "\n", encoding="utf-8")

    stats = {
        "updated_at_utc": now.isoformat(),
        "catalog_channels": len(CHANNELS),
        "channels_in_previous_tr": len(CHANNELS) - len(missing),
        "missing_before_search": len(missing),
        "missing_names": [row["name"] for row in missing],
        "max_target_searches_per_run": MAX_TARGET_SEARCHES,
        "cursor": next_cursor,
        "targets_searched": len(target_report),
        "target_report": target_report,
        "unique_candidates_found": len(found),
        "errors": errors,
        "note": (
            "Searches only catalog channels absent from the previous tr.m3u. "
            "Candidates are still validated by checker.py before entering tr.m3u."
        ),
    }
    STATS.write_text(
        json.dumps(stats, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
