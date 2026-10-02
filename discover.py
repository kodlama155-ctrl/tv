#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import json
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path

from channel_catalog import category_for_channel

ROOT = Path(__file__).resolve().parent
QUERIES = ROOT / "discover_queries.txt"
OUTPUT = ROOT / "discovered.m3u"
STATS = ROOT / "discovery_stats.json"
REVIEW = ROOT / "review_candidates.json"

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

SENSITIVE_QUERY_KEYS: set[str] = set()
EXPLICIT_CREDENTIAL_PATH_RE = re.compile(
    r"/(?:user(?:name)?|pass(?:word)?|token|auth(?:orization)?|session|jwt|key)(?:=|/)[^/?#]+",
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
    if EXPLICIT_CREDENTIAL_PATH_RE.search(p.path):
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


def _clean_option_value(value: str | None):
    if value is None:
        return None
    value = value.strip()
    if not value or "\r" in value or "\n" in value:
        return None
    return value


def extract_entries(
    text: str,
    default_turkish: bool = False,
    path_turkish: bool = False,
):
    entries = []
    last_meta = None
    options = {}
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
            options = {}
            continue

        if line.startswith("#EXTVLCOPT:"):
            payload = line.split(":", 1)[1]
            if "=" not in payload:
                continue

            key, value = payload.split("=", 1)
            key = key.strip().lower()
            value = _clean_option_value(value)

            if not value:
                continue
            if key == "http-user-agent":
                options["user_agent"] = value
            elif key in {"http-referrer", "http-referer"}:
                options["referrer"] = value
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
            options = {}
            continue

        match = M3U8_RE.search(line)
        if not match:
            continue

        url = match.group(0).rstrip("),;")

        if not safe_candidate(url):
            last_meta = None
            options = {}
            continue

        meta = (
            last_meta
            or '#EXTINF:-1 group-title="Discovered",Discovered stream'
        )
        entries.append((
            meta,
            url,
            options.get("user_agent"),
            options.get("referrer"),
        ))
        last_meta = None
        options = {}

    return entries

def _meta_attr(meta: str, name: str) -> str:
    m = re.search(rf'{re.escape(name)}="([^"]*)"', meta, flags=re.I)
    return m.group(1).strip() if m else ""


def _meta_label(meta: str) -> str:
    quoted = False
    for i, ch in enumerate(meta):
        if ch == '"':
            quoted = not quoted
        elif ch == "," and not quoted:
            return meta[i + 1:].strip()
    return ""


def _fold(text: str) -> str:
    value = str(text or "").replace("ı", "i").replace("İ", "I").lower()
    return re.sub(r"[^a-z0-9çğıöşü]+", " ", value).strip()


def discovery_decision(meta: str, url: str) -> tuple[str, str]:
    name = _meta_label(meta)
    tvg = _meta_attr(meta, "tvg-id")
    low = _fold(name)

    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except Exception:
        host = ""

    if host.endswith("prosto.tv") or host.endswith("europlayiptv.de"):
        return "reject", "noisy-provider"

    if re.search(r"(?:^|\s)(?:test|vpn|backup|yedek)(?:$|\s)", low, flags=re.I):
        return "reject", "test-or-backup"

    if re.search(r"[\u0400-\u04ff]", name):
        return "reject", "non-turkish-script"

    if re.search(r"\b(?:film|movie|polis|smackdown|fight pass)\b", low, flags=re.I):
        return "reject", "vod-or-event-like"

    if category_for_channel(tvg, name):
        return "accept", "catalog"

    # Keep plausible Turkish channels visible for review without spending
    # validation time on them until they are curated.
    if re.search(r"\.tr(?:@|$)", tvg, flags=re.I):
        return "review", "uncatalogued-tr-identity"

    return "reject", "uncatalogued"


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
    review_candidates = {}
    discovery_rejections = {}

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

                        for meta, url, user_agent, referrer in entries:
                            decision, reason = discovery_decision(meta, url)

                            if decision == "reject":
                                discovery_rejections[reason] = (
                                    discovery_rejections.get(reason, 0) + 1
                                )
                                continue

                            key = canonical(url)

                            if decision == "review":
                                review_candidates.setdefault(key, {
                                    "name": _meta_label(meta),
                                    "tvg_id": _meta_attr(meta, "tvg-id"),
                                    "url": url,
                                    "source_repo": full,
                                    "source_path": path,
                                    "source_updated": commit["date_raw"],
                                    "reason": reason,
                                })
                                continue

                            existing = found.get(key)

                            if existing is None:
                                found[key] = (
                                    meta,
                                    url,
                                    full,
                                    path,
                                    commit["date_raw"],
                                    user_agent,
                                    referrer,
                                )
                            else:
                                # İlk metadata/kaynak kaydı kalsın; aynı URL'nin
                                # sonraki kopyasında eksik HTTP seçenekleri varsa
                                # yalnız onları tamamla.
                                (
                                    old_meta,
                                    old_url,
                                    old_full,
                                    old_path,
                                    old_commit,
                                    old_ua,
                                    old_referrer,
                                ) = existing
                                found[key] = (
                                    old_meta,
                                    old_url,
                                    old_full,
                                    old_path,
                                    old_commit,
                                    old_ua or user_agent,
                                    old_referrer or referrer,
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
        user_agent,
        referrer,
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
        if referrer:
            lines.append(
                f"#EXTVLCOPT:http-referrer={referrer}"
            )
        if user_agent:
            lines.append(
                f"#EXTVLCOPT:http-user-agent={user_agent}"
            )
        lines.append(url)

    OUTPUT.write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )

    fresh_source_files.sort(
        key=lambda x: x["last_commit"],
        reverse=True,
    )

    REVIEW.write_text(
        json.dumps(
            {
                "updated_at_utc": now.isoformat(),
                "count": len(review_candidates),
                "candidates": sorted(
                    review_candidates.values(),
                    key=lambda row: (
                        str(row.get("name") or "").lower(),
                        str(row.get("url") or ""),
                    ),
                ),
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
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
        "review_candidates": len(review_candidates),
        "discovery_rejections": discovery_rejections,
        "header_aware_candidates": sum(
            1
            for value in found.values()
            if value[5] or value[6]
        ),
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
