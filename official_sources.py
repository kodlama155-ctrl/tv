#!/usr/bin/env python3
from __future__ import annotations

import concurrent.futures
import datetime as dt
import html
import json
import re
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

from hls_validator import validate_hls

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "official_discovered.m3u"
STATS = ROOT / "official_stats.json"

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

HTTP_TIMEOUT = 6
MAX_PAGE_BYTES = 3_000_000
MAX_AUX_RESOURCES = 2
MAX_CANDIDATES_PER_SOURCE = 5
SOURCE_WORKERS = 12
PROBE_WORKERS = 12

# Only public stream URLs are retained. URLs that appear to carry credentials
# or session/auth tokens are intentionally ignored.
SENSITIVE_QUERY_KEYS = {
    "token", "auth", "authorization", "password", "passwd", "username",
    "user", "key", "sig", "signature", "jwt", "session", "hdnts", "hdnea",
}
EPHEMERAL_MEDIA_HOSTS = (
    "googlevideo.com",
)
UNTRUSTED_STREAM_HOSTS = (
    "canlitv.fun",
)
EPHEMERAL_QUERY_KEYS = {
    "st", "e", "hash", "expire", "expires",
}
CREDENTIAL_PATH_RE = re.compile(
    r"/(?:iptv|live)/[A-Za-z0-9_-]{6,}/[A-Za-z0-9_-]{6,}/",
    re.I,
)
SUSPICIOUS_IPTV_PATH_RE = re.compile(
    r"/iptv/[A-Za-z0-9_-]{8,}/\\d{2,}/",
    re.I,
)

M3U8_RE = re.compile(
    r'https?://[^\s"\'<>]+?\.m3u8(?:\?[^\s"\'<>]*)?',
    re.I,
)
SCRIPT_RE = re.compile(
    r'<script[^>]+src=["\']([^"\']+)["\']',
    re.I,
)
IFRAME_RE = re.compile(
    r'<iframe[^>]+src=["\']([^"\']+)["\']',
    re.I,
)

