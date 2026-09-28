#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import json
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
QUERIES = ROOT / "discover_queries.txt"
OUTPUT = ROOT / "discovered.m3u"
STATS = ROOT / "discovery_stats.json"

API = "https://api.github.com"
TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
UA = "EmirTV-M3U-Discovery/2.0"

MAX_REPOS_PER_QUERY = 50
MAX_FILES_PER_REPO = 12
MAX_FILE_BYTES = 2_000_000
MAX_FILE_AGE_DAYS = 14

PLAYLIST_EXTS = (".m3u", ".m3u8", ".txt")
PATH_HINTS = (
    "tr", "turk", "turkey", "turkiye", "türkiye",
    "iptv", "playlist", "channel", "tv",
)

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
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(
            r.read().decode("utf-8", errors="replace")
        )


def fetch_text(url: str):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "*/*"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        data = r.read(MAX_FILE_BYTES + 1)

    if len(data) > MAX_FILE_BYTES:
        return None

    return data.decode("utf-8", errors="replace")


def parse_github_time(value: str | None):
    if not value:
        return None

    try:
        return dt.datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )
    except ValueError:
        return None


def likely_playlist_path(
    path: str,
    repo_has_turkish_hint: bool,
) -> bool:
    low = path.lower()

    if low.endswith((".m3u", ".m3u8")):
        # TV-tr.m3u / turkiye.m3u gibi açıkça Türkiye dosyaları her zaman uygun.
        if turkey_priority(path) == 0:
            return True

        # combined.m3u gibi genel isim ancak repo Türkçe/Türkiye odaklıysa incelenir.
        return repo_has_turkish_hint

    if low.endswith(".txt"):
        return (
            turkey_priority(path) == 0
            or (
                repo_has_turkish_hint
                and any(h in low for h in PATH_HINTS)
            )
        )

    return False


def turkey_priority(path: str):
    low = path.lower()

    strong = (
        "tr.m3u",
        "tr.m3u8",
        "tr-",
        "tr_",
        "turk",
        "turkey",
        "turkiye",
        "türkiye",
    )

    return 0 if any(h in low for h in strong) else 1


def repository_turkish_hints(item: dict):
    text = " ".join([
        str(item.get("full_name") or ""),
        str(item.get("name") or ""),
        str(item.get("description") or ""),
    ]).lower()

    positive = (
        "turkish",
        "turkey",
        "turkiye",
        "türkiye",
        "türk",
        "iptvtr",
        "iptv-tr",
        "tr iptv",
    )
    mixed = (
        "russian",
        "azerbaijan",
        "azerbaijani",
        "multi-country",
        "multicountry",
        "6 ulke",
        "6 ülke",
        "6 countries",
    )

    has_hint = any(token in text for token in positive)
    safe_default = has_hint and not any(
        token in text for token in mixed
    )

    return has_hint, safe_default


def safe_candidate(url: str) -> bool:
    try:
        p = urllib.parse.urlsplit(url)
    except Exception:
        return False

    if p.scheme not in ("http", "https") or not p.hostname:
        return False

    if p.username or p.password:
        return False

    if CREDENTIAL_PATH_RE.search(p.path):
        return False

    query_keys = {
        k.lower()
        for k, _ in urllib.parse.parse_qsl(
            p.query,
            keep_blank_values=True,
        )
    }

    if query_keys & SENSITIVE_QUERY_KEYS:
        return False

    return True


def extract_entries(
    text: str,
    default_turkish: bool = False,
    path_turkish: bool = False,
):
    entries = []
    last_meta = None
    active_turkish = bool(
        default_turkish or path_turkish
    )

    turkey_section_tokens = (
        "turkish",
        "turkey",
        "turkiye",
        "türkiye",
        "türk",
        "tr channels",
        "tr kanallar",
    )

    for raw in text.splitlines():
        line = raw.strip()

        if not line:
            continue

        if line.startswith("#EXTINF"):
            last_meta = line
            continue

        if line.startswith("#"):
            low = line.lower()

            # combined.m3u gibi çok ülkeli listelerde yalnız Türkiye bölümünü al.
            if any(
                token in low
                for token in turkey_section_tokens
            ):
                active_turkish = True
            elif (
                "channels" in low
                or "kanallar" in low
                or "канал" in low
            ):
                active_turkish = False

            continue

        if not active_turkish:
            last_meta = None
            continue

        match = M3U8_RE.search(line)
        if not match:
            continue

        url = match.group(0).rstrip("),;")

        if not safe_candidate(url):
            last_meta = None
            continue

        meta = (
            last_meta
            or '#EXTINF:-1 group-title="Discovered",Discovered stream'
        )
        entries.append((meta, url))
        last_meta = None

    return entries

def canonical(url: str) -> str:
    p = urllib.parse.urlsplit(url)

    return urllib.parse.urlunsplit(
        (
            p.scheme.lower(),
            p.netloc.lower(),
            p.path,
            p.query,
            "",
        )
    )


def last_file_commit(repo: str, path: str):
    query = urllib.parse.urlencode({
        "path": path,
        "per_page": "1",
    })

    data = request_json(
        f"{API}/repos/{repo}/commits?{query}"
    )

    if not isinstance(data, list) or not data:
        return None

    commit = data[0]
    date_value = (
        commit.get("commit", {})
        .get("committer", {})
        .get("date")
    )

    when = parse_github_time(date_value)

    return {
        "date": when,
        "date_raw": date_value,
        "sha": commit.get("sha"),
    }


