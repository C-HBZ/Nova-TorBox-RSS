import os
import re
import time
import logging
import json
import unicodedata
import datetime
from urllib.parse import quote
from typing import List, Dict, Set, Tuple, Optional
from concurrent.futures import ThreadPoolExecutor
from curl_cffi import requests

# ==========================================
# CONFIGURATION & TOGGLES
# ==========================================
TEST_RUN = True

TMDB_API_KEY = os.getenv("API_TMDB") or os.getenv("TMDB_API_KEY", "")
TORBOX_API_KEY = os.getenv("API_TORBOX") or os.getenv("TORBOX_API_KEY", "")

TORBOX_BASE_URL = "https://api.torbox.app/v1/api/torrents"
TMDB_BASE_URL = "https://api.themoviedb.org/3"
TORRENTIO_BASE_URL = "https://torrentio.strem.fun/stream"
MANIFEST_PATH = "manifest.txt"
MANIFEST_URLS = [
    "https://tmdb-discover-plus.elfhosted.com/fVJLIjWON0/manifest.json",
    "https://tmdb-discover-plus.elfhosted.com/ZOKAAUsfGq/manifest.json",
    "https://apps.soluserv.es/stremio_catalog_plus/c.4586dbb3765d08170188cb027e4bfce87975a649/manifest.json",
    "https://torrentio.strem.fun/sort=qualitysize%7Cqualityfilter=threed,720p,480p,scr,cam,unknown%7Climit=3%7Cdebridoptions=nocatalog%7Ctorbox=${API_TORBOX}/manifest.json",
    "https://v3-cinemeta.strem.io/manifest.json",
]

MAX_MOVIES = 30
MAX_TV_SERIES = 20
MAX_MEDIA_AGE_MONTHS = 24
MAX_NEW_EPISODES_PER_SHOW = 30
# TMDB genre ids excluded from all TV discovery paths: News, Soap, Talk. Daily soaps in particular can
# air 100-200+ episodes per "season" and would otherwise crowd out scripted shows purely on popularity.
EXCLUDED_TV_GENRE_IDS = {10763, 10766, 10767}
SERIES_INDEX_PATH = "series_index.json"
TORRENTIO_CONCURRENCY = 4
# Calibrated from observed run timing: ~10 movies per 6-10s, ~5 TV shows per ~60s serially (varies with
# how many episodes each show has aired and how many are already owned and skipped).
SECONDS_PER_MOVIE_ESTIMATE = 0.8
SECONDS_PER_TV_SHOW_ESTIMATE = 12.0
# With TORRENTIO_CONCURRENCY parallel workers, observed wall-clock speedup is ~3x (not the full 4x,
# due to per-thread request overhead and uneven per-show workloads) - divide the serial estimate by this.
EFFECTIVE_CONCURRENCY_SPEEDUP = 3.0
JUSTWATCH_NEW_URL = "https://www.justwatch.com/us/new"
MIN_TMDB_POPULARITY = 5.0
MIN_TMDB_VOTE_COUNT = 5
MIN_TMDB_AUDIENCE_POPULARITY = 8.0
ALLOWED_QUALITY_PRIORITY = ("2160P", "1080P")

# Match the Stremio/Torrentio flow in your config: newest-first, English-first, 2160p/1080p only.
BAD_RELEASE_PATTERNS = (
    "CAM", "HDCAM", "CAMRIP", "TS", "TELESYNC", "HDTS", "TELECINE", "HDTC", "TC",
    "HC", "KORSUB", "WORKPRINT", "WP", "PRE", "PREDVD", "PREAIR", "SCR", "DVDSCR",
    "BRSCR", "SCREENER", "R5", "R6", "DUBBED"
)

FOREIGN_ONLY_PATTERNS = (
    "VF2", "VOSTFR", "LEKTOR", "RUSSIAN", "POLISH", "FRENCH", "HINDI", "ARABIC",
    "SPANISH", "LATINO", "KOREAN", "JAPANESE", "ITALIAN", "GERMAN", "TAMIL",
    "TELUGU", "PORTUGUESE", "DUTCH", "HEBREW", "MVO"
)

ENGLISH_HINT_PATTERNS = (
    "ENG", "ENGLISH", "AAC", "AC3", "EAC3", "DTS", "DTS-HD", "TRUEHD", "ATMOS",
    "DDP", "DP", "MULTI", "DUAL", "AUDIO"
)

RESOLUTION_PATTERNS = ("2160P", "1080P", "UHD", "4K")

# Audio ranking tiers used only as a tie-breaker between candidates of the same resolution.
PREMIUM_AUDIO_PATTERNS = ("TRUEHD", "DTS-HD", "DTSHD")
STANDARD_AUDIO_PATTERNS = ("ATMOS", "DDP", "DD+", "EAC3", "AC3", "DTS")
PREMIUM_AUDIO_BONUS = 6.0
STANDARD_AUDIO_BONUS = 3.0

BAD_RELEASE_RE = re.compile(r"\b(?:" + "|".join(re.escape(p) for p in BAD_RELEASE_PATTERNS) + r")\b", re.IGNORECASE)
FOREIGN_ONLY_RE = re.compile(r"\b(?:" + "|".join(re.escape(p) for p in FOREIGN_ONLY_PATTERNS) + r")\b", re.IGNORECASE)
ENGLISH_HINT_RE = re.compile(r"\b(?:" + "|".join(re.escape(p) for p in ENGLISH_HINT_PATTERNS) + r")\b", re.IGNORECASE)
PREMIUM_AUDIO_RE = re.compile(r"\b(?:" + "|".join(re.escape(p) for p in PREMIUM_AUDIO_PATTERNS) + r")\b", re.IGNORECASE)
STANDARD_AUDIO_RE = re.compile(r"\b(?:" + "|".join(re.escape(p) for p in STANDARD_AUDIO_PATTERNS) + r")\b", re.IGNORECASE)
NON_LATIN_PATTERN = re.compile(r"[\u0400-\u04FF\u4E00-\u9FFF\u3040-\u30FF\uAC00-\uD7AF]")

LOG_FILE_PATH = "latest-run.log"
PROGRESS_LOG_EVERY_MOVIES = 10
PROGRESS_LOG_EVERY_SHOWS = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE_PATH, mode="w", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


def expand_secret_tokens(value: str) -> str:
    expanded = value
    for key in ("API_TORBOX", "API_TMDB"):
        secret = os.getenv(key, "")
        if secret:
            expanded = expanded.replace(f"${{{key}}}", secret).replace(f"%24%7B{key}%7D", secret)
    return expanded


