#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from channel_policy import channel_key, split_extinf

ROOT = Path(__file__).resolve().parent
DEFAULT_TARGETS = ROOT / "repair_targets.json"
DEFAULT_OUTPUT = ROOT / "repair_discovered.m3u"
DEFAULT_STATS = ROOT / "repair_discovery_stats.json"

API = "https://api.github.com"
TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
UA = "EmirTV-Targeted-Repair-Discovery/1.0"

BATCH_SIZE = 4
MAX_RESULTS_PER_BATCH = 30
SEARCH_DELAY_SECONDS = 4
MAX_FILE_BYTES = 2_000_000

M3U8_RE = re.compile(
    r'https?://[^\s"\'<>]+?\.m3u8(?:\?[^\s"\'<>]*)?',
    re.I,
)
SENSITIVE_QUERY_KEYS: set[str] = set()
EXPLICIT_CREDENTIAL_PATH_RE = re.compile(
    r"/(?:user(?:name)?|pass(?:word)?|token|auth(?:orization)?|session|jwt|key)(?:=|/)[^/?#]+",
    re.I,
)


def request_json(url: str, retry_limited: bool = False):
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
        if retry_limited and exc.code in {403, 429}:
            time.sleep(10)
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
    if EXPLICIT_CREDENTIAL_PATH_RE.search(parsed.path):
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
                _, label = split_extinf(meta)
                entries.append({
                    "meta": meta,
                    "name": label or "Unknown",
                    "channel_key": channel_key(meta, label),
                    "url": url,
                    "user_agent": options.get("user_agent"),
                    "referrer": options.get("referrer"),
                })

        meta = None
        options = {}

    return entries


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
        'tvg-country="tr"', ".tr@", ".tr\"",
    )
    return any(token in text for token in hints)


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
    parser = argparse.ArgumentParser()
    parser.add_argument("--targets", default=str(DEFAULT_TARGETS))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--stats", default=str(DEFAULT_STATS))
    args = parser.parse_args()

    targets = json.loads(Path(args.targets).read_text(encoding="utf-8"))
    targets = [
        row for row in targets
        if row.get("channel_key") and row.get("name")
    ]

    if not targets:
        Path(args.output).write_text("#EXTM3U\n", encoding="utf-8")
        Path(args.stats).write_text(
            json.dumps({"targets": 0, "candidates": 0}, indent=2) + "\n",
            encoding="utf-8",
        )
        print('{"targets": 0, "candidates": 0}')
        return

    target_by_key = {row["channel_key"]: row for row in targets}
    found = {}
    seen_files = set()
    batch_report = []
    errors = 0

    for batch_index, batch in enumerate(batches(targets, BATCH_SIZE)):
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
                retry_limited=True,
            )
            items = data.get("items", [])
            batch_report.append({
                "names": [row["name"] for row in batch],
                "search_results": len(items),
                "fallback_individual": False,
            })
        except Exception as exc:
            # A failed OR batch must not silently skip every channel in it.
            # Retry each target independently and merge the successful results.
            errors += 1
            items = []
            individual = []
            for row in batch:
                time.sleep(2)
                single_query = f'"{row["name"]}" extension:m3u'
                single_encoded = urllib.parse.urlencode({
                    "q": single_query,
                    "per_page": str(MAX_RESULTS_PER_BATCH),
                })
                try:
                    single = request_json(
                        f"{API}/search/code?{single_encoded}",
                        retry_limited=True,
                    )
                    single_items = single.get("items", [])
                    items.extend(single_items)
                    individual.append({
                        "name": row["name"],
                        "search_results": len(single_items),
                    })
                except Exception as single_exc:
                    errors += 1
                    individual.append({
                        "name": row["name"],
                        "error": type(single_exc).__name__,
                    })

            batch_report.append({
                "names": [row["name"] for row in batch],
                "error": type(exc).__name__,
                "fallback_individual": True,
                "individual": individual,
                "search_results": len(items),
            })

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

            repo = (item.get("repository") or {}).get("full_name", "")
            path = item.get("path", "")

            for entry in entries:
                target = target_by_key.get(entry["channel_key"])
                if target is None:
                    continue
                # Common names (TV 1, Kanal 1, Can TV, etc.) collide across
                # countries. Require Turkish source/metadata evidence before
                # admitting a GitHub repair candidate.
                if not source_is_turkish(item, entry["meta"]):
                    continue
                if canonical_url(entry["url"]) == canonical_url(target["old_url"]):
                    continue

                key = (
                    entry["channel_key"],
                    canonical_url(entry["url"]),
                    entry.get("user_agent") or "",
                    entry.get("referrer") or "",
                )
                found.setdefault(key, {
                    **entry,
                    "target_name": target["name"],
                    "repo": repo,
                    "path": path,
                })

    lines = ["#EXTM3U"]
    for row in sorted(found.values(), key=lambda x: (x["target_name"], x["url"])):
        lines.append(f'# SOURCE: github-targeted-repair:{row["repo"]}/{row["path"]}')
        lines.append(row["meta"])
        if row.get("referrer"):
            lines.append(f'#EXTVLCOPT:http-referrer={row["referrer"]}')
        if row.get("user_agent"):
            lines.append(f'#EXTVLCOPT:http-user-agent={row["user_agent"]}')
        lines.append(row["url"])

    Path(args.output).write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )

    stats = {
        "mode": "targeted-dead-channel-discovery",
        "targets": len(targets),
        "target_names": [row["name"] for row in targets],
        "files_scanned": len(seen_files),
        "unique_candidates": len(found),
        "search_batches": batch_report,
        "errors": errors,
    }
    Path(args.stats).write_text(
        json.dumps(stats, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
