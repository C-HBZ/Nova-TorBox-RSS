import time
import re
import logging
import sys
from typing import List, Dict
from curl_cffi import requests

# ---------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------
TEST_MODE = True  # Set to False for production runs
# ---------------------------------------------------------

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

BLACKLIST_REGEX = re.compile(
    r"(?i)\b(cam|ts|hdcam|hdtc|hq\s?pre|pre|screener|tc|telesync|dvdscr|telecine|hc|kor)\b"
)

# Initialise a persistent session that mimics a real browser to avoid 403s
http_session = requests.Session(impersonate="chrome")

def deduplicate_queue(media_list: List[Dict]) -> List[Dict]:
    seen = set()
    unique = []
    for item in media_list:
        if item['imdb_id'] not in seen:
            unique.append(item)
            seen.add(item['imdb_id'])
    return unique

def fetch_torrentio(imdb_id: str, media_type: str = "movie", season: int = None, episode: int = None) -> List[Dict]:
    base_url = "https://torrentio.strem.fun/stream"
    
    if media_type == "movie":
        url = f"{base_url}/movie/{imdb_id}.json"
    else:
        url = f"{base_url}/series/{imdb_id}:{season}:{episode}.json"

    max_retries = 4
    for attempt in range(max_retries):
        try:
            response = http_session.get(url, timeout=10)
            if response.status_code == 429:
                wait_time = (2 ** attempt) + 1
                logging.warning(f"Rate limited (429) on {imdb_id}. Backing off for {wait_time}s...")
                time.sleep(wait_time)
                continue
            
            # Catch 403s specifically so they don't crash the script silently
            if response.status_code == 403:
                logging.error(f"HTTP 403 Forbidden on {imdb_id} - Bot protection blocking request.")
                return []
                
            response.raise_for_status()
            time.sleep(1.2) 
            return response.json().get("streams", [])
            
        except requests.exceptions.RequestException as e:
            logging.error(f"Network error on {imdb_id}: {e}")
            time.sleep(2)
            
    return []

def rank_and_filter_streams(streams: List[Dict]) -> List[Dict]:
    valid_streams = []
    for stream in streams:
        title = stream.get("title", "")
        title_lower = title.lower()
        
        if BLACKLIST_REGEX.search(title):
            continue
            
        if "2160p" in title_lower or "4k" in title_lower:
            score = 3
        elif "1080p" in title_lower:
            score = 2
        else:
            continue 
            
        if score == 3 and ("hdr" in title_lower or "dv" in title_lower or "dovi" in title_lower):
            score += 0.5

        valid_streams.append({"stream": stream, "score": score})
        
    valid_streams.sort(key=lambda x: x["score"], reverse=True)
    return [s["stream"] for s in valid_streams]

def execute_pipeline(media_queue: List[Dict], is_test: bool) -> Dict:
    clean_queue = deduplicate_queue(media_queue)
    logging.info(f"Initiating run for {len(clean_queue)} targets. (TEST MODE: {is_test})")

    results = {}
    for item in clean_queue:
        imdb_id = item['imdb_id']
        m_type = item.get('type', 'movie')
        
        if m_type == "movie":
            streams = fetch_torrentio(imdb_id, "movie")
        else:
            streams = fetch_torrentio(imdb_id, "series", item.get("season", 1), item.get("episode", 1))

        best_streams = rank_and_filter_streams(streams)
        
        if best_streams:
            best_match = best_streams[0]
            results[imdb_id] = best_match
            release_name = best_match.get('title', 'Unknown').split('\n')[0]
            logging.info(f"Acquired: [{imdb_id}] -> {release_name}")
        else:
            logging.info(f"Rejected: [{imdb_id}] -> No valid 1080p/2160p scene releases found.")
            results[imdb_id] = None

    return results

def get_production_queue() -> List[Dict]:
    """Placeholder for your live data ingestion logic."""
    logging.info("Fetching production queue...")
    return []

if __name__ == "__main__":
    if TEST_MODE:
        logging.info("Running in TEST MODE using static payload.")
        payload = [
            {"imdb_id": "tt15239678", "type": "movie"}, # Dune: Part Two
            {"imdb_id": "tt15239678", "type": "movie"}, # Duplicate injection
            {"imdb_id": "tt0903747", "type": "series", "season": 5, "episode": 14} # Breaking Bad
        ]
    else:
        logging.info("Running in PRODUCTION MODE.")
        payload = get_production_queue()
        if not payload:
            logging.error("Production queue is empty. Exiting.")
            sys.exit(1)
            
    final_output = execute_pipeline(payload, TEST_MODE)