def read_manifest_urls(path: str = MANIFEST_PATH) -> List[str]:
    urls: List[str] = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                cleaned = line.strip()
                if cleaned.startswith("http"):
                    urls.append(expand_secret_tokens(cleaned))
    if urls:
        return urls
    return [expand_secret_tokens(url) for url in MANIFEST_URLS]


def get_torrentio_stream_base_url(manifest_urls: List[str]) -> str:
    """Use the personalised sort/qualityfilter/debridoptions/torbox config from manifest.txt's Torrentio manifest URL, instead of the default public endpoint."""
    for url in manifest_urls:
        if "torrentio.strem.fun/" not in url or not url.endswith("manifest.json"):
            continue
        config = url.split("torrentio.strem.fun/", 1)[1][: -len("manifest.json")].rstrip("/")
        if config:
            return f"https://torrentio.strem.fun/{config}/stream"
    return TORRENTIO_BASE_URL


def is_recent_enough(date_value: Optional[str], max_months: int = MAX_MEDIA_AGE_MONTHS) -> bool:
    if not date_value:
        return False
    try:
        date_text = date_value.split("T")[0]
        year, month, day = [int(part) for part in date_text.split("-")[:3]]
        then = datetime.date(year, month, day)
        today = datetime.date.today()
        if then > today:
            return False
        cutoff = today - datetime.timedelta(days=max_months * 30)
        return then >= cutoff
    except Exception:
        return False


def normalize_title_for_match(value: str) -> str:
    if not value:
        return ""
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def is_mainstream_tmdb_candidate(item: dict) -> bool:
    """Keep the discovery pool aimed at newer, mainstream English-market titles without redesigning the flow."""
    if not isinstance(item, dict):
        return False

    if item.get("adult"):
        return False

    if EXCLUDED_TV_GENRE_IDS.intersection(item.get("genre_ids") or []):
        return False

    popularity = float(item.get("popularity") or 0.0)
    vote_count = int(item.get("vote_count") or 0)
    if popularity < MIN_TMDB_POPULARITY:
        return False
    if vote_count and vote_count < MIN_TMDB_VOTE_COUNT and popularity < MIN_TMDB_AUDIENCE_POPULARITY:
        return False
    return True


