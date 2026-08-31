⚠️ Prototype heavily produced by AI, not ready for general use ⚠️

# check-cloud-ready

A deterministic CLI that assesses whether an Earth-systems dataset is **cloud
ready**: on object storage, randomly accessible via HTTP range requests,
sensibly chunked, well-compressed, and standards-conformant (CF / GeoZarr). It
produces a verdict-first scorecard (`assessment-report.md` + machine-readable
`findings.json`) with concrete, prioritized recommendations for the data
provider — every failing/warning check carries a remediation, often an exact
command.

The four criteria the whole assessment hangs off:

1. **On the cloud** — object storage (S3/GCS/Azure), not only a bespoke download
   portal.
2. **Randomly accessible** — subsets fetchable via HTTP range requests, not
   whole-file downloads.
3. **Well-chunked** — measured compressed chunk size in the ~8–16 MB sweet spot
   (see `.claude/skills/check-cloud-ready/references/chunking.md`), plus a
   sensible chunk *orientation* for the stated use case.
4. **Standards-conformant** — CF conventions, and GeoZarr for Zarr stores.

Supported today via a real opener: HDF5, NetCDF-4 (refined from HDF5), Zarr v2/v3,
Icechunk, COG, Parquet/GeoParquet (see the format reference docs for current
caveats on the COG/Parquet chunk-size grade). Recognized-but-hosting-only:
NetCDF-3, HDF4, GRIB2, shapefile, CSV, zip/tar (cloud-hostile — always graded,
always low, with a mandatory migration command). Not yet a supported opener:
COPC, FlatGeobuf, PMTiles. Out of scope entirely: STAC catalogs/API search,
Croissant/GeoCroissant descriptors — this CLI assesses one asset entrypoint (a
path, URL, or NASA CMR granule ID) per run, never a catalog crawl.

## Install

```bash
pip install -e '.[all]'
```

Or install only the extras you need:

| Extra | Adds | Needed for |
|---|---|---|
| `s3` | `s3fs`, `boto3` | anonymous/legacy S3 access, S3 probing |
| `hdf5` | `h5py` | opening HDF5/NetCDF-4 |
| `zarr` | `zarr>=3`, `numcodecs` | opening Zarr/Icechunk, the compression benchmark |
| `raster` | `rasterio`, `rio-cogeo` | opening/validating COGs |
| `parquet` | `pyarrow` | opening Parquet/GeoParquet |
| `nasa` | `earthaccess` | the `--earthaccess-fallback` credential path |
| `llm` | `anthropic` | `--suggest llm` variable suggestion |
| `cf` | `xarray` | (reserved; the built-in CF check needs no extra deps) |

`pip install -e '.[zarr,s3]'` combines any subset. Every check degrades to
`SKIPPED` with a "pip install X" reason when its extra is missing — the CLI never
crashes for an optional dependency.

## Quickstart

**Interactive** (a human at a terminal — the CLI prompts progressively):

```
$ check-cloud-ready s3://some-bucket/dataset.zarr
Detected: zarr. Accept? [Y / type one of: cog, hdf5, hdf4, icechunk, ...] [y]:
0  /sst  (time,lat,lon)×(5844,1023,2047)  float32
1  /analysed_sst  (time,lat,lon)×(5844,1023,2047)  float32
Select variable(s) to assess (indices/names, comma-separated, 'all', or '?' to suggest) [analysed_sst]:
Run compression codec trials? Downloads ~1 sampled chunk [y/N]: y
Report style: [verdict/score/both] [both]:
**Verdict: READY WITH CAVEATS — no consumer-blocking failures, but some checks warn/partial**
Tier B · 82/100
Report written to cloud-readiness-dataset.zarr/assessment-report.md
```

**Non-interactive** (CI, or an agent driving the CLI):

```bash
check-cloud-ready s3://some-bucket/dataset.zarr \
  --non-interactive --report-style both --out ./out \
  --all-variables --benchmark --use-case timeseries --json
```

`--non-interactive` is also implied automatically whenever stdin isn't a TTY.

## Flags

