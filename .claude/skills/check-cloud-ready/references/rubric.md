# Cloud-readiness rubric (scoring engine)

This is what `scoring.py`'s `score()` implements. It is domain-agnostic scoring
machinery over the outputs of `access/`, `openers.py`, `chunking.py`,
`compression.py`, and `conventions.py` — no STAC catalog, no Croissant descriptor,
and no catalog crawl are inputs anywhere in this rubric. This CLI assesses **one
asset** (a file/store URL, or a CMR granule ID resolved to one) per run.

## Scoring model

- One asset gets a 0–100 score = A + B + C + D + E.
- Each dimension is scored from named checks. Each check records `{id, dimension,
  points_possible, points_awarded, status (pass|partial|fail|skipped|n/a), evidence,
  remediation}`.
- `skipped` checks (e.g. no CMR metadata available, network unavailable) are **not**
  excluded from the denominator — they're scored at a static-inference value (never
  zero, never full credit) and stay in the rollup; only the report's **confidence**
  label is downgraded (High → Reduced → Low), never the score itself.
- `n/a` checks (nothing to evaluate at all, e.g. no file-attrs evidence whatsoever
  for catalog quality) are excluded and the dimension is renormalized over what
  remains.
- Every fail/partial check must carry a remediation (`_ensure_remediation` fills in
  a fallback if an upstream module left one blank) — a fail or partial with no
  actionable fix never ships.

## Dimension A — Format & structure (30 pts)

One check (`A-class`): a class baseline (cloud-native highest, cloud-optimizable
middle, cloud-hostile lowest, unknown format in between) adjusted by format-specific
signals — COG validity/overviews/codec, Zarr consolidated-metadata presence,
HDF5/NetCDF-4 chunked-vs-contiguous and chunk-size-≥1MB, or (for a cloud-hostile
format) a mandatory migration remediation (`references/legacy.md`).

**Known gap**: the original rubric's HDF5/NetCDF-4 paged-aggregation sub-check
(file-space strategy = PAGE, 2–16 MB page size, per NASA ESDIS/IMPACT guidance) is
not evaluated — the opener's HDF5 inventory records don't currently capture
file-space/page-size telemetry. An HDF5/NetCDF-4 asset's A-class ceiling today is
baseline + chunking/contiguity adjustments only, never the full bonus the earlier
rubric allowed for paged aggregation.

## Dimension B — Metadata locality & richness (20 pts)

