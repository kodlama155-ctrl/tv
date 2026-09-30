#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import json
import urllib.request
from pathlib import Path

from channel_policy import SOURCE_PRIORITY, channel_key, split_extinf
from checker import is_known_false_identity
from hls_validator import validate_hls

ROOT = Path(__file__).resolve().parent
DEFAULT_PLAYLIST = ROOT / "tr.m3u"
DEFAULT_TARGETS = ROOT / "repair_targets.json"
DEFAULT_REPORT = ROOT / "repair_result.json"
SOURCES = ROOT / "sources.txt"

LOCAL_CANDIDATE_FILES = [
    ("priority_sources.m3u", "priority"),
    ("official_discovered.m3u", "official_html"),
    ("turkuvaz_discovered.m3u", "official_api"),
    ("browser_discovered.m3u", "official_browser"),
    ("repair_discovered.m3u", "github_discovery"),
    ("missing_discovered.m3u", "github_discovery"),
    ("discovered.m3u", "github_discovery"),
    ("a.m3u", "unknown"),
    ("all.m3u", "unknown"),
]

UA = "Mozilla/5.0 (EmirTV-Targeted-Repair/1.0)"
PLAYLIST_TIMEOUT = 20
MAX_PLAYLIST_BYTES = 8_000_000
MAX_WORKERS = 8


def canonical(url: str) -> str:
    from urllib.parse import urlsplit, urlunsplit
    p = urlsplit(url)
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path, p.query, ""))


def parse_playlist_text(text: str, source_kind: str, source_name: str):
    rows = []
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

        if meta and line.startswith(("http://", "https://")):
            _, label = split_extinf(meta)
            rows.append({
                "meta": meta,
                "name": label or "Unknown",
                "channel_key": channel_key(meta, label),
                "url": line,
                "user_agent": options.get("user_agent"),
                "referrer": options.get("referrer"),
                "source_kind": source_kind,
                "source_name": source_name,
            })
            meta = None
            options = {}

    return rows


def parse_file(path: Path, source_kind: str):
    if not path.exists():
        return []
    return parse_playlist_text(
        path.read_text(encoding="utf-8"),
        source_kind,
        path.name,
    )


def fetch_text(url: str):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "*/*"},
    )
    with urllib.request.urlopen(req, timeout=PLAYLIST_TIMEOUT) as response:
        raw = response.read(MAX_PLAYLIST_BYTES)
    return raw.decode("utf-8", errors="replace")


def current_playlist_rows(path: Path):
    return parse_file(path, "current")