OFFICIAL_SOURCES = [
    {
        "name": "TRT Haber",
        "category": "Haber",
        "page": "https://www.trthaber.com/canli-yayin-izle.html",
        "hints": ["trthaber", "trt-haber", "haber"],
    },
    {
        "name": "TRT Spor",
        "category": "Spor",
        "page": "https://www.trtspor.com.tr/canli-yayin-izle/trt-spor",
        "hints": ["trtspor", "trt-spor"],
        "exclude_hints": ["yildiz", "radyo"],
    },
    {
        "name": "FB TV",
        "category": "Spor",
        "page": "https://www.fenerbahce.org/fenerbahcetv/canliyayin",
        "hints": ["fbtv", "fenerbahce", "fenerbahcetv"],
    },
    {
        "name": "ATV",
        "category": "Ulusal",
        "page": "https://www.atv.com.tr/canli-yayin",
        "hints": ["atv"],
    },
    {
        "name": "ATV Avrupa",
        "category": "Uluslararası",
        "page": "https://www.atvavrupa.tv/webtv/canli-yayin",
        "hints": ["atvavrupa", "atv-avrupa"],
    },
    {
        "name": "A2",
        "category": "Ulusal",
        "page": "https://www.atv.com.tr/a2tv/canli-yayin",
        "hints": ["a2tv", "a2"],
    },
    {
        "name": "A Haber",
        "category": "Haber",
        "page": "https://www.ahaber.com.tr/video/canli-yayin",
        "hints": ["ahaber", "a-haber"],
    },
    {
        "name": "A Spor",
        "category": "Spor",
        "page": "https://www.aspor.com.tr/webtv/canli-yayin",
        "hints": ["aspor", "a-spor"],
    },
    {
        "name": "A Para",
        "category": "Haber",
        "page": "https://www.apara.com.tr/canli-yayin",
        "hints": ["apara", "a-para"],
    },
    {
        "name": "A News",
        "category": "Uluslararası",
        "page": "https://www.anews.com.tr/webtv/live-broadcast",
        "hints": ["anews", "a-news"],
    },
    {
        "name": "Vav TV",
        "category": "Dini",
        "page": "https://www.vavtv.com.tr/canli-yayin",
        "hints": ["vavtv", "vav"],
    },
    {
        "name": "Kanal D",
        "category": "Ulusal",
        "page": "https://www.kanald.com.tr/canli-yayin",
        "hints": ["kanald", "kanal-d"],
    },
    {
        "name": "Show TV",
        "category": "Ulusal",
        "page": "https://www.showtv.com.tr/canli-yayin",
        "hints": ["showtv", "show-tv"],
    },
    {
        "name": "Star TV",
        "category": "Ulusal",
        "page": "https://www.startv.com.tr/canli-yayin",
        "hints": ["startv", "star-tv"],
    },
    {
        "name": "NOW TV",
        "category": "Ulusal",
        "page": "https://www.nowtv.com.tr/canli-yayin",
        "hints": ["nowtv", "now-tv"],
    },
    {
        "name": "TV8",
        "category": "Ulusal",
        "page": "https://www.tv8.com.tr/canli-yayin",
        "hints": ["tv8"],
    },
    {
        "name": "TV8.5",
        "category": "Ulusal",
        "page": "https://img.tv8bucuk.com/tv8-5-canli-yayin",
        "hints": ["tv8bucuk", "tv85", "tv8-5"],
    },
    {
        "name": "Kanal 7",
        "category": "Ulusal",
        "page": "https://www.kanal7.com/canli-izle",
        "hints": ["kanal7", "kanal-7"],
    },
    {
        "name": "Habertürk",
        "category": "Haber",
        "page": "https://www.haberturk.com/canliyayin",
        "hints": ["haberturk", "haberturk-tv"],
    },
    {
        "name": "TV100",
        "category": "Haber",
        "page": "https://www.tv100.com/canli-yayin",
        "hints": ["tv100"],
    },
    {
        "name": "TVNET",
        "category": "Haber",
        "page": "https://www.tvnet.com.tr/canli-yayin",
        "hints": ["tvnet"],
    },
    {
        "name": "EKOTÜRK",
        "category": "Haber",
        "page": "https://www.ekoturk.com/canli-yayin/",
        "hints": ["ekoturk"],
    },
    {
        "name": "BengüTürk",
        "category": "Haber",
        "page": "https://www.benguturk.com/canli-yayin",
        "hints": ["benguturk", "bengu"],
    },
    {
        "name": "CNN Türk",
        "category": "Haber",
        "page": "https://www.cnnturk.com/canli-yayin",
        "hints": ["cnnturk", "cnn-turk", "cnn_turk"],
    },
    {
        "name": "AKİT TV",
        "category": "Haber",
        "page": "https://www.akittv.com.tr/canli-izle",
        "hints": ["akittv", "akit"],
    },
    {
        "name": "GZT",
        "category": "Haber",
        "page": "https://www.gzt.com/gzttv-canli-yayin",
        "hints": ["gzttv", "gzt"],
    },
    {
        "name": "TELE1",
        "category": "Haber",
        "page": "https://www.tele1.com.tr/canli-yayin",
        "hints": ["tele1"],
    },
    {
        "name": "KRT TV",
        "category": "Haber",
        "page": "https://www.krttv.com.tr/canli-yayin",
        "hints": ["krttv", "krt"],
    },
    {
        "name": "Sözcü TV",
        "category": "Haber",
        "page": "https://www.sozcu.com.tr/sozcu-tv-canli-yayin-wp7630113",
        "hints": ["sozcu", "szctv"],
    },
    {
        "name": "beIN Sports Haber",
        "category": "Spor",
        "page": "https://beinsports.com.tr/canli-yayin",
        "hints": ["beinsports", "bein"],
    },
    {
        "name": "TRT 3 Spor",
        "category": "Spor",
        "page": "https://www.trtspor.com.tr/canli-yayin-izle/trt-3-spor",
        "hints": ["trt3", "trt-3"],
    },
    {
        "name": "Habitat TV",
        "category": "Belgesel",
        "page": "https://www.habitattv.com.tr/home.php",
        "hints": ["habitat", "habibattv"],
    },
    {
        "name": "TLC",
        "category": "Belgesel",
        "page": "https://www.tlctv.com.tr/canli-izle",
        "hints": ["tlctv", "tlc"],
    },
    {
        "name": "DMAX",
        "category": "Belgesel",
        "page": "https://www.dmax.com.tr/canli-izle",
        "hints": ["dmax"],
    },
    {
        "name": "TGRT Belgesel",
        "category": "Belgesel",
        "page": "https://www.tgrtbelgesel.com.tr/canli-yayin",
        "hints": ["tgrtbelgesel", "tgrt-belgesel"],
    },
    {
        "name": "Yaban TV",
        "category": "Belgesel",
        "page": "https://www.yabantv.com/broadcast/",
        "hints": ["yabantv", "yaban"],
    },
    {
        "name": "Dream TV",
        "category": "Müzik",
        "page": "https://www.dreamtv.com.tr/",
        "hints": ["dreamtv", "dream"],
    },
    {
        "name": "Meltem TV",
        "category": "Ulusal",
        "page": "https://www.meltemtv.com.tr/canli-yayin",
        "hints": ["meltemtv", "meltem"],
    },
]