| Check | Pts | What |
|---|---|---|
| B1-open | 8 | Lazy open completes in a bounded number of requests. ≤3 → pass; ≤10 → partial; >10 → fail. Measured live when possible; statically inferred otherwise (COG/Parquet/PMTiles/FlatGeobuf/COPC header-at-front, consolidated Zarr metadata ⇒ pass; a cloud-hostile format's lack of a bounded-open path ⇒ fail). |
| B2-crs | 3 | CRS in-file (grid_mapping, or a CRS-container variable — `references/conventions.md`), else fail (net-verified) or skipped (not net-verified). |
| B3-semantics | 5 | Derived from the CF conventions check (`references/conventions.md`): CF pass → full credit, warn → partial, fail → low, skipped → mid (confidence-downgrading, not score-zeroing). |
| B4-catalog | 4 | **File-attrs based, not catalog-based**: format identified (+1.5), CF-shaped self-description present (+1.5), variable-level attrs present in the file (+1.0). `n/a` only when literally nothing is known (unknown format, no CF signal, no variable attrs at all) — that residual case doesn't count against the score. |

## Dimension C — Chunking & AI-workflow fit (25 pts)

Scored against the three access profiles from `references/chunking.md`'s
informational overlay (interactive/training/agentic `status`: within/below/above),
per measured variable.

| Check | Pts | Criteria |
|---|---|---|
| C1-interactive | 5 | All measured variables `within` the interactive target ⇒ pass; some ⇒ partial; none ⇒ fail; none measurable ⇒ skipped. A COG with no overviews forces fail regardless. |
| C2-training | 7 | Same pattern against the training target. |
| C3-agentic | 7 | Combines agentic chunk fit with B1-open's status (schema-enumerable-in-one-request is an agentic-profile requirement, not just a metadata-locality one) — both need to be good for full credit. |
| C4-count | 3 | Estimated total chunk count (`Σ ∏ ceil(shape/chunks)`) < 1,000,000 ⇒ pass; ≥ ⇒ fail (adopt Zarr v3 sharding). |
| C5-codec | 3 | Derived from `references/compression.md`'s codec inspection: all variables modern (zstd/blosc) ⇒ pass; all uncompressed ⇒ fail; mixed/weak (gzip-class) ⇒ partial. |

Note: within-chunk partial reads are impossible by design — the chunk is the atomic
read unit — so oversized chunks tax every partial read. This is why the profiles
bracket sizes rather than "bigger is better" (see `references/chunking.md`).

## Dimension D — Access & transport (15 pts)

Verified live via `access/probe.py` when possible (`references/access.md`); a
missing finding for a given check is scored `skipped` at half credit, with the
`D0-*` finding's reason (local-only input, `--no-network`) as evidence.

| Check | Pts | Pass criteria |
|---|---|---|
| D1-range | 6 | Real Range request answered with 206 + correct classification (`references/access.md`'s `classify_access_failure`, which never confuses an auth gate for missing range support). |
| D2-auth | 3 | Anonymous access, or auth clearly documented. For a CMR-resolved granule, the DAAC's published `/s3credentials` endpoint (found in the granule's UMM-JSON) itself counts as "clearly documented" — a bare URL/local path with no such metadata can only reach this via a live, successful probe. |
| D3-https-cors | 3 | HTTPS with valid TLS; CORS headers if browser use is plausible. |
| D4-cleanpath | 3 | No redirect chains, HTML interstitials, or click-through pages in front of data URLs; broken virtual-Zarr references also land here. |

## Dimension E — Reproducibility & governance (10 pts)

| Check | Pts | Pass criteria |
|---|---|---|
| E1-version | 2 | Icechunk snapshots ⇒ full credit; a strong ETag ⇒ partial; neither ⇒ fail. |
| E2-checksums | 2 | Icechunk ⇒ full credit; a strong (non-weak) ETag ⇒ partial; neither ⇒ fail. |
| E3-license | 2 | From CMR UMM-JSON `License`, when a granule ID was resolved. |
| E4-citation | 1 | From CMR `DOI`. |
| E5-contact | 1 | From CMR `ContactPersons`/`ContactGroups`. |
| E6-usage | 1 | From CMR `RelatedUrls` entries typed as documentation. |
| E7-applications | 1 | From CMR `ScienceKeywords` count (≥3) or `Abstract` length (≥200 chars); partial credit for thin-but-nonzero metadata. |

**E3–E7 need CMR metadata, which only exists for a granule-ID-resolved input.** A
bare URL or local path run has `cmr_meta = None`: all five checks are `skipped` at
half credit — this is a **confidence downgrade, not a fail** — the file genuinely
carries no separately-verifiable license/DOI/contact/keyword information on its own,
and the CLI has no catalog to consult for it (there is no STAC/Croissant record it
could otherwise read). This is the deliberate divergence from an older behavior that
scored these as outright failures with no license found, catalog or not.

## Tiers, caps, and rollup rules

| Score | Tier |
|---|---|
| 90–100 | **A** |
| 75–89 | **B** |
| 55–74 | **C** |
| 35–54 | **D** |
| <35 | **F** |

Hard rules:
- **Smoke-test FAIL caps the score at 74** regardless of static checks.
- **Any consumer-visible FAIL also caps the score at 74**: specifically
  `C1-interactive`, `C2-training`, or `D1-range` failing — these are the checks a
  real consumer would actually hit (bad chunk size, no range support), as opposed to
  governance/reproducibility/transport-quality gaps that don't block use. SKIPPED
  never caps anything; it only downgrades confidence (High → Reduced → Low, based on
  the count of skipped checks: 0 → High, ≤3 → Reduced, >3 → Low).
- Being on object storage earns **zero points by itself** — "cloud-hosted" and
  "cloud-optimized" are different claims; say so explicitly in the verdict when the
  gap applies.

## Categorical verdict mapping

Derived from the **same check statuses** that produce the score/tier — never a
separate judgment call:

- **NOT READY** — the smoke test failed (the data could not be read/verified live),
  **or** ≥1 consumer-visible FAIL (`C1-interactive`, `C2-training`, or `D1-range`).
- **READY WITH CAVEATS** — no consumer-blocking failure, but some checks are
  warn/partial or fail elsewhere in the rubric.
- **READY** — every assessable check passes.

The verdict and the tier are two views of the same evidence; they never contradict
each other because both are computed from the same `checks` list in the same call to
`score()`.

## Evidence & traceability

Every deduction must be traceable to evidence recorded in `findings.json` (header
bytes, request counts, timings, or the metadata field that failed). Every
fail/partial check carries at least one remediation with an exact command where one
exists.

## Sources / rubric provenance

- **Cloud-Optimized Geospatial Formats Guide** (guide.cloudnativegeo.org) — COG,
  Zarr, and cloud-optimized HDF5/NetCDF checklists.
- **rio-cogeo** validation rules for COG.
- **NASA ESDIS/IMPACT** cloud-optimization guidance for HDF5 — paged aggregation,
  ~8 MB page-size findings, avoiding scattered metadata.
- **Pangeo community** chunking guidance for analysis-ready, cloud-optimized (ARCO)
  data.
- **Zarr v3 specification** — sharding codec, consolidated-metadata behavior.
- **Datacube guide** (developmentseed.org/datacube-guide) — chunking worst-practices
  catalog.
- **Cross-provider request-size guidance** — AWS S3 byte-range whitepaper (8–16 MB
  ranged GETs), Azure Blob performance checklist (≥4 MiB blocks), GCS best
  practices, ESIP cloud optimization practices — basis for the ~8–16 MB compressed
  wire target.
- **HEFTIE Zarr benchmarks** and Klöwer et al. bit-rounding (xbitinfo) — compression
  grid conventions and precision-filter guidance (`references/compression.md`).