| Flag | Purpose |
|---|---|
| `INPUT` (positional) | Local path, `s3://`/`https://` URL, or CMR granule ID. |
| `--granule-id` | Resolve access via a CMR granule ID. |
| `--credentials-url` | An explicit DAAC `/s3credentials` endpoint. |
| `--earthaccess-fallback` | Opt into `earthaccess`-minted temporary S3 credentials. |
| `--anon` | Force anonymous S3 access. |
| `--format` | Override auto-detected format (default `auto`). |
| `--variables` | Comma-separated names/paths/substrings to assess. |
| `--all-variables` | Assess every variable in the inventory. |
| `--suggest {heuristic,llm}` | How to suggest a variable when none is given. |
| `--llm-model` | Model for `--suggest llm` (default `claude-sonnet-5`). |
| `--benchmark` / `--no-benchmark` | Run the empirical compression codec grid. |
| `--bitround-max-abs-error` | Also benchmark a lossy BitRound variant targeting this absolute error. |
| `--use-case {timeseries,maps,balanced}` | Which chunk-orientation query gets graded. |
| `--report-style {verdict,score,both}` | Report content (verdict-only, score-only, or both). |
| `--out` | Output directory (default `cloud-readiness-<input-basename>/`). |
| `--non-interactive` | Never prompt; use defaults or fail with a named flag to pass instead. |
| `--no-network` | Skip every access probe/remote resolution step. |
| `--byte-cap` / `--time-cap` | Transfer/wall-clock budget (MB / seconds; default 25 / 60). |
| `--json` | Also print `findings.json` to stdout. |
| `-v`/`--verbose` | Verbose logging. |
| `--version` | Print the version and exit. |

Authoritative source: `check-cloud-ready --help`.

## Output

Every run writes `<out>/assessment-report.md` and `<out>/findings.json`
(byte-for-byte the same content `--json` prints to stdout). Shape sketch:

```json
{
  "input": {"raw": "s3://...", "type": "url", "profile_hint": "timeseries"},
  "assets": [{
    "url": "s3://...", "format": "zarr", "format_class": "cloud-native",
    "access_findings": [{"id": "D1-range", "dim": "D", "status": "pass", "evidence": "..."}],
    "inventory": [{"name": "/analysed_sst", "shape": [...], "chunks": [...], "dtype": "float32"}],
    "chunking": {"variables": [{"grade": "pass", "orientation": {...}, "profiles": {...}}]},
    "compression": {"inspection": [...], "benchmark": [...]},
    "conventions": {"cf": {"status": "pass", "checks": [...]}},
    "checks": [{"id": "A-class", "dimension": "A", "points_possible": 30, "points_awarded": 26.0,
                "status": "pass", "evidence": "...", "remediation": null}],
    "dimensions": {"A": {"score": 26.0, "max": 30}},
    "score": 82.0, "tier": "B"
  }],
  "score": 82.0, "tier": "B",
  "verdict": "READY WITH CAVEATS", "verdict_reason": "...", "confidence": "High"
}
```

## Auth setup

Never paste credentials into a chat/agent conversation. Configure one of, ahead of
time:

- `EARTHDATA_TOKEN`, or `EARTHDATA_USERNAME`+`EARTHDATA_PASSWORD`, or `~/.netrc`
  (chmod 600, `machine urs.earthdata.nasa.gov`) — for Earthdata Login.
- The standard AWS credential chain (env vars, `~/.aws/credentials`, instance
  role) — for non-NASA protected S3.

This CLI never rewrites a protected `s3://` URL into an HTTPS fallback — NASA
Earthdata Cloud protected buckets are only correctly readable via S3, in-region
(us-west-2), with minted credentials. A failure there is reported as a
`credentials-endpoint-auth` (fix your EDL credentials) or `in-region-only`
(re-run from us-west-2 compute) finding, never masked or misreported as a hosting
defect. Full detail:
`.claude/skills/check-cloud-ready/references/access.md`.

## Using this with an agent

For agent-assisted / agentic-workflow use — driving this CLI and interpreting its
output for a data provider — see the packaged skill:
`.claude/skills/check-cloud-ready/SKILL.md`. It documents the agent-mode
invocation, what each rubric dimension means and why, and how to translate a
FAIL/WARN into provider guidance without re-measuring anything the CLI already
measured.

For the reasoning behind any specific check (chunk-size targets, the compression
benchmark protocol, CF/GeoZarr rules, per-format assessment notes, the full scoring
rubric, or live acceptance checks against real public datasets), see
`.claude/skills/check-cloud-ready/references/`.

## Development

```bash
pip install -e '.[dev]'
pytest
```
