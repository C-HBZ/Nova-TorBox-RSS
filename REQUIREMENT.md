# TorBox → Nova Player Automated Content Pipeline Requirements

## 1. Goal
Use TorBox via WebDAV as the content source for Nova Player on Android TV, ensuring the Nova library is refreshed multiple times per day with the newest, highest‑quality English‑targeted movies and TV shows aimed at audiences in UK, USA, Australia and Canada.

Foreign-made titles are acceptable when they are English‑language compatible, but the shortlist must prioritise content aimed at English-speaking markets.

## 2. Source of Truth
All media discovery and validation must use live Stremio/Torrentio stream JSON payloads as the authoritative source of:

- Availability
- Quality markers
- Release identity
- Audio tracks
- Torrent metadata

The Comet addon manifest is **NOT** a content source and must never be used for stream discovery.

## 3. Content Scope
- Focus on recent releases, ideally within the last 24 months.
- Prioritise new movie releases and newly launched or currently active TV series.
- Exclude pornography and any clearly inappropriate or non‑family‑safe content.
- Only include content that is English-language or has English audio/title paths.

## 4. Quality Rules
Mandatory priority:

- 2160p must be selected whenever available.
- 1080p is allowed only when no valid 2160p release exists.
- Reject anything below 1080p.

Reject all low-quality sources:

- CAM
- Screener
- Workprint
- Telecine
- Telesync
- Any obviously junk or pre-release material

Prefer strong audio tracks:

- DTS-HD
- TrueHD
- Atmos
- Dolby Digital
- DTS

Avoid foreign-dub-only releases unless they still contain English audio.

## 5. Movie Requirements
- Maintain a rolling set of the newest 30 movies that pass all filters.
- The script must discover more than 30 candidates, because some titles will be unavailable or invalid.
- Select the 30 most recent valid films each run.
- Update the movie list every refresh cycle.

## 6. TV Show Requirements
- Maintain a rolling set of the newest 20 distinct TV series (the cap applies to the count of different shows, not the number of episodes added per run).
- Only include newly launched or currently active series; avoid older archive content or long‑finished shows.
- For each tracked series, track only its current/latest season. Do not retain or add episodes from an older season once a newer season exists for that series.

Each refresh cycle must:

- Detect all newly available, cached, valid episodes belonging to the current season of each of the 20 tracked series (not capped per run — a single series may contribute multiple new episodes in one cycle).
- Add them to the existing series entry.
- When a series advances to a new season, remove the previous season's episodes from TorBox so only the current season remains for that series.
- When a series falls out of the newest 20 (superseded by more recently active shows), remove all of its episodes from TorBox.

**Episode discovery — query per-episode like Stremio/Torrentio clients do**

Torrentio's stream endpoint is scoped to one specific season+episode per request (`/series/{imdbId}:{season}:{episode}.json`), the same way a Stremio client only asks for the episode a viewer is currently browsing. To collect *all* aired episodes of a series' current season — not just episode 1 — the script must query the endpoint once per aired episode number (from TMDB's `last_episode_to_air`, which also tells us how many episodes of the current season have aired so far), skipping the request entirely when that episode is already present in TorBox. This is capped at a sane maximum per show per run to bound request volume.

**Extra correctness measure — standard SxxEyy filename matching**

Torrent/release *display* names are frequently ambiguous or localized (e.g. a bare `S01` season-pack label, or a translated "Season 1 / Episodes 1-2 of 8" summary line), which is not reliable enough to identify an exact season/episode. The underlying per-file filename that TorBox/Torrentio resolves to, however, reliably follows the international `SxxEyy` filename convention (e.g. `Show.Name.S01E01.2160p...mkv`). The script must prefer that per-file filename (or the first line of the stream metadata that actually matches `SxxEyy`) over the noisier top-level release name when identifying season/episode and matching a stream to its series — only falling back to the release name if no `SxxEyy` match is found anywhere. This avoids miscounting season packs as single episodes and avoids treating alternate-language/regional releases of the same episode as distinct entries.

## 7. Library Management (TorBox)
- TorBox must be accessed via WebDAV for Nova Player compatibility.
- The script must ensure Nova Player's GUI remains correctly populated by maintaining a Nova‑compatible folder structure.
- Before queueing any item, the script must check TorBox to avoid:
  - Re-adding cached items
  - Duplicates
  - Redundant downloads

Deduplication must use real metadata:

- IMDb ID
- Series identity
- Torrent hash
- Release identity

TorBox must always contain:

- 20 newest TV series
- 30 newest movies

Older content must be deleted or replaced automatically.

## 8. Processing Flow
The script must follow this exact pipeline:

1. TMDB discovery
2. Metadata lookup
3. Live Stremio/Torrentio stream query
4. Quality filtering
5. English‑audience targeting filter
6. TorBox cache validation
7. Queue selection
8. WebDAV sync to Nova Player

This flow must be used for every refresh cycle, multiple times per day.

## 9. Operational Notes
- WebDAV is the user-facing storage layer for Nova Player.
- The scraper logic must operate entirely from torrent/debrid stream metadata, not filenames.
- The system must be capable of continuous updates as new titles become available throughout the day.

---

## Appendix: Operational and Developer Notes (Required)

**Environment and secrets**
- **API_TORBOX** — TorBox API/endpoint for torrent caching (inject via environment variable).
- **TMDB_API_KEY** — TMDB API key (env var).
- **DEBRID_KEYS** — Any debrid provider keys (env vars).
- **Do not commit** any keys or secrets to source control.

**Refresh cadence**
- Run **minimum 3 times per day**; configurable schedule (cron) with jitter to avoid upstream spikes.

**Rate limits and backoff**
- Respect upstream rate limits; implement exponential backoff and retry with capped attempts.
- Log and alert on repeated 429/5xx responses.

**Manifest handling**
- Persist manifest `version` and checksum on each run.
- Validate manifest JSON against an expected schema; fail fast and alert on schema changes.

**Stream JSON schema**
- Expect and parse: `quality`, `resolution`, `size`, `audio`, `audioCodec`, `audioLanguages`, `hash`, `torrentId`, `releaseName`, `releaseGroup`, `seeders`, `leechers`, `provider`, `language`, `subtitleLanguages`, `magnet`/`torrentUrl`.
- Fail selection if required fields are missing.

**Retention and pruning**
- TorBox must retain exactly **20 TV series** and **30 movies**; deletion decisions must use canonical IDs and torrent hashes.
- Maintain a deletion audit log (timestamp, ID, hash, reason).

**Nova GUI compatibility**
- Document and enforce the WebDAV folder layout required by Nova Player; include an example folder tree in the repo.

**Monitoring and alerts**
- Emit metrics for: manifest changes, candidate counts, selected counts, TorBox cache hits, deletions, failed downloads, and refresh durations.
- Alert on: manifest schema changes, repeated selection failures, TorBox auth failures, or when fewer than target items are selected.

**Testing and QA**
- Provide a test harness that can run discovery → stream query → selection → dry‑run TorBox sync without writing to WebDAV.
- Include sample stream JSON fixtures and unit tests for the comparator and dedupe logic.

**Security and compliance**
- Use least privilege for any API keys.
- Rotate keys periodically and document rotation steps.
- Ensure no user PII is logged.

**Change log and contact**
- Maintain a `CHANGELOG.md` for requirement and manifest‑driven behavior changes.
- Provide a primary contact (name/email) for upstream manifest issues and emergency rollbacks.

**Operational runbook**
- Include step‑by‑step recovery instructions for: manifest break, TorBox outage, mass deletion rollback, and rate‑limit incidents.
