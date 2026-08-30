Nova TorBox RSS
A pragmatic Python automation pipeline that discovers recent, English-market-friendly content, checks the live Torrentio/Stremio stream metadata, validates quality, and then checks whether the relevant hashes are already cached in TorBox before selecting what to queue.

What the app does now
pulls recent titles from TMDB (movies + TV) via three merged discovery passes: newly-launched shows, currently-active returning shows (so long-running series aren't missed just for being "old"), and a minimal JustWatch "new" discovery set
uses the personalized Torrentio manifest config (sort/qualityfilter/limit/debridoptions/torbox) from manifest.txt instead of the generic public endpoint
excludes daily soap operas, News and Talk shows (TMDB genres) from every TV discovery path, so a single long-running soap season can't crowd out scripted shows on popularity alone
sorts the merged TV candidate pool by genuine TMDB popularity/rating before evaluation, so well-regarded returning shows compete fairly for the 20 series slots against a flood of brand-new season-1 releases
resolves the real per-file SxxEyy filename (not the noisier top-level release name) to identify season/episode correctly and avoid ambiguous/duplicate entries
queries Torrentio per-episode across a show's currently airing season (up to 30 aired episodes), skipping episodes already present in TorBox
identifies TV series by their canonical TMDB id (not parsed release-name text), using a small persistent local index (series_index.json) so the same show uploaded under different naming conventions is never tracked or pushed twice
applies a quality gate: prefer 2160p, fall back to 1080p, reject anything lower; rejects CAM/screener/junk releases and overly foreign-only packs unless they still show English-compatible metadata
adds a small audio-codec tie-break bonus (TrueHD/DTS-HD > Atmos/DTS/Dolby Digital) when candidates otherwise tie on resolution
detects TorBox cache status directly from Torrentio's `[TB+]`/`[TB download]` label, with a `/checkcached` API call as a fallback
runs Torrentio lookups for movies and shows concurrently (a small worker pool) instead of one at a time, cutting run time roughly 3x
caps selection at 30 movies and 20 distinct TV series per run (episodes per series are not capped)
deduplicates the final push list by torrent hash, so a single season-pack file covering many episodes isn't submitted to TorBox once per episode
prunes the TorBox library to keep only the newest 30 movies / 20 series, and removes a tracked series' older-season episodes once it advances to a new season
writes its own fresh latest-run.log each run (overwritten, not appended) and prints live progress with a calibrated time estimate, so a long run doesn't look hung
logs a full movie + TV audit breakdown so selected/skipped/rejected reasoning is visible
runs in TEST_RUN = True by default so it can be validated without live pushes
Source of truth
The current implementation is intentionally aligned with the working upstream Stremio/Torrentio flow rather than custom filename-only heuristics. It treats the live JSON manifests and stream payloads as the real metadata source, not filenames alone.

Required behavior
The project aims to do the following:

prefer recent titles released within the last 24 months
bias toward newer releases and both newly launched and currently active TV series, without daily soap operas crowding them out
keep the library focused on English-language-market content without fully excluding valid foreign-friendly releases
reject pornography and clearly non-family-safe content
keep the library refreshed with a rolling set of the newest valid 30 movies and 20 TV series (latest season only per series)
avoid duplicates and re-adding already-cached items, using canonical show identity rather than parsed filenames
Current validation status
The app has been validated in test mode with a fresh run. The script compiles successfully and the latest run logs indicate:

TMDB + JustWatch new results: 63 movies and 123 series after merge.
SELECTION SUMMARY: 30 Movies, 20 TV Series (221 new episodes) queued.
Full run completed in ~7.75 minutes with parallelized Torrentio querying (previously 20+ minutes serially at a similar pool size).
Dry-run pruning correctly identified 17 stale torrents beyond the 30 movie / 20 series rolling cap.
This is a working prototype that is good enough for ongoing tuning and validation, but it is not yet a final production-grade audience filter. Some niche or mixed-language titles still make it through the stream selection layer, which is acceptable for a pragmatic next-step implementation while the heuristics are refined.

Minimal tuning strategy
The current design keeps the app simple and avoids a redesign. The practical approach is:

discovery using TMDB (newly-launched + currently-active passes) plus a minimal JustWatch "new" source
popularity gating and genre exclusion to keep the pool mainstream and scripted
real stream metadata validation
TorBox cache validation before queueing
only small refinements to ranking and audience filters when needed
This mirrors the kind of lightweight discovery logic used by apps such as Stremio/Torrentio and keeps the project close to the actual live data model.

Configuration
The workflow expects these secrets:

API_TORBOX
API_TMDB
Keep TEST_RUN = True while validating, and only switch to False when live queueing is intentionally required.

Local run
python3 -m py_compile main.py
python3 main.py
(the script writes its own latest-run.log each run; no shell redirection needed)

Notes
manifest.txt contains the working upstream manifest URLs.
REQUIREMENT.md captures the current product requirement and intent.
generated files like latest-run.log and series_index.json are not intended to be committed to the repo.

To Do
- [x] Track 20 distinct TV series per run (not 20 total episodes); track only each series' latest season and auto-remove older-season episodes once a show advances.
- [x] Query Torrentio per-episode across a show's aired episodes (not just episode 1), skipping episodes already in TorBox.
- [x] Prefer the real per-file SxxEyy filename over ambiguous/localized release names for season/episode identification.
- [x] Use the canonical TMDB id (not parsed release-name text) as TV series identity, via a persistent local hash index, to fully collapse alternate-naming releases of the same show (e.g. "Law & Order: SVU" vs "Law and Order Special Victims Unit") into one series entry.
- [x] Raise MAX_NEW_EPISODES_PER_SHOW to 30 to cover higher-episode-count scripted shows (procedurals, animated sitcoms) without truncation.
- [x] Exclude daily soap operas (and News/Talk) from all TV discovery paths.
- [x] Parallelize Torrentio calls (concurrent worker pool) for a ~3x runtime speedup.
- [x] Sort the TV candidate pool by popularity/rating so established shows compete fairly against new season-1 releases for the 20 series slots.
- [x] Self-managed log file, live progress heartbeats, and a calibrated time estimate.
- [x] Deduplicate the final push list by torrent hash (season-pack collapsing).
- [ ] Add a GitHub Actions workflow to run the pipeline multiple times per day (deferred for now).
- [ ] Persist a deletion audit log (timestamp, id, hash, reason) for TorBox pruning.
- [ ] Persist manifest.txt version/checksum and validate its schema before each run.
- [ ] Add a lightweight test harness (fixtures + unit tests) for the dedupe/comparator logic.
- [ ] Validate discovery coverage against real "new releases" sources (e.g. JustWatch/TMDB new & trending) to check for popularity-threshold gaps.
- [ ] Consider early-exit episode scanning (stop checking further episode numbers after several consecutive empty results) to save time — deferred pending explicit sign-off, since it risks missing later valid episodes if a show has a temporary mid-season caching gap.
- [ ] Flip TEST_RUN to False once a few more clean test runs have been reviewed.
