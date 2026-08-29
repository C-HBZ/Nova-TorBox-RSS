import os
import re
import time
import logging
import json
import unicodedata
import datetime
from urllib.parse import quote
from typing import List, Dict, Set, Tuple, Optional
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

BAD_RELEASE_RE = re.compile(r"\b(?:" + "|".join(re.escape(p) for p in BAD_RELEASE_PATTERNS) + r")\b", re.IGNORECASE)
FOREIGN_ONLY_RE = re.compile(r"\b(?:" + "|".join(re.escape(p) for p in FOREIGN_ONLY_PATTERNS) + r")\b", re.IGNORECASE)
ENGLISH_HINT_RE = re.compile(r"\b(?:" + "|".join(re.escape(p) for p in ENGLISH_HINT_PATTERNS) + r")\b", re.IGNORECASE)
NON_LATIN_PATTERN = re.compile(r"[\u0400-\u04FF\u4E00-\u9FFF\u3040-\u30FF\uAC00-\uD7AF]")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
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


def is_recent_enough(date_value: Optional[str], max_months: int = MAX_MEDIA_AGE_MONTHS) -> bool:
    if not date_value:
        return False
    try:
        date_text = date_value.split("T")[0]
        year, month, day = [int(part) for part in date_text.split("-")[:3]]
        then = datetime.date(year, month, day)
        now = datetime.date.today()
        months_delta = (now.year - then.year) * 12 + (now.month - then.month)
        return 0 <= months_delta <= max_months
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
    """Bias 2160p ahead of 1080p, and ignore all others."""
    quality = extract_quality_bucket(title)
    if quality == "2160P":
        score = 100.0
        if any(hdr in title.upper() for hdr in ["HDR", "DV", "DOVI", "HDR10+"]):
            score += 10.0
        return score
    if quality == "1080P":
        return 90.0
    return 0.0


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
def get_existing_torbox_items(session: requests.Session) -> Tuple[Set[str], Set[str]]:
    res = safe_http_get(session, f"{TORBOX_BASE_URL}/mylist", params={"bypass_cache": "true"})
    if not res:
        return set(), set()

    try:
        data = res.json().get("data", [])
        if isinstance(data, dict):
            data = data.get("items", data.get("torrents", []))

        existing_episodes = set()
        existing_movies = set()

        for item in data:
            name = item.get("name") or item.get("torrent_name") or ""
            ep_info = extract_episode_info(name)
            if ep_info:
                existing_episodes.add(f"{ep_info[0]}_{ep_info[1]}")
            else:
                mov_key = extract_movie_key(name)
                if mov_key:
                    existing_movies.add(mov_key)

        return existing_episodes, existing_movies
    except Exception as exc:
        logger.error(f"Failed to parse existing TorBox items: {exc}")
        return set(), set()


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

    existing_episodes, existing_movies = get_existing_torbox_items(session)
    logger.info(f"Existing TorBox items indexed: {len(existing_movies)} movies, {len(existing_episodes)} episodes.")

    recent_movies = get_tmdb_recent_media(session, "movie", target_count=60)
    recent_shows = get_tmdb_recent_media(session, "tv", target_count=60)
    recent_movies, recent_shows = merge_justwatch_new_titles(session, recent_movies, recent_shows)
    logger.info(f"TMDB + JustWatch new results: {len(recent_movies)} movies and {len(recent_shows)} series after merge.")

    movie_candidates = []
    movie_diagnostics = []

    logger.info("Querying Torrentio for recent films...")
    for movie in recent_movies:
        title = movie.get("title", "")
        release_date = movie.get("release_date", "")
        year = release_date[:4] if release_date else ""
        tmdb_id = movie.get("id")
        mov_key = extract_movie_key(f"{title} {year}")

        if not title or not mov_key or not is_recent_enough(release_date):
            continue
        if not is_mainstream_tmdb_candidate(movie):
            continue

        diag = {"title": title, "year": year, "key": mov_key, "status": "PENDING", "streams_found": 0, "reason": ""}

        if mov_key in existing_movies:
            diag["status"] = "SKIPPED"
            diag["reason"] = "Already exists in TorBox library"
            movie_diagnostics.append(diag)
            continue

        imdb_id = get_imdb_id(session, tmdb_id, "movie")
        if not imdb_id:
            diag["status"] = "REJECTED"
            diag["reason"] = "No IMDb ID mapped on TMDB"
            movie_diagnostics.append(diag)
            continue

        res = safe_http_get(session, f"{TORRENTIO_BASE_URL}/movie/{imdb_id}.json")
        time.sleep(1.0)

        if res and res.status_code == 200:
            streams = res.json().get("streams", [])
            diag["streams_found"] = len(streams)
            valid_found = []
            seen_hashes = set()
            seen_keys = set()

            for stream in streams:
                t_hash = stream.get("infoHash", "").lower()
                details = stream.get("title", "")
                if not details:
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
                    movie_candidates.append(candidate)
                    valid_found.append(candidate)

            if not valid_found:
                diag["status"] = "REJECTED"
                diag["reason"] = "No valid 2160p/1080p English releases found"
            else:
                diag["candidates"] = valid_found
            movie_diagnostics.append(diag)

    logger.info("Querying Torrentio for recent series...")
    tv_candidates = []
    for show in recent_shows:
        show_title = show.get("name", "")
        first_air_date = show.get("first_air_date", "")
        if not show_title or not is_recent_enough(first_air_date):
            continue
        if not is_mainstream_tmdb_candidate(show):
            continue

        tmdb_id = show.get("id")
        imdb_id = get_imdb_id(session, tmdb_id, "tv")
        if not imdb_id:
            continue

        res = safe_http_get(session, f"{TORRENTIO_BASE_URL}/series/{imdb_id}:1:1.json")
        time.sleep(1.0)
        if not (res and res.status_code == 200):
            continue

        seen_hashes = set()
        seen_keys = set()
        for stream in res.json().get("streams", []):
            details = stream.get("title", "")
            if not details:
                continue
            if not is_valid_release(details):
                continue

            quality = extract_quality_bucket(details)
            if quality not in ALLOWED_QUALITY_PRIORITY:
                continue

            t_hash = stream.get("infoHash", "").lower()
            ep_info = extract_episode_info(details)
            if ep_info:
                series_key, ep_id = ep_info
                full_ep_key = f"{series_key}_{ep_id}"
                dedupe_key = build_torrent_dedupe_key(details, episode_key=full_ep_key)
            else:
                continue

            if t_hash in seen_hashes or dedupe_key in seen_keys:
                continue
            seen_hashes.add(t_hash)
            seen_keys.add(dedupe_key)

            score = calculate_quality_score(details)
            if score > 0 and full_ep_key not in existing_episodes:
                tv_candidates.append({
                    "series_key": series_key,
                    "ep_id": ep_id,
                    "full_key": full_ep_key,
                    "hash": t_hash,
                    "magnet": f"magnet:?xt=urn:btih:{t_hash}&dn={quote(show_title)}",
                    "score": score,
                    "sort_date": first_air_date or "1970-01-01",
                    "title": details.splitlines()[0],
                })

    all_hashes = list({c["hash"] for c in movie_candidates + tv_candidates})
    logger.info(f"Pinging TorBox cache endpoint for {len(all_hashes)} candidate hashes...")
    cached_map = check_torbox_cache(session, all_hashes)

    selected_movies = {}
    for diag in movie_diagnostics:
        if diag.get("status") in ["SKIPPED", "REJECTED"]:
            continue
        candidates = diag.get("candidates", [])
        cached_options = [c for c in candidates if cached_map.get(c["hash"])]
        if not cached_options:
            diag["status"] = "REJECTED"
            diag["reason"] = f"0 of {diag['streams_found']} streams cached on TorBox"
        else:
            best = max(cached_options, key=lambda x: (x["score"], x["sort_date"]))
            diag["status"] = "SELECTED"
            diag["selected_release"] = best
            selected_movies[diag["key"]] = best

    final_movies = sorted(selected_movies.values(), key=lambda item: item["sort_date"], reverse=True)[:MAX_MOVIES]

    selected_tv = {}
    for c in tv_candidates:
        if not cached_map.get(c["hash"]):
            continue
        full_key = c["full_key"]
        if full_key not in selected_tv or (c["score"], c["sort_date"]) > (selected_tv[full_key]["score"], selected_tv[full_key]["sort_date"]):
            selected_tv[full_key] = c

    final_tv_payloads = sorted(selected_tv.values(), key=lambda item: item["sort_date"], reverse=True)[:MAX_TV_SERIES]

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
    logger.info(f"SELECTION SUMMARY: {len(final_movies)} Movies, {len(final_tv_payloads)} TV Episodes queued.")
    logger.info("==========================================")

    if TEST_RUN:
        logger.info("[TEST RUN ACTIVE] Items identified but NOT pushed to TorBox:\n")
        for m in final_movies:
            logger.info(f"  - FILM: {m['title']} (Hash: {m['hash']})")
        for t in final_tv_payloads:
            logger.info(f"  - TV:   {t['title']} (Hash: {t['hash']})")
    else:
        logger.info("[LIVE RUN ACTIVE] Pushing cached magnets to TorBox...")
        for item in final_movies + final_tv_payloads:
            try:
                res = session.post(f"{TORBOX_BASE_URL}/createtorrent", data={"magnet": item["magnet"]})
                if res and res.status_code == 200:
                    logger.info(f"[SUCCESS] Added: {item['title']}")
                else:
                    logger.error(f"Failed adding {item['title']}")
                time.sleep(0.5)
            except Exception as exc:
                logger.error(f"Error adding {item['title']}: {exc}")


if __name__ == "__main__":
    main()