def write_playlist(path: Path, rows):
    lines = ["#EXTM3U"]
    for row in rows:
        lines.append(row["meta"])
        if row.get("referrer"):
            lines.append(f'#EXTVLCOPT:http-referrer={row["referrer"]}')
        if row.get("user_agent"):
            lines.append(f'#EXTVLCOPT:http-user-agent={row["user_agent"]}')
        lines.append(row["url"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def probe(row: dict):
    try:
        result = validate_hls(
            row["url"],
            user_agent=row.get("user_agent"),
            referrer=row.get("referrer"),
        )
    except Exception as exc:
        result = {"status": "unknown", "reason": type(exc).__name__}

    out = dict(row)
    out.update(result)
    out["status"] = result.get("status", "unknown")
    return out


def choose_verified(rows):
    verified = [row for row in rows if row["status"] == "verified"]
    if not verified:
        return None

    return max(
        verified,
        key=lambda row: (
            SOURCE_PRIORITY.get(row.get("source_kind", "unknown"), 0),
            int(row.get("height") or 0),
            int(row.get("bandwidth") or 0),
            1 if row["url"].startswith("https://") else 0,
            -int(row.get("manifest_latency_ms") or 999999),
        ),
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--playlist", default=str(DEFAULT_PLAYLIST))
    parser.add_argument("--targets", default=str(DEFAULT_TARGETS))
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    args = parser.parse_args()

    playlist_path = Path(args.playlist)
    targets = json.loads(Path(args.targets).read_text(encoding="utf-8"))
    targets = [row for row in targets if row.get("channel_key")]

    if not targets:
        payload = {
            "mode": "targeted-repair",
            "targets": 0,
            "repaired": 0,
            "unresolved": [],
            "results": [],
        }
        Path(args.report).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    target_by_key = {row["channel_key"]: row for row in targets}
    target_keys = set(target_by_key)
    candidates = []

    for filename, source_kind in LOCAL_CANDIDATE_FILES:
        for row in parse_file(ROOT / filename, source_kind):
            if row["channel_key"] in target_keys:
                candidates.append(row)

    source_report = []
    if SOURCES.exists():
        source_urls = [
            line.strip()
            for line in SOURCES.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        for src in source_urls:
            try:
                source_kind = "iptv_org" if "iptv-org.github.io" in src else "upstream"
                parsed = parse_playlist_text(fetch_text(src), source_kind, src)
                matched = [row for row in parsed if row["channel_key"] in target_keys]
                candidates.extend(matched)
                source_report.append({
                    "url": src,
                    "status": "ok",
                    "matched_candidates": len(matched),
                })
            except Exception as exc:
                source_report.append({
                    "url": src,
                    "status": "error",
                    "error": type(exc).__name__,
                })

    old_urls = {
        (row["channel_key"], canonical(row["old_url"]))
        for row in targets
        if row.get("old_url")
    }

    unique = {}
    for row in candidates:
        if is_known_false_identity(row):
            continue
        if (row["channel_key"], canonical(row["url"])) in old_urls:
            continue
        key = (
            row["channel_key"],
            canonical(row["url"]),
            row.get("user_agent") or "",
            row.get("referrer") or "",
        )
        current = unique.get(key)
        if current is None:
            unique[key] = row
            continue
        if SOURCE_PRIORITY.get(row["source_kind"], 0) > SOURCE_PRIORITY.get(current["source_kind"], 0):
            unique[key] = row

    candidate_rows = list(unique.values())

    if candidate_rows:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(MAX_WORKERS, len(candidate_rows))
        ) as executor:
            probed = list(executor.map(probe, candidate_rows))
    else:
        probed = []

    by_key = {}
    for row in probed:
        by_key.setdefault(row["channel_key"], []).append(row)

    existing = current_playlist_rows(playlist_path)
    replacements = {}
    repaired = []
    unresolved = []

    for key, target in target_by_key.items():
        chosen = choose_verified(by_key.get(key, []))
        if chosen is None:
            unresolved.append({
                "name": target.get("name"),
                "channel_key": key,
                "candidates_tested": len(by_key.get(key, [])),
            })
            continue

        replacements[key] = chosen
        repaired.append({
            "name": target.get("name"),
            "channel_key": key,
            "old_url": target.get("old_url"),
            "new_url": chosen["url"],
            "source_kind": chosen.get("source_kind"),
            "source_name": chosen.get("source_name"),
            "resolution": chosen.get("resolution"),
            "bandwidth": chosen.get("bandwidth"),
            "manifest_latency_ms": chosen.get("manifest_latency_ms"),
            "segment_latency_ms": chosen.get("segment_latency_ms"),
        })

    if replacements:
        updated = []
        for row in existing:
            chosen = replacements.get(row["channel_key"])
            if chosen is None:
                updated.append(row)
                continue

            changed = dict(row)
            changed["url"] = chosen["url"]
            changed["user_agent"] = chosen.get("user_agent")
            changed["referrer"] = chosen.get("referrer")
            updated.append(changed)

        write_playlist(playlist_path, updated)

    payload = {
        "mode": "targeted-repair",
        "targets": len(targets),
        "candidate_count": len(candidate_rows),
        "tested_count": len(probed),
        "repaired": len(repaired),
        "replacements": repaired,
        "unresolved": unresolved,
        "sources": source_report,
        "results": probed,
    }
    Path(args.report).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "targets": len(targets),
        "candidate_count": len(candidate_rows),
        "repaired": len(repaired),
        "unresolved": [row["name"] for row in unresolved],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
