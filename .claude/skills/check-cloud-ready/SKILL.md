---
name: check-cloud-ready
description: >
  Evaluate whether an Earth-systems dataset is cloud-optimized / cloud-native ready
  — on object storage, randomly accessible via HTTP range requests, well-chunked,
  well-compressed, and standards-conformant — and produce a graded scorecard
  (A–F tier, 0–100 score, and a READY / READY WITH CAVEATS / NOT READY verdict) with
  concrete fixes for the data provider. Use this skill whenever the user runs
  check-cloud-ready, asks whether a dataset is cloud-optimized / cloud-native / ARCO
  / AI-ready / analysis-ready, asks to assess chunking or compression recommendations
  for a NetCDF, HDF5, Zarr, Icechunk, kerchunk/VirtualiZarr, COG, or GeoParquet
  dataset, provides a dataset URL (s3://, https://, gs://, az://) or a NASA CMR
  granule ID and wants to know how well it will perform for cloud/AI-agentic access,
  or asks to grade/audit a dataset's cloud readiness — even if they don't use the
  word "assessment". Scope is cloud + data only: storage layout, formats, metadata,
  and access patterns. Never compute, orchestration, model architectures, or cost —
  decline politely if asked in the same breath. STAC catalogs and Croissant/
  GeoCroissant descriptors are out of scope: this skill assesses one asset
  entrypoint (a path, URL, or CMR granule ID) per run, never a catalog crawl.
---

# check-cloud-ready

## 1. What this skill does

`check-cloud-ready` is a deterministic CLI (`pip install -e '.[all]'`, console
script `check-cloud-ready`) that does **all** measurement: access resolution,
format detection, chunk/compression sampling, CF/GeoZarr checks, scoring, and
report rendering. This skill's added value is **interpretation** — reading the
CLI's output and explaining it to a data provider or an agentic workflow — not
re-measuring anything.

**Hard rule: never re-measure or hand-probe what the CLI measures.** Don't write
your own `requests.get(url, headers={"Range": ...})` call, don't open the dataset
yourself to "double check" a chunk size, don't guess a codec from a file extension.
If the CLI marks something `SKIPPED` (no network, no auth, a library not installed),
relay that reason to the user rather than improvising a workaround probe — a
`SKIPPED` reason is itself a finding, not a gap for you to paper over.

## 2. Install & run

```bash
pip install -e '.[all]'          # everything: s3, hdf5, zarr, raster, parquet, nasa, llm, cf
# or a subset: pip install -e '.[zarr,s3]'  (extras: s3, hdf5, zarr, raster, parquet, nasa, llm, cf)
```

**Agent-mode invocation** (non-interactive, both report styles, explicit output
directory):

```bash
check-cloud-ready <INPUT> --non-interactive --report-style both --out <dir>
```

Flags an agent typically needs, all real `argparse` flags on `check-cloud-ready`
(verify with `check-cloud-ready --help` if in doubt — never invent one):

| Flag | Purpose |
|---|---|
| `--granule-id G...` | Resolve access via a CMR granule ID instead of/in addition to a bare URL. |
| `--credentials-url URL` | An explicit DAAC `/s3credentials` endpoint. |
| `--earthaccess-fallback` | Opt into `earthaccess`-minted temporary S3 credentials. |
| `--anon` | Force anonymous S3 access. |
| `--variables a,b,c` / `--all-variables` | Pick which variables to assess (else the CLI ranks and picks one automatically, non-interactively). |
| `--suggest heuristic` | Deterministic ranking-based variable suggestion (`--suggest llm` also exists but needs `anthropic` + a key; falls back to heuristic automatically). |
| `--benchmark` / `--no-benchmark` | Run the empirical compression codec grid (downloads/decodes one sampled chunk). |
| `--format <fmt>` | Override auto-detection (see `references/formats.md` for the supported format strings). |
| `--use-case {timeseries,maps,balanced}` | Which chunk-orientation query gets *graded* (both are always reported). |
| `--no-network` | Skip every probe/remote resolution step (static-only run). |
| `--json` | Also print `findings.json` to stdout. |
| `--byte-cap` / `--time-cap` | Transfer/wall-clock budget in MB/seconds (defaults 25 MB / 60 s). |

Other flags exist (`--llm-model`, `--bitround-max-abs-error`, `--report-style`,
`-v`) — run `check-cloud-ready --help` for the complete, authoritative list.

**Interactive mode** (a human at a terminal, no `--non-interactive` and stdin is a
TTY): the CLI prompts progressively — which variable(s), whether to run the
compression benchmark, which report style — instead of requiring every flag
upfront. Let it prompt; don't pre-answer on the user's behalf unless they already
told you the answer.

## 3. Inputs

- A local file or directory path (hosting/access can't be assessed from a local
  copy — say so, and ask whether a cloud-hosted URL exists).
- An `https://`, `s3://`, `gs://`, or `az://` URL to a single file or a Zarr/
  Icechunk store root.
- A NASA CMR granule concept ID (`G<digits>-<PROVIDER>`), resolved via CMR to its
  data URL(s) — see `references/access.md`.

**Explicitly not supported**: STAC catalogs/API search, static STAC crawling, or
Croissant/GeoCroissant descriptor resolution. One asset per run.

## 4. Auth prerequisites

**Never ask the user to type a credential into the conversation.** Point them at
one of `EARTHDATA_TOKEN`, `EARTHDATA_USERNAME`+`EARTHDATA_PASSWORD`, or `~/.netrc`
(chmod 600) for Earthdata Login; the standard AWS credential chain for non-NASA
protected S3. Full detail, including the exact decision flow and error
classifications: `references/access.md`.

Two things worth knowing before you interpret a run:

- **Never S3→HTTPS.** This CLI will never rewrite a protected `s3://` URL into an
  HTTPS fallback to route around a credentials problem — that would silently defeat
  the point of an in-region cloud read. A protected-S3 failure is always reported
  as an auth/credentials finding, never masked by a fallback.
- **`in-region-only` vs `credentials-endpoint-auth`**: an S3 access failure is
  classified as one or the other. `credentials-endpoint-auth` means EDL credentials
  are missing/expired/insufficient — fixable from anywhere. `in-region-only` means
  credentials were minted fine but the read was denied anyway — almost always
  because the request isn't running in AWS us-west-2. This is **not a hosting
  defect**: hosting still passes, and structural checks that need the data itself
  are reported "not assessed — requires in-region compute", not failed.

## 5. Interpreting the report (the point of this skill)

The scorecard has five dimensions (A–E, `references/rubric.md`) plus a categorical
verdict. For each, know what it measures and why it matters enough to explain to a
provider:

- **A — Format & structure.** Is the container inherently cloud-friendly (Zarr/COG/
  Icechunk vs. plain NetCDF-3/HDF4)? This is the single biggest lever: no amount of
  good chunking rescues a format whose layout defeats range-based subsetting.
- **B — Metadata locality & richness.** `requests-to-open` is the number that
  matters here — it's literally how many round trips a client pays just to learn
  what's in the dataset, before reading any data. CF/CRS presence is what lets
  generic tools (not just bespoke readers written for this one dataset) interpret
  the file at all.
- **C — Chunking & AI-workflow fit.** The chunk is the atomic unit of decompression
  — a partial read still pays for the whole chunk. `references/chunking.md`
  explains why **compressed bytes on the wire**, not uncompressed size, is the unit
  that matters (it's what actually crosses the network), and what "orientation"
  means: a chunk shape can be optimized for full-extent maps at one timestep, or
  for a time series at one point, but rarely both at a useful size — that's a real
  design tradeoff to surface to a provider, not a bug to fix.
- **D — Access & transport.** Verified live, not inferred from metadata: does a
  real ranged GET return 206, is auth handled cleanly, is the data path free of
  redirect chains. This is the dimension most likely to surprise a provider who
  assumed "it's on S3" was the whole story.
- **E — Reproducibility & governance.** Versioning, checksums, license, citation,
  contact, discoverability — for a CMR-resolved granule these come from real
  UMM-JSON; for a bare URL/local path there's no catalog to consult, so these are
  `SKIPPED` (confidence-downgrading), never scored as an outright fail for
  something the file genuinely can't self-report.
- **The compression benchmark's current-codec baseline** — every comparison is
  measured against the dataset's *own* current codec first (reproduced locally),
  never an assumed default, so "switch to X" claims are backed by a real
  before/after number (`references/compression.md`).

**Verdict/tier/confidence semantics** (`references/rubric.md`): `SKIPPED`
downgrades **confidence**, never the score. A smoke-test **FAIL** (data could not
be read/verified live) caps the tier at C (score ≤74) and forces verdict
`NOT READY`, regardless of how good the static checks look. **Cloud-hosted ≠
cloud-optimized** — being on S3/GCS/Azure earns zero points by itself; say this
explicitly when a provider's data is well-hosted but poorly chunked/compressed.
Verdict and tier are always derived from the *same* check statuses in the same
scoring call — they never disagree with each other.

## 6. Translating findings for providers

Every `FAIL`/`WARN` (`"partial"`/`"fail"` status) check in `findings.json` carries a
non-empty `remediation` — a data provider recommendation, often an exact command.
Lead with the verdict line, then the highest-point-value remediations first
(`findings.json`'s checks are sorted by `points_possible` in the rendered report's
"Prioritized remediation" section already — don't re-derive an ordering). Phrase
guidance as "here's the fix and why it matters" (e.g. "rechunk time from 1 to 240
per chunk (→ ~12 MB compressed) — closes the gap between a 3,650-request time series
and a 1-request one"), not just "this failed". Migration commands for cloud-hostile
legacy formats live in `references/legacy.md` — quote them verbatim, they're
copy-paste-ready.

## 7. Degradation

- **Missing extras** (e.g. no `h5py`/`zarr`/`rasterio` installed): the relevant
  check reports `SKIPPED` with the exact package to `pip install`; the CLI never
  crashes for a missing optional dependency. Tell the user which extra to add if
  they want that check to run for real.
- **`--no-network`**: every access probe and remote resolution step is skipped up
  front (`D0-network`); the run still completes fully from whatever static evidence
  remains, with reduced/low confidence — don't apologize for this, it's the
  documented, intended behavior for an offline/CI run.
- **Auth-skipped** (`D2-auth` status `skipped`): tell the user exactly which
  credential source is missing (the finding's `remediation` already says which) —
  don't guess at what they might need beyond what the finding states.

## 8. References index

- `references/access.md` — the full access-resolution decision flow, credential
  sources, never-S3→HTTPS invariant, and S3 error classification.
- `references/chunking.md` — the one scored chunk-size criterion, the three-profile
  informational overlay, and orientation/read-amplification grading.
- `references/compression.md` — codec inspection, the current-codec-baseline
  benchmark protocol, and why lossy precision is a provider decision.
- `references/conventions.md` — the CF and GeoZarr structural checks.
- `references/formats.md` — format detection and per-format opener behavior/gaps.
- `references/hdf5-netcdf.md` — HDF5/NetCDF-4-specific checks and remediations.
- `references/zarr.md` — Zarr/Icechunk/virtual-Zarr-specific checks and remediations.
- `references/cog.md` — COG-specific checks, current opener caveats, and remediations.
- `references/geoparquet.md` — GeoParquet-specific checks, current opener caveats,
  and remediations.
- `references/legacy.md` — cloud-hostile legacy formats and mandatory migrations.
- `references/rubric.md` — the full scoring model: dimensions, points, tiers, caps,
  confidence rules, and the categorical verdict mapping.
- `references/acceptance-tests.md` — live GREEN-gate checks against real public
  datasets, for validating a change to this tooling.