def fetch_text(url: str, referer: str | None = None):
    headers = {
        "User-Agent": BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml,application/javascript,"
                  "text/javascript,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.7,en;q=0.6",
        "Cache-Control": "no-cache",
    }
    if referer:
        headers["Referer"] = referer

    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as response:
        raw = response.read(MAX_PAGE_BYTES + 1)
        final_url = response.geturl()

    if len(raw) > MAX_PAGE_BYTES:
        return None, final_url

    return raw.decode("utf-8", errors="replace"), final_url


def normalize_embedded_text(text: str) -> str:
    value = html.unescape(text)
    value = value.replace("\\/", "/")
    value = value.replace("\\u002F", "/").replace("\\u002f", "/")
    value = value.replace("\\u003A", ":").replace("\\u003a", ":")
    value = value.replace("\\u0026", "&")
    value = value.replace("\\u003D", "=").replace("\\u003d", "=")
    try:
        decoded = urllib.parse.unquote(value)
        if decoded != value:
            value += "\n" + decoded
    except Exception:
        pass
    return value


def safe_candidate(url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
    except Exception:
        return False

    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    if parsed.username or parsed.password:
        return False
    host = (parsed.hostname or "").lower()
    if any(host == suffix or host.endswith("." + suffix) for suffix in EPHEMERAL_MEDIA_HOSTS):
        return False
    if any(host == suffix or host.endswith("." + suffix) for suffix in UNTRUSTED_STREAM_HOSTS):
        return False
    if CREDENTIAL_PATH_RE.search(parsed.path):
        return False
    if SUSPICIOUS_IPTV_PATH_RE.search(parsed.path):
        return False

    query_keys = {
        key.lower()
        for key, _ in urllib.parse.parse_qsl(
            parsed.query,
            keep_blank_values=True,
        )
    }
    if query_keys & SENSITIVE_QUERY_KEYS:
        return False
    if query_keys & EPHEMERAL_QUERY_KEYS:
        return False

    return True


def canonical(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path,
            parsed.query,
            "",
        )
    )


def same_site(base_url: str, candidate_url: str) -> bool:
    try:
        base_host = (urllib.parse.urlsplit(base_url).hostname or "").lower()
        cand_host = (urllib.parse.urlsplit(candidate_url).hostname or "").lower()
    except Exception:
        return False

    base_host = base_host.removeprefix("www.")
    cand_host = cand_host.removeprefix("www.")
    return (
        cand_host == base_host
        or cand_host.endswith("." + base_host)
        or base_host.endswith("." + cand_host)
    )


def extract_m3u8(text: str, base_url: str):
    normalized = normalize_embedded_text(text)
    found = {}

    for match in M3U8_RE.finditer(normalized):
        raw = match.group(0).rstrip("),;]}")
        url = urllib.parse.urljoin(base_url, raw)
        if safe_candidate(url):
            found.setdefault(canonical(url), url)

    return list(found.values())


def extract_aux_urls(text: str, base_url: str):
    normalized = normalize_embedded_text(text)
    urls = []

    for regex in (IFRAME_RE, SCRIPT_RE):
        for match in regex.finditer(normalized):
            url = urllib.parse.urljoin(base_url, match.group(1))
            if not url.startswith(("http://", "https://")):
                continue
            if not same_site(base_url, url):
                continue
            if url not in urls:
                urls.append(url)

    return urls[:MAX_AUX_RESOURCES]


def candidate_matches_source(url: str, source: dict) -> bool:
    low = url.lower()
    hints = [str(x).lower() for x in source.get("hints", [])]
    excludes = [str(x).lower() for x in source.get("exclude_hints", [])]

    if any(token in low for token in excludes):
        return False
    if hints and not any(token in low for token in hints):
        return False
    return True


def candidate_score(url: str, hints: list[str]) -> int:
    low = url.lower()
    score = 0

    if "live" in low or "canli" in low:
        score += 40
    if "master" in low:
        score += 20
    if "playlist" in low or "index" in low:
        score += 10
    if any(hint.lower() in low for hint in hints):
        score += 35

    bad = (
        "vod", "archive", "arsiv", "clip", "trailer", "fragman",
        "preview", "preroll", "advert", "/ads/", "promo",
    )
    if any(token in low for token in bad):
        score -= 100

    return score