def raw_file_url(repo: str, branch: str, path: str):
    encoded_branch = urllib.parse.quote(
        branch,
        safe="",
    )
    encoded_path = urllib.parse.quote(
        path,
        safe="/",
    )

    return (
        f"https://raw.githubusercontent.com/"
        f"{repo}/{encoded_branch}/{encoded_path}"
    )


def main():
    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=MAX_FILE_AGE_DAYS)
    cutoff_date = cutoff.date().isoformat()

    queries = [
        q.strip()
        for q in QUERIES.read_text(
            encoding="utf-8"
        ).splitlines()
        if q.strip()
        and not q.lstrip().startswith("#")
    ]

    repo_seen = set()
    file_seen = set()
    found = {}

    query_report = []
    repo_count = 0
    file_candidates = 0
    fresh_files_scanned = 0
    stale_files_skipped = 0
    stale_repos_skipped = 0
    errors = 0

    fresh_source_files = []

    for base_query in queries:
        try:
            search_query = (
                f"{base_query} pushed:>={cutoff_date}"
            )

            encoded = urllib.parse.urlencode({
                "q": search_query,
                "sort": "updated",
                "order": "desc",
                "per_page": str(
                    MAX_REPOS_PER_QUERY
                ),
            })

            data = request_json(
                f"{API}/search/repositories?{encoded}"
            )

            items = data.get("items", [])

            query_report.append({
                "query": base_query,
                "github_query": search_query,
                "repos_returned": len(items),
            })

            for item in items:
                full = item.get("full_name")
                branch = (
                    item.get("default_branch")
                    or "main"
                )

                if not full or full in repo_seen:
                    continue

                repo_seen.add(full)

                pushed_at = parse_github_time(
                    item.get("pushed_at")
                )

                if (
                    pushed_at is not None
                    and pushed_at < cutoff
                ):
                    stale_repos_skipped += 1
                    continue

                repo_count += 1

                try:
                    encoded_branch = urllib.parse.quote(
                        branch,
                        safe="",
                    )

                    tree_url = (
                        f"{API}/repos/{full}/git/trees/"
                        f"{encoded_branch}?recursive=1"
                    )

                    tree = request_json(tree_url)

                except Exception:
                    errors += 1
                    continue

                repo_has_hint, repo_default_turkish = (
                    repository_turkish_hints(item)
                )

                paths = [
                    node.get("path", "")
                    for node in tree.get(
                        "tree",
                        [],
                    )
                    if node.get("type") == "blob"
                    and likely_playlist_path(
                        node.get("path", ""),
                        repo_has_hint,
                    )
                ]

                paths = sorted(
                    paths,
                    key=lambda p: (
                        turkey_priority(p),
                        len(p),
                    ),
                )[:MAX_FILES_PER_REPO]

                for path in paths:
                    key = (full, path)

                    if key in file_seen:
                        continue

                    file_seen.add(key)
                    file_candidates += 1

                    try:
                        commit = last_file_commit(
                            full,
                            path,
                        )

                        if (
                            not commit
                            or not commit["date"]
                        ):
                            errors += 1
                            continue

                        if commit["date"] < cutoff:
                            stale_files_skipped += 1
                            continue

                        fresh_files_scanned += 1

                        raw_url = raw_file_url(
                            full,
                            branch,
                            path,
                        )

                        text = fetch_text(raw_url)

                        if not text:
                            continue

                        entries = extract_entries(
                            text,
                            default_turkish=repo_default_turkish,
                            path_turkish=(
                                turkey_priority(path) == 0
                            ),
                        )

                        fresh_source_files.append({
                            "repo": full,
                            "path": path,
                            "last_commit": (
                                commit["date_raw"]
                            ),
                            "entries_found": len(
                                entries
                            ),
                        })

                        for meta, url in entries:
                            found.setdefault(
                                canonical(url),
                                (
                                    meta,
                                    url,
                                    full,
                                    path,
                                    commit[
                                        "date_raw"
                                    ],
                                ),
                            )

                    except Exception:
                        errors += 1

        except Exception as e:
            errors += 1
            query_report.append({
                "query": base_query,
                "error": type(e).__name__,
            })

    lines = ["#EXTM3U"]

    for _, (
        meta,
        url,
        full,
        path,
        last_commit,
    ) in sorted(
        found.items(),
        key=lambda kv: kv[0],
    ):
        lines.append(
            f"# SOURCE: github:{full}/{path}"
        )
        lines.append(
            f"# SOURCE-UPDATED: {last_commit}"
        )
        lines.append(meta)
        lines.append(url)

    OUTPUT.write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )

    fresh_source_files.sort(
        key=lambda x: x["last_commit"],
        reverse=True,
    )

    stats = {
        "updated_at_utc": now.isoformat(),
        "max_file_age_days": MAX_FILE_AGE_DAYS,
        "cutoff_utc": cutoff.isoformat(),
        "repos_per_query": MAX_REPOS_PER_QUERY,
        "queries": query_report,
        "repos_scanned": repo_count,
        "stale_repos_skipped": stale_repos_skipped,
        "playlist_file_candidates": file_candidates,
        "fresh_files_scanned": fresh_files_scanned,
        "stale_files_skipped": stale_files_skipped,
        "unique_m3u8_candidates": len(found),
        "fresh_source_files": fresh_source_files,
        "errors": errors,
    }

    STATS.write_text(
        json.dumps(
            stats,
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )

    print(
        json.dumps(
            stats,
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
