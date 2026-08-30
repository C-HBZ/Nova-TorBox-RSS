Nova TorBox RSS
A pragmatic Python automation pipeline that discovers recent, English-market-friendly content, checks the live Torrentio/Stremio stream metadata, validates quality, and then checks whether the relevant hashes are already cached in TorBox before selecting what to queue.

What the app does now
pulls recent titles from TMDB (movies + TV), merged with a minimal JustWatch "new" discovery set
uses the personalized Torrentio manifest config (sort/qualityfilter/limit/debridoptions/torbox) from manifest.txt instead of the generic public endpoint
resolves the real per-file SxxEyy filename (not the noisier top-level release name) to identify season/episode correctly and avoid ambiguous/duplicate entries
queries Torrentio per-episode across a show's currently airing season (not just episode 1), skipping episodes already present in TorBox
applies a quality gate: prefer 2160p, fall back to 1080p, reject anything lower; rejects CAM/screener/junk releases and overly foreign-only packs unless they still show English-compatible metadata
adds a small audio-codec tie-break bonus (TrueHD/DTS-HD > Atmos/DTS/Dolby Digital) when candidates otherwise tie on resolution
detects TorBox cache status directly from Torrentio's `[TB+]`/`[TB download]` label, with a `/checkcached` API call as a fallback
caps selection at 30 movies and 20 distinct TV series per run (episodes per series are not capped)
prunes the TorBox library to keep only the newest 30 movies / 20 series, and removes a tracked series' older-season episodes once it advances to a new season
logs a full movie + TV audit breakdown so selected/skipped/rejected reasoning is visible
runs in TEST_RUN = True by default so it can be validated without live pushes
Source of truth
The current implementation is intentionally aligned with the working upstream Stremio/Torrentio flow rather than custom filename-only heuristics. It treats the live JSON manifests and stream payloads as the real metadata source, not filenames alone.

Required behavior
The project aims to do the following:

prefer recent titles released within the last 24 months
bias toward newer releases and newly launched TV series
keep the library focused on English-language-market content without fully excluding valid foreign-friendly releases
reject pornography and clearly non-family-safe content
keep the library refreshed with a rolling set of the newest valid 30 movies and 20 TV series (latest season only per series)
avoid duplicates and re-adding already-cached items
Current validation status
The app has been validated in test mode with a fresh run. The script compiles successfully and the latest run logs indicate:

TMDB + JustWatch new results: 64 movies and 73 series after merge.
SELECTION SUMMARY: 30 Movies, 20 TV Series (95 new episodes) queued.
Dry-run pruning correctly identified 17 stale torrents beyond the 30 movie / 20 series rolling cap.
This is a working prototype that is good enough for ongoing tuning and validation, but it is not yet a final production-grade audience filter. Some niche or mixed-language titles still make it through the stream selection layer, which is acceptable for a pragmatic next-step implementation while the heuristics are refined.

Minimal tuning strategy
The current design keeps the app simple and avoids a redesign. The practical approach is:

discovery using TMDB plus a minimal JustWatch "new" source
popularity gating to keep the pool more mainstream
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
python3 main.py 2>&1 | tee latest-run.log
Notes
manifest.txt contains the working upstream manifest URLs.
REQUIREMENT.md captures the current product requirement and intent.
generated files like latest-run.log are not intended to be committed to the repo.

To Do
- [x] Track 20 distinct TV series per run (not 20 total episodes); track only each series' latest season and auto-remove older-season episodes once a show advances.
- [x] Query Torrentio per-episode across a show's aired episodes (not just episode 1), skipping episodes already in TorBox.
- [x] Prefer the real per-file SxxEyy filename over ambiguous/localized release names for season/episode identification.
- [ ] Use IMDb ID (not parsed release-name text) as the canonical TV series identity, to fully collapse alternate-region/language releases of the same show (e.g. "Blodsoffer" vs "Sacrifice de Sang") into one series entry.
- [ ] Add a GitHub Actions workflow to run the pipeline multiple times per day (deferred for now).
- [ ] Persist a deletion audit log (timestamp, id, hash, reason) for TorBox pruning.
- [ ] Persist manifest.txt version/checksum and validate its schema before each run.
- [ ] Add a lightweight test harness (fixtures + unit tests) for the dedupe/comparator logic.
- [ ] Validate discovery coverage against real "new releases" sources (e.g. JustWatch/TMDB new & trending) to check for popularity-threshold gaps.
- [ ] Flip TEST_RUN to False once a few more clean test runs have been reviewed.
