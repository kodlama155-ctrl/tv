#!/usr/bin/env python3
from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SOURCES = ROOT / "sources.txt"
DISCOVERED = ROOT / "discovered.m3u"
OUTPUT = ROOT / "a.m3u"
STATS = ROOT / "stats.json"

UA = "Mozilla/5.0 (EmirTV-M3U-Bot/1.0)"
PLAYLIST_TIMEOUT = 20
STREAM_TIMEOUT = 8
MAX_WORKERS = 24

def get(url: str, timeout: int, headers: dict | None = None):
    req_headers = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    return urllib.request.urlopen(req, timeout=timeout)

def fetch_text(url: str) -> str:
    with get(url, PLAYLIST_TIMEOUT) as r:
        raw = r.read(8_000_000)
    return raw.decode("utf-8", errors="replace")

def parse_playlist(text: str):
    lines = [x.strip() for x in text.splitlines()]
    out = []
    meta = None
    for line in lines:
        if not line:
            continue
        if line.startswith("#EXTINF"):
            meta = line
            continue
        if line.startswith("#"):
            continue
        if line.startswith(("http://", "https://")):
            out.append((meta or "#EXTINF:-1,Unknown", line))
            meta = None
    return out

def canonical(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path, p.query, ""))

def probe(url: str):
    req = urllib.request.Request(
        url,
        method="GET",
        headers={
            "User-Agent": UA,
            "Accept": "*/*",
            "Range": "bytes=0-2047",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=STREAM_TIMEOUT) as r:
            code = getattr(r, "status", 200)
            sample = r.read(2048)
            ctype = (r.headers.get("Content-Type") or "").lower()
        ok = 200 <= code < 400
        if ok and (b"#EXTM3U" in sample or "mpegurl" in ctype or "video" in ctype or "octet-stream" in ctype):
            return "ok", code
        return ("ok" if ok else "dead"), code
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 429, 451):
            return "restricted", e.code
        if e.code in (404, 410):
            return "dead", e.code
        return "unknown", e.code
    except (urllib.error.URLError, TimeoutError, socket.timeout):
        return "unknown", None
    except Exception:
        return "unknown", None

def main():
    source_urls = [
        x.strip() for x in SOURCES.read_text(encoding="utf-8").splitlines()
        if x.strip() and not x.lstrip().startswith("#")
    ]

    entries = []
    source_report = []
    for src in source_urls:
        try:
            text = fetch_text(src)
            parsed = parse_playlist(text)
            entries.extend(parsed)
            source_report.append({"url": src, "status": "ok", "entries": len(parsed)})
        except Exception as e:
            source_report.append({"url": src, "status": "error", "error": type(e).__name__})

    discovered_entries = 0
    if DISCOVERED.exists():
        try:
            parsed = parse_playlist(DISCOVERED.read_text(encoding="utf-8"))
            discovered_entries = len(parsed)
            entries.extend(parsed)
            source_report.append({"url": "local:discovered.m3u", "status": "ok", "entries": discovered_entries})
        except Exception as e:
            source_report.append({"url": "local:discovered.m3u", "status": "error", "error": type(e).__name__})

    unique = {}
    for meta, url in entries:
        unique.setdefault(canonical(url), (meta, url))
    candidates = list(unique.values())

    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        future_map = {ex.submit(probe, url): url for _, url in candidates}
        for fut in concurrent.futures.as_completed(future_map):
            url = future_map[fut]
            try:
                results[canonical(url)] = fut.result()
            except Exception:
                results[canonical(url)] = ("unknown", None)

    kept = []
    counts = {"ok": 0, "restricted": 0, "unknown": 0, "dead": 0}
    for meta, url in candidates:
        status, _ = results.get(canonical(url), ("unknown", None))
        counts[status] = counts.get(status, 0) + 1
        # Geçici/ağ kaynaklı belirsizliklerde kanalı hemen silme.
        if status != "dead":
            kept.append((meta, url))

    body = ["#EXTM3U"]
    for meta, url in kept:
        body.extend([meta, url])
    OUTPUT.write_text("\n".join(body) + "\n", encoding="utf-8")

    stats = {
        "updated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "sources": source_report,
        "raw_entries": len(entries),
        "discovered_entries": discovered_entries,
        "unique_entries": len(candidates),
        "kept_entries": len(kept),
        "probe": counts,
    }
    STATS.write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(stats, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