def scan_source(source: dict):
    page = source["page"]
    result = {
        "name": source["name"],
        "page": page,
        "status": "ok",
        "page_final_url": None,
        "page_candidates": 0,
        "aux_scanned": 0,
        "candidates": [],
    }

    try:
        text, final_page = fetch_text(page)
    except Exception as exc:
        result["status"] = "page_error"
        result["error"] = type(exc).__name__
        return result

    if not text:
        result["status"] = "page_too_large"
        return result

    result["page_final_url"] = final_page
    found = {}

    for url in extract_m3u8(text, final_page):
        found.setdefault(canonical(url), url)

    result["page_candidates"] = len(found)

    for aux_url in extract_aux_urls(text, final_page):
        try:
            aux_text, aux_final = fetch_text(aux_url, referer=final_page)
            if not aux_text:
                continue
            result["aux_scanned"] += 1
            for url in extract_m3u8(aux_text, aux_final):
                found.setdefault(canonical(url), url)
        except Exception:
            continue

    identity_matched = [
        url
        for url in found.values()
        if candidate_matches_source(url, source)
    ]
    result["identity_rejected_candidates"] = (
        len(found) - len(identity_matched)
    )

    ordered = sorted(
        identity_matched,
        key=lambda url: (
            candidate_score(url, source.get("hints", [])),
            1 if url.startswith("https://") else 0,
        ),
        reverse=True,
    )[:MAX_CANDIDATES_PER_SOURCE]

    result["candidates"] = ordered
    return result


def probe_candidate(source: dict, url: str):
    try:
        validation = validate_hls(
            url,
            user_agent=BROWSER_UA,
            referrer=source["page"],
        )
    except Exception as exc:
        validation = {
            "status": "unknown",
            "reason": type(exc).__name__,
        }

    return {
        "source": source,
        "url": url,
        "validation": validation,
    }


def main():
    now = dt.datetime.now(dt.timezone.utc)

    scan_results = []
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=SOURCE_WORKERS
    ) as executor:
        futures = {
            executor.submit(scan_source, source): source
            for source in OFFICIAL_SOURCES
        }
        for future in concurrent.futures.as_completed(futures):
            source = futures[future]
            try:
                scan_results.append(future.result())
            except Exception as exc:
                scan_results.append({
                    "name": source["name"],
                    "page": source["page"],
                    "status": "scan_error",
                    "error": type(exc).__name__,
                    "candidates": [],
                })

    source_by_name = {
        source["name"]: source
        for source in OFFICIAL_SOURCES
    }

    probe_jobs = []
    for result in scan_results:
        source = source_by_name.get(result["name"])
        if not source:
            continue
        for url in result.get("candidates", []):
            probe_jobs.append((source, url))

    probed = []
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=PROBE_WORKERS
    ) as executor:
        futures = {
            executor.submit(probe_candidate, source, url): (source, url)
            for source, url in probe_jobs
        }
        for future in concurrent.futures.as_completed(futures):
            source, url = futures[future]
            try:
                probed.append(future.result())
            except Exception as exc:
                probed.append({
                    "source": source,
                    "url": url,
                    "validation": {
                        "status": "unknown",
                        "reason": type(exc).__name__,
                    },
                })

    accepted = []
    probe_statuses = Counter()

    for item in probed:
        validation = item["validation"]
        status = validation.get("status", "unknown")
        probe_statuses[status] += 1

        # A confirmed VOD item found in a live page is not a channel stream.
        if status == "verified" and validation.get("vod"):
            continue

        # Confirmed dead/DRM candidates are not useful as live-channel inputs.
        if status in {"dead", "drm"}:
            continue

        accepted.append(item)

    # One URL may appear on more than one official page. Keep the first
    # official identity but avoid duplicate network candidates.
    unique = {}
    for item in accepted:
        unique.setdefault(canonical(item["url"]), item)

    lines = ["#EXTM3U"]
    for item in sorted(
        unique.values(),
        key=lambda row: (
            row["source"]["category"],
            row["source"]["name"],
            row["url"],
        ),
    ):
        source = item["source"]
        validation = item["validation"]
        reason = validation.get("reason", "").replace("\n", " ")

        lines.append(
            f'# SOURCE: official:{source["page"]}'
        )
        lines.append(
            f'# PROBE: {validation.get("status", "unknown")} {reason}'.rstrip()
        )
        lines.append(
            f'#EXTINF:-1 group-title="{source["category"]}",{source["name"]}'
        )
        lines.append(
            f'#EXTVLCOPT:http-referrer={source["page"]}'
        )
        lines.append(
            f'#EXTVLCOPT:http-user-agent={BROWSER_UA}'
        )
        lines.append(item["url"])

    OUTPUT.write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )

    scan_results.sort(key=lambda row: row["name"])

    stats = {
        "updated_at_utc": now.isoformat(),
        "official_pages": len(OFFICIAL_SOURCES),
        "pages_ok": sum(
            1 for row in scan_results if row.get("status") == "ok"
        ),
        "pages_with_candidates": sum(
            1 for row in scan_results if row.get("candidates")
        ),
        "raw_candidates": sum(
            len(row.get("candidates", []))
            for row in scan_results
        ),
        "accepted_unique_candidates": len(unique),
        "probe_statuses": dict(probe_statuses),
        "sources": scan_results,
    }

    STATS.write_text(
        json.dumps(stats, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
