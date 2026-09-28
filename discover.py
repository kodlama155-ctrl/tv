#!/usr/bin/env python3
from __future__ import annotations

import base64
import datetime as dt
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
QUERIES = ROOT / "discover_queries.txt"
OUTPUT = ROOT / "discovered.m3u"
STATS = ROOT / "discovery_stats.json"

API = "https://api.github.com"
TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
UA = "EmirTV-M3U-Discovery/1.0"
MAX_REPOS_PER_QUERY = 8
MAX_FILES_PER_REPO = 12
MAX_FILE_BYTES = 2_000_000

PLAYLIST_EXTS = (".m3u", ".m3u8", ".txt")
PATH_HINTS = ("tr", "turk", "turkey", "turkiye", "türkiye", "iptv", "playlist", "channel")
M3U8_RE = re.compile(r'https?://[^\s"\'<>]+?\.m3u8(?:\?[^\s"\'<>]*)?', re.I)

def request_json(url: str):
    headers = {
        "User-Agent": UA,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))

def fetch_text(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = r.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        return None
    return data.decode("utf-8", errors="replace")

def likely_playlist_path(path: str) -> bool:
    low = path.lower()
    if not low.endswith(PLAYLIST_EXTS):
        return False
    return any(h in low for h in PATH_HINTS)

def extract_entries(text: str):
    entries = []
    last_meta = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            last_meta = line
            continue
        if line.startswith("#"):
            continue
        m = M3U8_RE.search(line)
        if m:
            url = m.group(0).rstrip("),;")
            meta = last_meta or "#EXTINF:-1 group-title=\"Discovered\",Discovered stream"
            entries.append((meta, url))
            last_meta = None

    # Some files embed URLs in JSON/markdown instead of normal M3U lines.
    if not entries:
        for url in M3U8_RE.findall(text):
            entries.append(("#EXTINF:-1 group-title=\"Discovered\",Discovered stream", url.rstrip("),;")))
    return entries

def canonical(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path, p.query, ""))

def main():
    queries = [
        q.strip() for q in QUERIES.read_text(encoding="utf-8").splitlines()
        if q.strip() and not q.lstrip().startswith("#")
    ]

    repo_seen = set()
    file_seen = set()
    found = {}
    query_report = []
    repo_count = 0
    file_count = 0
    errors = 0

    for query in queries:
        try:
            q = urllib.parse.urlencode({
                "q": query,
                "sort": "updated",
                "order": "desc",
                "per_page": str(MAX_REPOS_PER_QUERY),
            })
            data = request_json(f"{API}/search/repositories?{q}")
            items = data.get("items", [])
            query_report.append({"query": query, "repos": len(items)})

            for item in items:
                full = item.get("full_name")
                branch = item.get("default_branch") or "main"
                if not full or full in repo_seen:
                    continue
                repo_seen.add(full)
                repo_count += 1

                try:
                    tree_url = f"{API}/repos/{full}/git/trees/{urllib.parse.quote(branch, safe='')}?recursive=1"
                    tree = request_json(tree_url)
                except Exception:
                    errors += 1
                    continue

                paths = [
                    n.get("path", "") for n in tree.get("tree", [])
                    if n.get("type") == "blob" and likely_playlist_path(n.get("path", ""))
                ]
                paths = sorted(paths, key=lambda p: (
                    0 if any(h in p.lower() for h in ("tr.", "tr_", "turk", "turkey", "turkiye")) else 1,
                    len(p),
                ))[:MAX_FILES_PER_REPO]

                for path in paths:
                    key = (full, path)
                    if key in file_seen:
                        continue
                    file_seen.add(key)
                    file_count += 1
                    raw_url = f"https://raw.githubusercontent.com/{full}/{urllib.parse.quote(branch, safe='')}/{urllib.parse.quote(path)}"
                    try:
                        text = fetch_text(raw_url)
                        if not text:
                            continue
                        for meta, url in extract_entries(text):
                            found.setdefault(canonical(url), (meta, url, full, path))
                    except Exception:
                        errors += 1

        except Exception as e:
            errors += 1
            query_report.append({"query": query, "error": type(e).__name__})

    lines = ["#EXTM3U"]
    for _, (meta, url, full, path) in sorted(found.items(), key=lambda kv: kv[0]):
        # Keep original EXTINF when available; add discovery provenance as a comment.
        lines.append(f"# SOURCE: github:{full}/{path}")
        lines.append(meta)
        lines.append(url)

    OUTPUT.write_text("\n".join(lines) + "\n", encoding="utf-8")

    stats = {
        "updated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "queries": query_report,
        "repos_scanned": repo_count,
        "files_scanned": file_count,
        "unique_m3u8_candidates": len(found),
        "errors": errors,
    }
    STATS.write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(stats, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
