# Nova TorBox RSS

A Python-based Torrentio discovery and TorBox automation pipeline. The current script runs in test mode, queries Torrentio for candidate releases, filters by quality, and prints a report without adding anything to TorBox.

## Current Status

The latest test run correctly identifies and rejects unreleased 2026 films lacking high-definition sources, such as *Avengers: Doomsday* and *Moana*. However, the pipeline still has serious issues with quality control, release-date parameters, localisation, and item deduplication.

## Next Review: Known Issues

### Quality and Formatting Failures

- **Sub-standard sources:** The script allows low-tier scene releases through the filter. *Insidious: Out of the Further* was queued as an `HDTC` (high-definition telecine), which is a theatre-recorded rip. *Spider-Man: Brand New Day* was queued as a `Pre` (pre-release or screener). Neither meets a true retail 1080p/2160p standard.
- **Metadata mismatches:** The parser incorrectly matched the 2025 promotional short *The Odyssey - Prologue* to the main 2026 feature film of the same name.
- **Localisation blindness:** The selection logic grabs foreign dubs instead of clean English audio. Examples include Polish voiceovers (`Lektor i Napisy PL`), Russian dubs, and Hindi audio streams. The pipeline needs to prioritise primary English tracks.

### Newness and Logic Failures

- **Catalogue creep:** The film logic targets 2026 releases, but the TV module scrapes older library content. *Reacher* (2022), *Silo* (2023), and *Dark Matter* (2024) violate the newness requirement.
- **Zero deduplication:** The script queued 20 TV episodes containing redundant hashes. It attempted to push eight tracker releases for *Dark Matter* Season 1 and six for *Silo* Season 1 simultaneously, which would unnecessarily fill the TorBox queue.

## To Do For Next Time

1. Implement strict negative regex filters to block undesirable source tags such as `HDTC`, `CAM`, `TS`, `Pre`, `HC`, and `KORSUB`.
2. Add a language parser that prioritises clean UK releases, or US primary streams, and aggressively filters European regional dubs such as Polish, Russian, and French `VF2`, along with international audio such as Hindi unless explicitly requested.
3. Enforce a hard release-year boundary of `>= 2026` for TV Torrentio queries so older programmes are not archived.
4. Add final-array deduplication so only one optimal, highest-weighted hash is sent to the TorBox cache endpoint per title or season.

## Configuration

The GitHub Actions workflow expects these repository secrets:

- `API_TORBOX`
- `API_TMDB` when TMDB-backed discovery is enabled

Keep `TEST_RUN = True` while evaluating changes. Set it to `False` only when live TorBox additions are intentionally required.

## Running Locally

```bash
python3 main.py
```

The local run log can be captured with:

```bash
python3 main.py 2>&1 | tee latest-run.log
```

Generated Python cache files and `latest-run.log` are ignored by Git.