def fetch_justwatch_new_titles(limit: int = 40) -> List[dict]:
    """Minimal JustWatch 'new' fetch: gather likely new titles from the New page and return just enough metadata for TMDB matching."""
    try:
        resp = requests.get(JUSTWATCH_NEW_URL, headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"}, timeout=25)
        resp.raise_for_status()
    except Exception as exc:
        logger.warning(f"Failed to fetch JustWatch new page: {exc}")
        return []

    matches = re.findall(r'/us/(movie|tv-show)/([^"/?#]+)', resp.text)
    results: List[dict] = []
    seen: Set[str] = set()
    for media_type, slug in matches:
        if not slug:
            continue
        slug = slug.split("/")[0]
        if not slug or slug in seen:
            continue
        seen.add(slug)
        title = slug.replace("-", " ")
        if title:
            results.append({"title": title.strip(), "media_type": "movie" if media_type == "movie" else "tv"})
        if len(results) >= limit:
            break
    return results


def search_tmdb_by_title(session: requests.Session, title: str, media_type: str) -> Optional[dict]:
    query = (title or "").strip()
    if not query:
        return None
    try:
        resp = safe_http_get(session, f"{TMDB_BASE_URL}/search/{media_type}", params={"api_key": TMDB_API_KEY, "query": query, "include_adult": "false", "language": "en-US"})
        if not resp:
            return None
        data = resp.json()
        results = data.get("results", [])
    except Exception:
        return None

    if not results:
        return None

    best_match = None
    best_score = -1.0
    normalized_query = normalize_title_for_match(query)
    for item in results:
        name = item.get("title") or item.get("name") or ""
        if not name:
            continue
        score = 0.0
        normalized_name = normalize_title_for_match(name)
        if normalized_name == normalized_query:
            score = 1.0
        elif normalized_query in normalized_name or normalized_name in normalized_query:
            score = 0.8
        elif normalized_query.split()[0] == normalized_name.split()[0]:
            score = 0.6
        if score > best_score:
            best_score = score
            best_match = item

    if not best_match:
        return None
    if not is_mainstream_tmdb_candidate(best_match):
        return None
    return best_match


def merge_justwatch_new_titles(session: requests.Session, recent_movies: List[dict], recent_shows: List[dict]) -> Tuple[List[dict], List[dict]]:
    """Minimal merge: add JustWatch 'new' titles when TMDB popularity is high enough and the title is not already present."""
    existing_ids = {item.get("id") for item in recent_movies + recent_shows if item.get("id")}
    justwatch_items = fetch_justwatch_new_titles(limit=50)
    if not justwatch_items:
        return recent_movies, recent_shows

    merged_movies = list(recent_movies)
    merged_shows = list(recent_shows)
    seen_titles: Set[str] = set()
    for item in recent_movies + recent_shows:
        title = item.get("title") or item.get("name") or ""
        seen_titles.add(normalize_title_for_match(title))

    for candidate in justwatch_items:
        media_type = candidate.get("media_type", "movie")
        title = candidate.get("title", "")
        norm = normalize_title_for_match(title)
        if not title or norm in seen_titles:
            continue
        match = search_tmdb_by_title(session, title, media_type)
        if not match:
            continue
        if match.get("id") in existing_ids:
            continue
        if media_type == "movie":
            merged_movies.append(match)
        else:
            merged_shows.append(match)
        seen_titles.add(norm)
        existing_ids.add(match.get("id"))

    return merged_movies, merged_shows


def extract_quality_bucket(title: str) -> Optional[str]:
    if not title:
        return None
    upper = title.upper()
    if "2160" in upper or "UHD" in upper or "4K" in upper:
        return "2160P"
    if "1080" in upper:
        return "1080P"
    return None


# Torrentio's debrid-integrated manifest (torbox=<key>) drops "infoHash" and instead exposes a
# "/resolve/torbox/<key>/<hash>/..." url, and signals live cache status via the stream name prefix
# ("[TB+]" = already cached on TorBox, "[TB download]" = not cached yet).
RESOLVE_HASH_RE = re.compile(r"/resolve/torbox/[^/]+/([0-9a-fA-F]{32,40})(?:/|$)")
CACHED_STREAM_RE = re.compile(r"^\[TB\+\]", re.IGNORECASE)


def extract_stream_hash(stream: dict) -> str:
    """Return the lowercase infoHash for a Torrentio stream, from either the classic field or a debrid resolve URL."""
    info_hash = (stream.get("infoHash") or "").strip().lower()
    if info_hash:
        return info_hash
    match = RESOLVE_HASH_RE.search(stream.get("url") or "")
    return match.group(1).lower() if match else ""


def is_stream_cached_hint(stream: dict) -> bool:
    """True when the debrid-integrated manifest already tells us this stream is cached on TorBox."""
    return bool(CACHED_STREAM_RE.match((stream.get("name") or "").strip()))


STANDARD_EPISODE_RE = re.compile(r"(?i)\bs\d{1,2}e\d{1,2}\b")


def get_canonical_episode_text(stream: dict) -> str:
    """Extra correctness measure: TV release/torrent display names are often localized or ambiguous
    season summaries (e.g. a bare "S01" or a translated "Season 1 / Episodes 1-2 of 8" line), but the
    underlying per-file filename Torrentio/TorBox resolves to almost always follows the international
    SxxEyy filename standard. Prefer that filename (or the first line that actually matches SxxEyy) for
    identifying season/episode and series identity, instead of the noisier top-level release name."""
    filename = (stream.get("behaviorHints") or {}).get("filename") or ""
    if filename and STANDARD_EPISODE_RE.search(filename):
        return filename
    for line in (stream.get("title") or "").splitlines():
        if STANDARD_EPISODE_RE.search(line):
            return line
    return filename or (stream.get("title") or "")


def canonical_stream_key(title: str) -> str:
    clean = re.sub(r"\b(?:2160p|1080p|4k|uhd|hdr|hevc|h265|x265|x264|bluray|webdl|webrip|hdtv|remux|av1|aac|ac3|eac3|dts|eng|english|multi|dual|audio|sub|subs|xvid|avi|mkv|mp4)\b", " ", title, flags=re.IGNORECASE)
    clean = re.sub(r"[._\-\[\](){}+/\\]", " ", clean)
    clean = re.sub(r"[^a-zA-Z0-9\s]", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip().lower()
    return clean


def build_torrent_dedupe_key(title: str, year: Optional[str] = None, episode_key: Optional[str] = None) -> str:
    base = canonical_stream_key(title)
    if episode_key:
        return f"ep:{episode_key}:{base}"
    if year:
        return f"movie:{year}:{base}"
    return f"title:{base}"


# ==========================================
# QUALITY & PARSING LOGIC
# ==========================================
def contains_non_latin_text(value: str) -> bool:
    """Reject non-Latin-only titles while allowing mixed-script releases that include a clear English title."""
    if not value:
        return False

    latin_words = bool(re.search(r"[A-Za-z]{3,}", value))
    non_latin_chars = False
    for ch in value:
        if ch.isascii():
            continue
        name = unicodedata.name(ch, "")
        if name.startswith("LATIN"):
            continue
        non_latin_chars = True
        break

    return non_latin_chars and not latin_words


def is_valid_release(release_title: str) -> bool:
    """Mirror the Stremio/Torrentio JSON behavior more closely: allow valid 2160p/1080p releases unless they are obvious junk or explicitly foreign-only without an English title or audio cue."""
    if not release_title:
        return False

    title = release_title.strip()
    if contains_non_latin_text(title):
        return False
    if NON_LATIN_PATTERN.search(title) and not re.search(r"[A-Za-z]{3,}", title):
        return False

    upper = re.sub(r"[._\-\[\]\(\)]", " ", title).upper()

    quality = extract_quality_bucket(title)
    if quality not in ALLOWED_QUALITY_PRIORITY:
        return False

    if BAD_RELEASE_RE.search(upper):
        return False

    # Foreign-only titles should be rejected unless the release also contains an English title or audio cue.
    if FOREIGN_ONLY_RE.search(upper):
        return bool(re.search(r"[A-Za-z]{3,}", title) or ENGLISH_HINT_RE.search(upper))

    # A normal English 2160p/1080p title can be valid without writing "ENG" in the filename.
    return True


def calculate_quality_score(title: str) -> float:
    """Bias 2160p ahead of 1080p; audio codec only breaks ties within the same resolution."""
    quality = extract_quality_bucket(title)
    if quality not in ALLOWED_QUALITY_PRIORITY:
        return 0.0

    upper = title.upper()
    audio_bonus = 0.0
    if PREMIUM_AUDIO_RE.search(upper):
        audio_bonus = PREMIUM_AUDIO_BONUS
    elif STANDARD_AUDIO_RE.search(upper):
        audio_bonus = STANDARD_AUDIO_BONUS

    if quality == "2160P":
        score = 100.0
        if any(hdr in upper for hdr in ["HDR", "DV", "DOVI", "HDR10+"]):
            score += 10.0
        return score + audio_bonus
    return 90.0 + audio_bonus


def extract_episode_info(title: str) -> Optional[Tuple[str, str]]:
    """Return (series_key, episode_id) for compact TV dedupe keys."""
    if not title:
        return None

    t = title.strip()
    match = re.search(
        r"(?i)(?:^|[^a-z])s(?P<season>\d{1,2})e(?P<episode>\d{1,2})\b|\b(?P<season_alt>\d{1,2})x(?P<episode_alt>\d{1,2})\b",
        t,
    )
    if not match:
        return None

    season = match.group("season") or match.group("season_alt")
    episode = match.group("episode") or match.group("episode_alt")
    if not season:
        return None

    prefix = t[:match.start()]
    prefix = re.sub(r"\b(?:19|20)\d{2}\b", " ", prefix, flags=re.IGNORECASE)
    prefix = re.sub(
        r"(?i)\b(?:season|series|episode|ep|pilot|special|part|web|dl|webrip|webdl|2160p|1080p|x264|x265|hevc|avc|bluray|hmax|amzn|nf|atvp|dsnp|now|repack|hdr|dolby|atmos|dv|xvid|avi|mkv|mp4|h264|h265)\b",
        " ",
        prefix,
    )
    prefix = prefix.replace("&", " and ")
    prefix = re.sub(r"[\[\]\(\)\{\}\._/-]+", " ", prefix)
    prefix = re.sub(r"[^a-z0-9\s]", " ", prefix.lower())
    prefix = re.sub(r"\s+", " ", prefix).strip()
    if not prefix:
        return None

    clean_series = re.sub(r"\b(?:the|a|an)\b", " ", prefix)
    clean_series = re.sub(r"\s+", "_", clean_series).strip("_")
    ep_id = f"s{int(season):02d}e{int(episode):02d}" if episode else f"s{int(season):02d}_pack"
    return clean_series, ep_id


def season_number_from_ep_id(ep_id: str) -> Optional[int]:
    match = re.match(r"s(\d+)", ep_id or "")
    return int(match.group(1)) if match else None


def extract_movie_key(title: str) -> Optional[str]:
    """Return a unique key for movie deduplication."""
    match = re.search(r"^(.*?)(?:\(|\[|\b)(\d{4})(?:\)|\]|\b)", title)
    if match:
        raw_name, year = match.groups()
    else:
        raw_name, year = title, ""
    clean_name = re.sub(r"[^\w\s]", "", raw_name).strip().lower()
    clean_name = re.sub(r"[\s_.]+", "_", clean_name)
    if not clean_name:
        return None
    return f"{clean_name}_{year}" if year else clean_name


# ==========================================
# NETWORK & API HANDLING
# ==========================================
def safe_http_get(session: requests.Session, url: str, params: dict = None, max_retries: int = 3) -> Optional[requests.Response]:
    for attempt in range(max_retries):
        try:
            res = session.get(url, params=params, timeout=10)
            if res.status_code == 429:
                time.sleep((2 ** attempt) + 1)
                continue
            if res.status_code == 403:
                logger.error(f"HTTP 403 Forbidden accessing {url}")
                return None
            res.raise_for_status()
            return res
        except Exception as exc:
            if attempt == max_retries - 1:
                logger.error(f"Request failed for {url}: {exc}")
            time.sleep(1)
    return None


# ==========================================
# TORBOX MANAGEMENT & CACHE
# ==========================================
def fetch_torbox_library(session: requests.Session) -> List[dict]:
    res = safe_http_get(session, f"{TORBOX_BASE_URL}/mylist", params={"bypass_cache": "true"})
    if not res:
        return []
    try:
        data = res.json().get("data", [])
        if isinstance(data, dict):
            data = data.get("items", data.get("torrents", []))
        return data if isinstance(data, list) else []
    except Exception as exc:
        logger.error(f"Failed to parse TorBox library: {exc}")
        return []


def load_series_index() -> Dict[str, dict]:
    """Persistent hash -> {series_id, ep_id} map so TV episode identity can rely on the canonical
    TMDB show id instead of re-parsing a torrent's (often inconsistently spelled) release name."""
    if os.path.exists(SERIES_INDEX_PATH):
        try:
            with open(SERIES_INDEX_PATH, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except Exception as exc:
            logger.warning(f"Failed to load {SERIES_INDEX_PATH}: {exc}")
    return {}


def save_series_index(index: Dict[str, dict]) -> None:
    try:
        with open(SERIES_INDEX_PATH, "w", encoding="utf-8") as handle:
            json.dump(index, handle)
    except Exception as exc:
        logger.warning(f"Failed to save {SERIES_INDEX_PATH}: {exc}")


def get_existing_torbox_items(library: List[dict], series_index: Dict[str, dict]) -> Tuple[Set[str], Set[str]]:
    existing_episodes = set()
    existing_movies = set()

    for item in library:
        name = item.get("name") or item.get("torrent_name") or ""
        item_hash = (item.get("hash") or item.get("info_hash") or "").lower()
        indexed = series_index.get(item_hash) if item_hash else None
        if indexed and indexed.get("series_id") and indexed.get("ep_id"):
            existing_episodes.add(f"{indexed['series_id']}_{indexed['ep_id']}")
            continue

        ep_info = extract_episode_info(name)
        if ep_info:
            existing_episodes.add(f"{ep_info[0]}_{ep_info[1]}")
        else:
            mov_key = extract_movie_key(name)
            if mov_key:
                existing_movies.add(mov_key)

    return existing_episodes, existing_movies


def delete_torbox_torrent(session: requests.Session, torrent_id) -> bool:
    try:
        res = session.post(f"{TORBOX_BASE_URL}/controltorrent", json={"torrent_id": torrent_id, "operation": "delete"})
        return bool(res is not None and res.status_code == 200)
    except Exception as exc:
        logger.error(f"Failed to delete TorBox torrent {torrent_id}: {exc}")
        return False


def prune_torbox_library(session: requests.Session, library: List[dict], dry_run: bool, series_index: Dict[str, dict],
                          max_movies: int = MAX_MOVIES, max_tv_series: int = MAX_TV_SERIES) -> None:
    """Keep only the newest max_movies movies and max_tv_series distinct TV series (latest season only per series); delete the rest."""
    movie_items: Dict[str, List[dict]] = {}
    movie_latest_date: Dict[str, str] = {}
    series_items: Dict[str, List[Tuple[dict, Tuple[str, str]]]] = {}
    series_latest_date: Dict[str, str] = {}

    for item in library:
        name = item.get("name") or item.get("torrent_name") or ""
        if not name or item.get("id") is None:
            continue
        added = item.get("created_at") or item.get("updated_at") or ""

        ep_info = extract_episode_info(name)
        if ep_info:
            series_key = ep_info[0]
            series_items.setdefault(series_key, []).append((item, ep_info))
            if added > series_latest_date.get(series_key, ""):
                series_latest_date[series_key] = added
            continue

        mov_key = extract_movie_key(name)
        if not mov_key:
            continue
        movie_items.setdefault(mov_key, []).append(item)
        if added > movie_latest_date.get(mov_key, ""):
            movie_latest_date[mov_key] = added

    kept_movie_keys = {
        key for key, _ in sorted(movie_latest_date.items(), key=lambda pair: pair[1], reverse=True)[:max_movies]
    }
    kept_series_keys = {
        key for key, _ in sorted(series_latest_date.items(), key=lambda pair: pair[1], reverse=True)[:max_tv_series]
    }

    to_delete: List[dict] = []
    for mov_key, items in movie_items.items():
        if mov_key in kept_movie_keys:
            # Still trim accidental duplicate torrents for the same movie, keeping only the newest.
            items_sorted = sorted(items, key=lambda it: it.get("created_at") or it.get("updated_at") or "", reverse=True)
            to_delete.extend(items_sorted[1:])
        else:
            to_delete.extend(items)

    for series_key, entries in series_items.items():
        if series_key not in kept_series_keys:
            to_delete.extend(item for item, _ in entries)
            continue

        # Within a kept series, retain only its most recent season; drop episodes from older seasons.
        seasons_present = {season_number_from_ep_id(ep_id) for _, (_, ep_id) in entries}
        seasons_present.discard(None)
        if not seasons_present:
            continue
        latest_season = max(seasons_present)
        for item, (_, ep_id) in entries:
            if season_number_from_ep_id(ep_id) != latest_season:
                to_delete.append(item)

    if not to_delete:
        logger.info("TorBox pruning: no old torrents needed removal.")
        return

    for item in to_delete:
        name = item.get("name") or item.get("torrent_name") or ""
        if dry_run:
            logger.info(f"[TEST RUN] Would prune old TorBox torrent: {name}")
            continue
        if delete_torbox_torrent(session, item.get("id")):
            logger.info(f"[PRUNED] Removed old TorBox torrent: {name}")
            item_hash = (item.get("hash") or item.get("info_hash") or "").lower()
            series_index.pop(item_hash, None)
        else:
            logger.error(f"Failed to prune TorBox torrent: {name}")

    verb = "Would prune" if dry_run else "Pruned"
    logger.info(f"TorBox pruning: {verb} {len(to_delete)} old torrent(s) to maintain the {max_movies}/{max_tv_series} rolling library.")


def check_torbox_cache(session: requests.Session, hashes: List[str]) -> Dict[str, bool]:
    if not hashes:
        return {}

    cached_hashes = {}
    for i in range(0, len(hashes), 100):
        batch = hashes[i:i + 100]
        params = {"hash": ",".join(batch), "format": "object", "list_files": "false"}
        res = safe_http_get(session, f"{TORBOX_BASE_URL}/checkcached", params=params)
        if res and res.status_code == 200:
            try:
                data = res.json().get("data", {})
                if isinstance(data, dict):
                    for k, v in data.items():
                        cached_hashes[k.lower()] = bool(v)
            except Exception as exc:
                logger.error(f"Failed to parse TorBox cache response: {exc}")
    return cached_hashes


# ==========================================
# TMDB & TRACKER DISCOVERY
# ==========================================
def get_tmdb_recent_media(session: requests.Session, media_type: str, target_count: int = 50) -> List[dict]:
    results: List[dict] = []
    page = 1
    today = datetime.date.today()
    min_date = (today - datetime.timedelta(days=730)).isoformat()
    max_date = today.isoformat()

    if media_type == "movie":
        endpoint = f"{TMDB_BASE_URL}/discover/movie"
        params_template = {
            "api_key": TMDB_API_KEY,
            "sort_by": "primary_release_date.desc",
            "include_adult": "false",
            "include_video": "false",
            "language": "en-US",
            "region": "US",
            "vote_count.gte": "10",
            "primary_release_date.gte": min_date,
            "primary_release_date.lte": max_date,
        }
    else:
        endpoint = f"{TMDB_BASE_URL}/discover/tv"
        params_template = {
            "api_key": TMDB_API_KEY,
            "sort_by": "first_air_date.desc",
            "include_adult": "false",
            "language": "en-US",
            "timezone": "America/New_York",
            "vote_count.gte": "5",
            "first_air_date.gte": min_date,
            "first_air_date.lte": max_date,
            "without_genres": ",".join(str(g) for g in EXCLUDED_TV_GENRE_IDS),
        }

    while len(results) < target_count and page <= 10:
        params = {**params_template, "page": page}
        res = safe_http_get(session, endpoint, params=params)
        if not res:
            break
        data = res.json()
        page_results = data.get("results", [])
        if not page_results:
            break
        for item in page_results:
            if not is_mainstream_tmdb_candidate(item):
                continue
            if media_type == "movie":
                date_value = item.get("release_date")
            else:
                date_value = item.get("first_air_date")
            if is_recent_enough(date_value, max_months=MAX_MEDIA_AGE_MONTHS):
                results.append(item)
        page += 1

    return results[:target_count]


def get_imdb_id(session: requests.Session, tmdb_id: int, media_type: str) -> Optional[str]:
    url = f"{TMDB_BASE_URL}/{media_type}/{tmdb_id}/external_ids"
    res = safe_http_get(session, url, params={"api_key": TMDB_API_KEY})
    if res and res.status_code == 200:
        imdb_id = res.json().get("imdb_id")
        if imdb_id and imdb_id.startswith("tt"):
            return imdb_id
    return None


def get_latest_season_progress(session: requests.Session, tmdb_id: int) -> Tuple[int, int, Optional[str]]:
    """Return (season_number, episodes_aired_so_far, last_air_date) for the show's current/most recent season.

    Mirrors how Stremio/Torrentio-style clients track episode progress: TMDB's last_episode_to_air
    tells us both the active season and how many episodes of it have actually aired, so we know the
    full range of episodes worth searching for instead of only ever asking for episode 1. The air
    date is used to judge whether the show is still recently active, since a long-running show's
    original first_air_date can be years old even while it airs brand new episodes today.
    """
    res = safe_http_get(session, f"{TMDB_BASE_URL}/tv/{tmdb_id}", params={"api_key": TMDB_API_KEY})
    if res and res.status_code == 200:
        data = res.json()
        last_ep = data.get("last_episode_to_air") or {}
        season_number = last_ep.get("season_number")
        episode_number = last_ep.get("episode_number")
        last_air_date = last_ep.get("air_date") or data.get("last_air_date")
        if isinstance(season_number, int) and season_number > 0 and isinstance(episode_number, int) and episode_number > 0:
            return season_number, episode_number, last_air_date

        next_ep = data.get("next_episode_to_air") or {}
        season_number = next_ep.get("season_number")
        if isinstance(season_number, int) and season_number > 0:
            return season_number, 1, next_ep.get("air_date") or last_air_date

        number_of_seasons = data.get("number_of_seasons")
        if isinstance(number_of_seasons, int) and number_of_seasons > 0:
            return number_of_seasons, 1, last_air_date
    return 1, 1, None


def get_tmdb_active_tv_series(session: requests.Session, target_count: int = 50) -> List[dict]:
    """Discover currently-active returning TV series by recent episode air date rather than the
    show's original launch date, so long-running shows airing new seasons/episodes are not missed
    by a "newly launched" discovery pass alone."""
    results: List[dict] = []
    page = 1
    today = datetime.date.today()
    min_date = (today - datetime.timedelta(days=730)).isoformat()
    max_date = today.isoformat()
    endpoint = f"{TMDB_BASE_URL}/discover/tv"
    params_template = {
        "api_key": TMDB_API_KEY,
        "sort_by": "popularity.desc",
        "include_adult": "false",
        "language": "en-US",
        "vote_count.gte": "5",
        "air_date.gte": min_date,
        "air_date.lte": max_date,
        "without_genres": ",".join(str(g) for g in EXCLUDED_TV_GENRE_IDS),
    }

    while len(results) < target_count and page <= 10:
        params = {**params_template, "page": page}
        res = safe_http_get(session, endpoint, params=params)
        if not res:
            break
        data = res.json()
        page_results = data.get("results", [])
        if not page_results:
            break
        for item in page_results:
            if is_mainstream_tmdb_candidate(item):
                results.append(item)
        page += 1

    return results[:target_count]


def dedupe_push_items_by_hash(items: List[dict]) -> List[dict]:
    """Collapse candidates sharing the same torrent hash (e.g. multiple episodes resolved from one
    season-pack file) into a single push entry, so the same magnet isn't submitted to TorBox repeatedly."""
    merged: Dict[str, dict] = {}
    for item in items:
        h = item["hash"]
        if h not in merged:
            merged[h] = {**item, "titles": [item["title"]]}
        else:
            merged[h]["titles"].append(item["title"])
    return list(merged.values())


def log_progress(label: str, done: int, total: int, start_time: float) -> None:
    """Periodic heartbeat so a long Torrentio pass doesn't look hung when watched live."""
    elapsed = time.time() - start_time
    rate = elapsed / done if done else 0
    remaining = rate * (total - done)
    logger.info(f"  ...progress: {done}/{total} {label} evaluated ({elapsed / 60:.1f} min elapsed, ~{remaining / 60:.1f} min remaining)")


def process_movie(movie: dict, torrentio_base_url: str, existing_movies: Set[str]) -> Optional[Tuple[dict, List[dict], Dict[str, bool]]]:
    """Evaluate one movie against Torrentio. Runs in its own thread with its own HTTP session."""
    title = movie.get("title", "")
    release_date = movie.get("release_date", "")
    year = release_date[:4] if release_date else ""
    tmdb_id = movie.get("id")
    mov_key = extract_movie_key(f"{title} {year}")

    if not title or not mov_key or not is_recent_enough(release_date):
        return None
    if not is_mainstream_tmdb_candidate(movie):
        return None

    diag = {"title": title, "year": year, "key": mov_key, "status": "PENDING", "streams_found": 0, "reason": ""}
    local_hints: Dict[str, bool] = {}

    if mov_key in existing_movies:
        diag["status"] = "SKIPPED"
        diag["reason"] = "Already exists in TorBox library"
        return diag, [], local_hints

    session = requests.Session(impersonate="chrome")
    session.headers.update({"Authorization": f"Bearer {TORBOX_API_KEY}"})

    imdb_id = get_imdb_id(session, tmdb_id, "movie")
    if not imdb_id:
        diag["status"] = "REJECTED"
        diag["reason"] = "No IMDb ID mapped on TMDB"
        return diag, [], local_hints

    res = safe_http_get(session, f"{torrentio_base_url}/movie/{imdb_id}.json")
    time.sleep(1.0)

    valid_found: List[dict] = []
    if res and res.status_code == 200:
        streams = res.json().get("streams", [])
        diag["streams_found"] = len(streams)
        seen_hashes = set()
        seen_keys = set()

        for stream in streams:
            t_hash = extract_stream_hash(stream)
            details = stream.get("title", "")
            if not details or not t_hash:
                continue
            if not is_valid_release(details):
                continue

            quality = extract_quality_bucket(details)
            if quality not in ALLOWED_QUALITY_PRIORITY:
                continue

            dedupe_key = build_torrent_dedupe_key(details, year=year)
            if t_hash in seen_hashes or dedupe_key in seen_keys:
                continue
            seen_hashes.add(t_hash)
            seen_keys.add(dedupe_key)

            local_hints[t_hash] = local_hints.get(t_hash, False) or is_stream_cached_hint(stream)

            score = calculate_quality_score(details)
            if score > 0:
                candidate = {
                    "key": mov_key,
                    "hash": t_hash,
                    "magnet": f"magnet:?xt=urn:btih:{t_hash}&dn={quote(title)}",
                    "score": score,
                    "sort_date": release_date or "1970-01-01",
                    "title": f"{title} ({year}) [{details.splitlines()[0]}]",
                }
                valid_found.append(candidate)

        if not valid_found:
            diag["status"] = "REJECTED"
            diag["reason"] = "No valid 2160p/1080p English releases found"
        else:
            diag["candidates"] = valid_found

    return diag, valid_found, local_hints


def process_show(show: dict, torrentio_base_url: str, existing_episodes: Set[str]) -> Optional[Tuple[dict, List[dict], Dict[str, bool]]]:
    """Evaluate one TV show across its currently-aired episodes. Runs in its own thread with its own HTTP session."""
    show_title = show.get("name", "")
    first_air_date = show.get("first_air_date", "")

    if not show_title:
        return None
    if not is_mainstream_tmdb_candidate(show):
        return None

    diag = {"title": show_title, "year": first_air_date[:4] if first_air_date else "", "status": "PENDING", "streams_found": 0, "reason": ""}
    local_hints: Dict[str, bool] = {}

    session = requests.Session(impersonate="chrome")
    session.headers.update({"Authorization": f"Bearer {TORBOX_API_KEY}"})

    tmdb_id = show.get("id")
    imdb_id = get_imdb_id(session, tmdb_id, "tv")
    if not imdb_id:
        diag["status"] = "REJECTED"
        diag["reason"] = "No IMDb ID mapped on TMDB"
        return diag, [], local_hints

    latest_season, episodes_aired, last_air_date = get_latest_season_progress(session, tmdb_id)
    # Gate recency on the show's most recent aired episode, not its original launch date, so
    # long-running/returning series with new activity aren't excluded just for being "old".
    recency_reference = last_air_date or first_air_date
    if not is_recent_enough(recency_reference):
        diag["status"] = "REJECTED"
        diag["reason"] = "No episodes aired within the last 24 months"
        return diag, [], local_hints

    # Canonical series identity (TMDB id) instead of text parsed from release names, so the same show
    # uploaded under different naming conventions (e.g. "Law & Order: SVU" vs "Law and Order Special
    # Victims Unit") is always treated as one series and doesn't get pushed/tracked twice.
    series_id = f"tmdb{tmdb_id}"
    episodes_to_check = min(episodes_aired, MAX_NEW_EPISODES_PER_SHOW)

    valid_found: List[dict] = []
    got_any_response = False

    for episode_number in range(1, episodes_to_check + 1):
        guessed_ep_id = f"s{latest_season:02d}e{episode_number:02d}"
        if f"{series_id}_{guessed_ep_id}" in existing_episodes:
            continue  # already have this episode; no need to re-query Torrentio for it

        res = safe_http_get(session, f"{torrentio_base_url}/series/{imdb_id}:{latest_season}:{episode_number}.json")
        time.sleep(1.0)
        if not (res and res.status_code == 200):
            continue
        got_any_response = True

        streams = res.json().get("streams", [])
        diag["streams_found"] += len(streams)
        # Reset per episode: a season-pack torrent shares one info-hash across many episodes,
        # so dedup must not persist across different episode-number queries for this show.
        seen_hashes = set()
        seen_keys = set()
        for stream in streams:
            details = stream.get("title", "")
            if not details:
                continue
            if not is_valid_release(details):
                continue

            quality = extract_quality_bucket(details)
            if quality not in ALLOWED_QUALITY_PRIORITY:
                continue

            t_hash = extract_stream_hash(stream)
            if not t_hash:
                continue
            episode_text = get_canonical_episode_text(stream)
            ep_info = extract_episode_info(episode_text)
            if not ep_info:
                continue
            _, ep_id = ep_info
            if season_number_from_ep_id(ep_id) != latest_season:
                continue
            full_ep_key = f"{series_id}_{ep_id}"
            dedupe_key = build_torrent_dedupe_key(episode_text, episode_key=full_ep_key)

            if t_hash in seen_hashes or dedupe_key in seen_keys:
                continue
            seen_hashes.add(t_hash)
            seen_keys.add(dedupe_key)

            local_hints[t_hash] = local_hints.get(t_hash, False) or is_stream_cached_hint(stream)

            score = calculate_quality_score(details)
            if score > 0 and full_ep_key not in existing_episodes:
                candidate = {
                    "series_key": series_id,
                    "ep_id": ep_id,
                    "full_key": full_ep_key,
                    "hash": t_hash,
                    "magnet": f"magnet:?xt=urn:btih:{t_hash}&dn={quote(show_title)}",
                    "score": score,
                    "sort_date": last_air_date or first_air_date or "1970-01-01",
                    "title": episode_text.strip() or details.splitlines()[0],
                }
                valid_found.append(candidate)

    if not got_any_response:
        diag["status"] = "REJECTED"
        diag["reason"] = "No stream response from Torrentio"
        return diag, [], local_hints

    if not valid_found:
        diag["status"] = "REJECTED"
        diag["reason"] = "No new valid 2160p/1080p English episodes found"
    else:
        diag["candidates"] = valid_found

    return diag, valid_found, local_hints


# ==========================================
# MAIN AUTOMATION ROUTINE
# ==========================================
def main():
    if not TMDB_API_KEY or not TORBOX_API_KEY:
        raise ValueError("Missing API_TMDB or API_TORBOX environment variables.")

    session = requests.Session(impersonate="chrome")
    session.headers.update({"Authorization": f"Bearer {TORBOX_API_KEY}"})

    logger.info(f"--- RUNNING TORBOX AUTOMATION PIPELINE (TEST_RUN = {TEST_RUN}) ---")
    manifest_urls = read_manifest_urls()
    logger.info(f"Loaded {len(manifest_urls)} manifest URLs from {MANIFEST_PATH}.")

    torrentio_base_url = get_torrentio_stream_base_url(manifest_urls)
    logger.info(f"Using Torrentio stream base URL: {torrentio_base_url}")

    torbox_library = fetch_torbox_library(session)
    series_index = load_series_index()
    existing_episodes, existing_movies = get_existing_torbox_items(torbox_library, series_index)
    logger.info(f"Existing TorBox items indexed: {len(existing_movies)} movies, {len(existing_episodes)} episodes.")

    recent_movies = get_tmdb_recent_media(session, "movie", target_count=60)
    recent_shows = get_tmdb_recent_media(session, "tv", target_count=60)

    active_shows = get_tmdb_active_tv_series(session, target_count=60)
    known_show_ids = {s.get("id") for s in recent_shows}
    added_active = [s for s in active_shows if s.get("id") not in known_show_ids]
    recent_shows += added_active
    logger.info(f"Currently-active TV discovery added {len(added_active)} returning series not found by the newly-launched pass.")

    recent_movies, recent_shows = merge_justwatch_new_titles(session, recent_movies, recent_shows)
    logger.info(f"TMDB + JustWatch new results: {len(recent_movies)} movies and {len(recent_shows)} series after merge.")

    # Evaluate shows by genuine popularity/rating (from the TMDB metadata we already fetched) rather than
    # by discovery-pass order, so a well-regarded returning series isn't starved out of the 20 series slots
    # just because a flood of brand-new season-1 shows happened to be processed first.
    recent_shows.sort(key=lambda s: (float(s.get("popularity") or 0.0), float(s.get("vote_average") or 0.0)), reverse=True)

    # Estimate using observed per-item pace rather than theoretical worst/best case; divided by the
    # measured concurrency speedup since Torrentio calls now run across TORRENTIO_CONCURRENCY workers.
    # A light +/-25% buffer accounts for variance (shows with more/fewer already-owned episodes skip
    # fewer/more calls).
    est_seconds = (len(recent_movies) * SECONDS_PER_MOVIE_ESTIMATE + len(recent_shows) * SECONDS_PER_TV_SHOW_ESTIMATE) / EFFECTIVE_CONCURRENCY_SPEEDUP
    est_min_minutes = (est_seconds * 0.75) / 60
    est_max_minutes = (est_seconds * 1.25) / 60
    logger.info(f"Found {len(recent_movies)} candidate movies and {len(recent_shows)} candidate TV shows to check against Torrentio.")
    logger.info(f"Estimated Torrentio querying time: {est_min_minutes:.0f}-{est_max_minutes:.0f} minutes (~{SECONDS_PER_MOVIE_ESTIMATE:.1f}s/movie, ~{SECONDS_PER_TV_SHOW_ESTIMATE:.0f}s/show).")

    movie_candidates = []
    movie_diagnostics = []
    hash_cache_hints: Dict[str, bool] = {}

    logger.info("Querying Torrentio for recent films...")
    movie_loop_start = time.time()
    with ThreadPoolExecutor(max_workers=TORRENTIO_CONCURRENCY) as executor:
        results = executor.map(lambda m: process_movie(m, torrentio_base_url, existing_movies), recent_movies)
        for movie_index, result in enumerate(results, 1):
            if movie_index % PROGRESS_LOG_EVERY_MOVIES == 0:
                log_progress("movies", movie_index, len(recent_movies), movie_loop_start)
            if result is None:
                continue
            diag, candidates, hints = result
            movie_diagnostics.append(diag)
            movie_candidates.extend(candidates)
            for h, hint in hints.items():
                hash_cache_hints[h] = hash_cache_hints.get(h, False) or hint

    logger.info("Querying Torrentio for recent series...")
    tv_candidates = []
    tv_diagnostics = []
    tv_loop_start = time.time()
    with ThreadPoolExecutor(max_workers=TORRENTIO_CONCURRENCY) as executor:
        results = executor.map(lambda s: process_show(s, torrentio_base_url, existing_episodes), recent_shows)
        for show_index, result in enumerate(results, 1):
            if show_index % PROGRESS_LOG_EVERY_SHOWS == 0:
                log_progress("TV shows", show_index, len(recent_shows), tv_loop_start)
            if result is None:
                continue
            diag, candidates, hints = result
            tv_diagnostics.append(diag)
            tv_candidates.extend(candidates)
            for h, hint in hints.items():
                hash_cache_hints[h] = hash_cache_hints.get(h, False) or hint

    all_hashes = list({c["hash"] for c in movie_candidates + tv_candidates})
    logger.info(f"Pinging TorBox cache endpoint for {len(all_hashes)} candidate hashes...")
    cached_map = check_torbox_cache(session, all_hashes)
    for h, hint in hash_cache_hints.items():
        if hint:
            cached_map[h] = True

    selected_movies = {}
    for diag in movie_diagnostics:
        if diag.get("status") in ["SKIPPED", "REJECTED"]:
            continue
        candidates = diag.get("candidates", [])
        cached_options = [c for c in candidates if cached_map.get(c["hash"])]
        if not cached_options:
            diag["status"] = "REJECTED"
            diag["reason"] = f"0 of {len(candidates)} valid releases cached on TorBox"
        else:
            best = max(cached_options, key=lambda x: (x["score"], x["sort_date"]))
            diag["status"] = "SELECTED"
            diag["selected_release"] = best
            selected_movies[diag["key"]] = best

    final_movies = sorted(selected_movies.values(), key=lambda item: item["sort_date"], reverse=True)[:MAX_MOVIES]

    selected_tv = {}
    selected_series_keys = set()
    for diag in tv_diagnostics:
        if diag.get("status") in ["SKIPPED", "REJECTED"]:
            continue
        candidates = diag.get("candidates", [])
        cached_options = [c for c in candidates if cached_map.get(c["hash"])]
        if not cached_options:
            diag["status"] = "REJECTED"
            diag["reason"] = f"0 of {len(candidates)} valid episodes cached on TorBox"
            continue

        series_key = cached_options[0]["series_key"]
        if series_key not in selected_series_keys and len(selected_series_keys) >= MAX_TV_SERIES:
            diag["status"] = "REJECTED"
            diag["reason"] = f"Already tracking {MAX_TV_SERIES} newer TV series this run"
            continue

        selected_series_keys.add(series_key)
        diag["status"] = "SELECTED"
        show_full_keys = set()
        for c in cached_options:
            full_key = c["full_key"]
            if full_key not in selected_tv or (c["score"], c["sort_date"]) > (selected_tv[full_key]["score"], selected_tv[full_key]["sort_date"]):
                selected_tv[full_key] = c
            show_full_keys.add(full_key)
        diag["selected_count"] = len(show_full_keys)

    final_tv_payloads = sorted(selected_tv.values(), key=lambda item: item["sort_date"], reverse=True)

    logger.info("==========================================")
    logger.info("MOVIE AUDIT BREAKDOWN:")
    for diag in movie_diagnostics:
        title = f"{diag['title']} ({diag['year']})"
        if diag["status"] == "SELECTED":
            rel = diag["selected_release"]
            logger.info(f"  [SELECTED] {title} -> {rel['title']}")
        elif diag["status"] == "SKIPPED":
            logger.info(f"  [SKIPPED]  {title} -> {diag['reason']}")
        else:
            logger.info(f"  [REJECTED] {title} -> {diag['reason']}")

    logger.info("==========================================")
    logger.info("TV AUDIT BREAKDOWN:")
    for diag in tv_diagnostics:
        title = f"{diag['title']} ({diag['year']})"
        if diag["status"] == "SELECTED":
            logger.info(f"  [SELECTED] {title} -> {diag['selected_count']} episode(s) queued")
        elif diag["status"] == "SKIPPED":
            logger.info(f"  [SKIPPED]  {title} -> {diag['reason']}")
        else:
            logger.info(f"  [REJECTED] {title} -> {diag['reason']}")

    logger.info("==========================================")
    logger.info(f"SELECTION SUMMARY: {len(final_movies)} Movies, {len(selected_series_keys)} TV Series ({len(final_tv_payloads)} new episodes) queued.")
    logger.info("==========================================")

    pushable_movies = dedupe_push_items_by_hash(final_movies)
    pushable_tv = dedupe_push_items_by_hash(final_tv_payloads)

    if TEST_RUN:
        logger.info("[TEST RUN ACTIVE] Items identified but NOT pushed to TorBox:\n")
        for m in pushable_movies:
            logger.info(f"  - FILM: {m['title']} (Hash: {m['hash']})")
        for t in pushable_tv:
            title = t["titles"][0] if len(t["titles"]) == 1 else f"{t['titles'][0]} (+{len(t['titles']) - 1} more episode(s) in this file)"
            logger.info(f"  - TV:   {title} (Hash: {t['hash']})")
    else:
        logger.info("[LIVE RUN ACTIVE] Pushing cached magnets to TorBox...")
        for item in pushable_movies + pushable_tv:
            try:
                res = session.post(f"{TORBOX_BASE_URL}/createtorrent", data={"magnet": item["magnet"]})
                if res and res.status_code == 200:
                    logger.info(f"[SUCCESS] Added: {item['title']}")
                    if "ep_id" in item:
                        series_index[item["hash"]] = {"series_id": item["series_key"], "ep_id": item["ep_id"]}
                else:
                    logger.error(f"Failed adding {item['title']}")
                time.sleep(0.5)
            except Exception as exc:
                logger.error(f"Error adding {item['title']}: {exc}")

    logger.info("==========================================")
    logger.info("Pruning TorBox library to maintain rolling window...")
    prune_torbox_library(session, torbox_library, dry_run=TEST_RUN, series_index=series_index)

    if not TEST_RUN:
        save_series_index(series_index)


if __name__ == "__main__":
    main()
