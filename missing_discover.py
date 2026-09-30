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
UA = "EmirTV-Missing-Discovery/2.0"

# GitHub code search supports OR. Six channel names per query keeps the query
# compact and uses only two search requests for a typical 12-channel gap.
BATCH_SIZE = 6
MAX_RESULTS_PER_BATCH = 50
SEARCH_DELAY_SECONDS = 7
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


def request_json(url: str, retry_429: bool = False):
    headers = {
        "User-Agent": UA,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"

    def once():
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=25) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))

    try:
        return once()
    except urllib.error.HTTPError as exc:
        if retry_429 and exc.code in {403, 429}:
            time.sleep(12)
            return once()
        raise


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

    return source_is_turkish(item, entry["meta"])


def fetch_search_file(item: dict):
    data = request_json(item["url"])
    if data.get("encoding") != "base64":
        return None
    payload = str(data.get("content") or "").replace("\n", "")
    raw = base64.b64decode(payload)
    if len(raw) > MAX_FILE_BYTES:
        return None
    return raw.decode("utf-8", errors="replace")


def batches(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def main():
    now = dt.datetime.now(dt.timezone.utc)
    present = existing_keys()
    missing = [
        row for row in CHANNELS
        if not (target_keys(row) & present)
    ]

    reports = {
        row["name"]: {
            "name": row["name"],
            "category": row["category"],
            "files_matched": 0,
            "candidates": 0,
        }
        for row in missing
    }

    found = {}
    seen_files = set()
    batch_report = []
    errors = 0

    for batch_index, batch in enumerate(batches(missing, BATCH_SIZE)):
        if batch_index:
            time.sleep(SEARCH_DELAY_SECONDS)

        query = " OR ".join(f'"{row["name"]}"' for row in batch) + " extension:m3u"
        encoded = urllib.parse.urlencode({
            "q": query,
            "per_page": str(MAX_RESULTS_PER_BATCH),
        })

        try:
            data = request_json(
                f"{API}/search/code?{encoded}",
                retry_429=True,
            )
            items = data.get("items", [])
            batch_report.append({
                "names": [row["name"] for row in batch],
                "query": query,
                "search_results": len(items),
            })
        except Exception as exc:
            errors += 1
            batch_report.append({
                "names": [row["name"] for row in batch],
                "query": query,
                "error": type(exc).__name__,
            })
            continue

        for item in items:
            file_key = item.get("url") or ""
            if not file_key or file_key in seen_files:
                continue
            seen_files.add(file_key)

            try:
                text = fetch_search_file(item)
                if not text:
                    continue
                entries = parse_playlist(text)
            except Exception:
                errors += 1
                continue

            matched_names = set()

            for row in missing:
                for entry in entries:
                    if not strong_match(row, entry, item):
                        continue

                    matched_names.add(row["name"])
                    key = canonical_url(entry["url"])
                    if key in found:
                        continue

                    found[key] = {
                        **entry,
                        "target": row,
                        "repo": (item.get("repository") or {}).get("full_name", ""),
                        "path": item.get("path", ""),
                    }
                    reports[row["name"]]["candidates"] += 1

            for name in matched_names:
                reports[name]["files_matched"] += 1

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
        "batch_size": BATCH_SIZE,
        "search_delay_seconds": SEARCH_DELAY_SECONDS,
        "search_batches": batch_report,
        "target_report": list(reports.values()),
        "files_scanned": len(seen_files),
        "unique_candidates_found": len(found),
        "errors": errors,
        "note": (
            "Only channels absent from the previous tr.m3u are searched. "
            "Candidates must still pass checker.py HLS validation."
        ),
    }

    STATS.write_text(
        json.dumps(stats, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
