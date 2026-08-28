import os
import re
import time
import logging
import json
from urllib.parse import quote
from typing import List, Dict, Set, Tuple, Optional
from curl_cffi import requests

# ==========================================
# CONFIGURATION & TOGGLES
# ==========================================
TEST_RUN = True  # Set to False to perform live magnet additions to TorBox

TMDB_API_KEY = os.getenv("API_TMDB") or os.getenv("TMDB_API_KEY", "")
TORBOX_API_KEY = os.getenv("API_TORBOX") or os.getenv("TORBOX_API_KEY", "")

TORBOX_BASE_URL = "https://api.torbox.app/v1/api/torrents"
TMDB_BASE_URL = "https://api.themoviedb.org/3"
TORRENTIO_BASE_URL = "https://torrentio.strem.fun/stream"

MAX_MOVIES = 30
MAX_TV_SERIES = 20

# Regex pattern to catch bootleg releases, telesyncs, and forced foreign dubs
INVALID_RELEASE_PATTERN = re.compile(
    r"\b(CAM|HDCAM|CAMRIP|TS|TELESYNC|HDTS|TC|TELECINE|HC|KORSUB|WORKPRINT|WP|DUBBED|DUAL-AUDIO)\b",
    re.IGNORECASE
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


# ==========================================
# QUALITY & PARSING LOGIC
# ==========================================
def is_valid_release(release_title: str) -> bool:
    """Filters out low-grade bootleg releases, bad encodes, and unwanted audio dubs."""
    if not release_title:
        return False
    return not bool(INVALID_RELEASE_PATTERN.search(release_title))


def calculate_quality_score(title: str) -> float:
    """
    Calculates priority score:
    2160p/4K = 3.0 points (+0.5 for HDR/DV)
    1080p = 2.0 points
    Lower = 0 points (rejected)
    """
    t_upper = title.upper()
    score = 0.0

    if any(q in t_upper for q in ["2160P", "4K", "UHD"]):
        score = 3.0
        if any(hdr in t_upper for hdr in ["HDR", "DV", "DOVI", "HDR10+"]):
            score += 0.5
    elif "1080P" in t_upper:
        score = 2.0

    return score


def extract_episode_info(title: str) -> Optional[Tuple[str, str]]:
    """Returns (series_key, episode_identifier) e.g. ('slow_horses', 's03e01')."""
    match = re.search(r"^(.*?)[._\s-]+[sS](\d{1,2})(?:[eE](\d{1,2}))?", title)
    if not match:
        return None
    raw_name, season, episode = match.groups()
    clean_series = re.sub(r"[^\w\s]", "", raw_name).strip().lower()
    clean_series = re.sub(r"[\s_.]+", "_", clean_series)

    if episode:
        ep_id = f"s{int(season):02d}e{int(episode):02d}"
    else:
        ep_id = f"s{int(season):02d}_pack"

    return clean_series, ep_id


def extract_movie_key(title: str) -> Optional[str]:
    """Returns a unique key for deduplication e.g. 'the_day_of_the_jackal_2024'."""
    match = re.search(r"^(.*?)(?:\(|\[|\b)(\d{4})(?:\)|\]|\b)", title)
    if match:
        raw_name, year = match.groups()
    else:
        raw_name = title
        year = ""
    clean_name = re.sub(r"[^\w\s]", "", raw_name).strip().lower()
    clean_name = re.sub(r"[\s_.]+", "_", clean_name)
    if not clean_name:
        return None
    return f"{clean_name}_{year}" if year else clean_name


# ==========================================
# NETWORK & API HANDLING
# ==========================================
def safe_http_get(session: requests.Session, url: str, params: dict = None, max_retries: int = 3) -> Optional[requests.Response]:
    """Executes HTTP GET with exponential backoff to handle rate limits (429/403)."""
    for attempt in range(max_retries):
        try:
            res = session.get(url, params=params, timeout=10)
            if res.status_code == 429:
                wait = (2 ** attempt) + 1
                logger.warning(f"Rate limited (429). Retrying in {wait}s...")
                time.sleep(wait)
                continue
            if res.status_code == 403:
                logger.error(f"HTTP 403 Forbidden accessing {url}. Cloudflare protection active.")
                return None
            res.raise_for_status()
            return res
        except Exception as e:
            if attempt == max_retries - 1:
                logger.error(f"Request failed for {url}: {e}")
            time.sleep(1)
    return None


# ==========================================
# TORBOX MANAGEMENT & CACHE
# ==========================================
def get_existing_torbox_items(session: requests.Session) -> Tuple[Set[str], Set[str]]:
    """Fetches current TorBox library to prevent re-adding existing items."""
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
    except Exception as e:
        logger.error(f"Failed to parse existing TorBox items: {e}")
        return set(), set()


def check_torbox_cache(session: requests.Session, hashes: List[str]) -> Dict[str, bool]:
    """Batch checks hashes against TorBox servers."""
    if not hashes:
        return {}

    cached_hashes = {}
    for i in range(0, len(hashes), 100):
        batch = hashes[i:i+100]
        params = {"hash": ",".join(batch), "format": "object", "list_files": "false"}
        res = safe_http_get(session, f"{TORBOX_BASE_URL}/checkcached", params=params)
        if res and res.status_code == 200:
            try:
                data = res.json().get("data", {})
                if isinstance(data, dict):
                    for k, v in data.items():
                        cached_hashes[k.lower()] = bool(v)
            except Exception as e:
                logger.error(f"Failed to parse TorBox cache response: {e}")
    return cached_hashes


# ==========================================
# TMDB & TRACKER DISCOVERY
# ==========================================
def get_tmdb_trending(session: requests.Session, media_type: str, target_count: int = 50) -> List[dict]:
    """Fetches trending movies or TV series from TMDB."""
    results = []
    page = 1
    while len(results) < target_count and page <= 5:
        url = f"{TMDB_BASE_URL}/trending/{media_type}/week"
        res = safe_http_get(session, url, params={"api_key": TMDB_API_KEY, "page": page})
        if not res:
            break
        data = res.json()
        page_results = data.get("results", [])
        if not page_results:
            break
        results.extend(page_results)
        page += 1
    return results[:target_count]


def get_imdb_id(session: requests.Session, tmdb_id: int, media_type: str) -> Optional[str]:
    """Translates TMDB ID to IMDb ID for Torrentio querying."""
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

    # Session configured with browser impersonation
    session = requests.Session(impersonate="chrome")
    session.headers.update({"Authorization": f"Bearer {TORBOX_API_KEY}"})

    logger.info(f"--- RUNNING TORBOX AUTOMATION PIPELINE (TEST_RUN = {TEST_RUN}) ---")

    # Step 1: Map existing TorBox library to avoid duplicates
    existing_episodes, existing_movies = get_existing_torbox_items(session)
    logger.info(f"Existing TorBox items indexed: {len(existing_movies)} movies, {len(existing_episodes)} episodes.")

    # Step 2: Fetch trending targets from TMDB
    trending_movies = get_tmdb_trending(session, "movie", target_count=40)
    trending_shows = get_tmdb_trending(session, "tv", target_count=20)
    logger.info(f"TMDB returned {len(trending_movies)} movie targets and {len(trending_shows)} TV series targets.")

    movie_candidates = []
    movie_diagnostics = []

    # Step 3: Gather Movie Candidates from Torrentio
    logger.info("Querying Torrentio for film releases...")
    for movie in trending_movies:
        title = movie.get("title", "")
        release_date = movie.get("release_date", "")
        year = release_date[:4] if release_date else ""
        tmdb_id = movie.get("id")
        mov_key = extract_movie_key(f"{title} {year}")

        if not title or not mov_key:
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
        time.sleep(1.0)  # Throttling rate to prevent Torrentio block

        if res and res.status_code == 200:
            streams = res.json().get("streams", [])
            diag["streams_found"] = len(streams)
            valid_found = []

            for stream in streams:
                t_hash = stream.get("infoHash", "").lower()
                details = stream.get("title", "")

                if not is_valid_release(details):
                    continue

                score = calculate_quality_score(details)
                if t_hash and score > 0:
                    candidate = {
                        "key": mov_key,
                        "hash": t_hash,
                        "magnet": f"magnet:?xt=urn:btih:{t_hash}&dn={quote(title)}",
                        "score": score,
                        "title": f"{title} ({year}) [{details.splitlines()[0]}]"
                    }
                    movie_candidates.append(candidate)
                    valid_found.append(candidate)

            if not valid_found:
                diag["status"] = "REJECTED"
                diag["reason"] = "No valid 1080p/2160p releases found"
            else:
                diag["candidates"] = valid_found
            movie_diagnostics.append(diag)

    # Step 4: Gather TV Series Candidates from Torrentio
    logger.info("Querying Torrentio for TV series releases...")
    tv_candidates = []
    for show in trending_shows:
        show_title = show.get("name", "")
        tmdb_id = show.get("id")
        imdb_id = get_imdb_id(session, tmdb_id, "tv")

        if not imdb_id:
            continue

        # Fetch S01E01 as baseline test for series availability
        res = safe_http_get(session, f"{TORRENTIO_BASE_URL}/series/{imdb_id}:1:1.json")
        time.sleep(1.0)

        if res and res.status_code == 200:
            for stream in res.json().get("streams", []):
                t_hash = stream.get("infoHash", "").lower()
                details = stream.get("title", "")

                if not is_valid_release(details):
                    continue

                score = calculate_quality_score(details)
                ep_info = extract_episode_info(details)

                if ep_info and score > 0 and t_hash:
                    series_key, ep_id = ep_info
                    full_ep_key = f"{series_key}_{ep_id}"

                    if full_ep_key not in existing_episodes:
                        tv_candidates.append({
                            "series_key": series_key,
                            "ep_id": ep_id,
                            "full_key": full_ep_key,
                            "hash": t_hash,
                            "magnet": f"magnet:?xt=urn:btih:{t_hash}&dn={quote(show_title)}",
                            "score": score,
                            "title": details.splitlines()[0]
                        })

    # Step 5: Batch Check Hash Caching on TorBox
    all_hashes = list({c["hash"] for c in movie_candidates + tv_candidates})
    logger.info(f"Pinging TorBox cache endpoint for {len(all_hashes)} candidate hashes...")
    cached_map = check_torbox_cache(session, all_hashes)

    # Step 6: Select Highest Scoring Cached Releases
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
            best = max(cached_options, key=lambda x: x["score"])
            diag["status"] = "SELECTED"
            diag["selected_release"] = best
            selected_movies[diag["key"]] = best

    final_movies = list(selected_movies.values())[:MAX_MOVIES]

    selected_tv = {}
    for c in tv_candidates:
        if not cached_map.get(c["hash"]):
            continue
        full_key = c["full_key"]
        if full_key not in selected_tv or c["score"] > selected_tv[full_key]["score"]:
            selected_tv[full_key] = c

    final_tv_payloads = list(selected_tv.values())[:MAX_TV_SERIES]

    # Step 7: Output Auditing Summary & Execution
    logger.info("==========================================")
    logger.info("MOVIE AUDIT BREAKDOWN:")
    for diag in movie_diagnostics:
        status = diag["status"]
        title = f"{diag['title']} ({diag['year']})"
        if status == "SELECTED":
            rel = diag["selected_release"]
            logger.info(f"  [SELECTED] {title} -> {rel['title']}")
        elif status == "SKIPPED":
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
            except Exception as e:
                logger.error(f"Error adding {item['title']}: {e}")

if __name__ == "__main__":
    main()