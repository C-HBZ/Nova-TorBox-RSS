# Nova TorBox RSS

A pragmatic Python automation pipeline that discovers recent, English-market-friendly content, checks the live Torrentio/Stremio stream metadata, validates quality, and then checks whether the relevant hashes are already cached in TorBox before selecting what to queue.

## What the app does now

- pulls recent titles from TMDB
- merges a minimal JustWatch "new" discovery set into the TMDB pool
- queries Torrentio/ Stremio-style stream payloads for actual release metadata
- applies a simple quality gate: prefer 2160p, fall back to 1080p, reject anything lower
- rejects obvious junk releases and overly foreign-only packs unless they still show English-compatible metadata
- checks TorBox cache before deciding what should be selected
- logs audit details so the user can see which titles were selected or rejected
- runs in `TEST_RUN = True` by default so it can be validated without live pushes

## Source of truth

The current implementation is intentionally aligned with the working upstream Stremio/Torrentio flow rather than custom filename-only heuristics. The project does not treat the Comet addon descriptor as a content source; it treats the live JSON manifests and stream payloads as the real metadata source.

## Required behavior

The project aims to do the following:

- prefer recent titles released within the last 24 months
- bias toward newer releases and newly launched TV series
- keep the library focused on English-language-market content without fully excluding valid foreign-friendly releases
- reject pornography and clearly non-family-safe content
- keep the library refreshed with a rolling set of the newest valid 25 movies and 20 TV items
- avoid duplicates and re-adding already-cached items

## Current validation status

The app has been validated in test mode with a fresh run. The script compiles successfully and the current run logs indicate:

- `TMDB + JustWatch new results: 62 movies and 72 series after merge.`
- `SELECTION SUMMARY: 30 Movies, 20 TV Episodes queued.`

This is a working prototype that is good enough for ongoing tuning and validation, but it is not yet a final production-grade audience filter. Some niche or mixed-language titles still make it through the stream selection layer, which is acceptable for a pragmatic next-step implementation while the heuristics are refined.

## Minimal tuning strategy

The current design keeps the app simple and avoids a redesign. The practical approach is:

1. discovery using TMDB plus a minimal JustWatch "new" source
2. popularity gating to keep the pool more mainstream
3. real stream metadata validation
4. TorBox cache validation before queueing
5. only small refinements to ranking and audience filters when needed

This mirrors the kind of lightweight discovery logic used by apps such as Stremio/Torrentio and keeps the project close to the actual live data model.

## Configuration

The workflow expects these secrets:

- `API_TORBOX`
- `API_TMDB`

Keep `TEST_RUN = True` while validating, and only switch to `False` when live queueing is intentionally required.

## Local run

```bash
python3 -m py_compile main.py
python3 main.py 2>&1 | tee latest-run.log
```

## Notes

- `manifest.txt` contains the working upstream manifest URLs.
- `REQUIREMENT.txt` captures the current product requirement and intent.
- generated files like `latest-run.log` are not intended to be committed to the repo.
