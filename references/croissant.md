# Croissant & GeoCroissant Integration

MLCommons **Croissant** is a JSON-LD vocabulary for ML datasets;
**GeoCroissant** is its geospatial extension (MLCommons working-group
draft — **still stabilizing**; validate against the current spec at build
time and keep the emitter isolated so spec churn never touches the rubric
engine).

**What Croissant is NOT for here:** it does not replace STAC
(catalog/search), and it carries **no information about byte layout** — it
never contributes to Dimensions A, C, or D. Those always come from
touching the files.

## 1. As an input adapter (peer to STAC)

Detection: JSON-LD whose `@context` references
`mlcommons.org/croissant` (accept `…/croissant/1.0`,  `…/croissant/1.1`,
and GeoCroissant context URLs).

Resolution:
- `distribution` entries of type `cr:FileObject` → `contentUrl` is a
  concrete asset URL: assess directly.
- `cr:FileSet` → resolve `containedIn` (the archive/prefix FileObject) +
  `includes` glob. If `containedIn` is an archive (zip/tar) that's a
  legacy red flag (see `legacy.md`); if it's a prefix, list/sample like a
  storage prefix.
- Run the exact same assessment pipeline on the resolved URLs; sampling
  rules apply (N per FileSet, grouped by `encodingFormat`).

Pre-population from GeoCroissant fields (Dimension B only):
- spatial extent (`geocr:BoundingBox` / bbox), temporal extent → B4
  catalog-quality credit
- CRS declared at descriptor level → counts toward B2 **only if** it
  matches the in-file CRS (verify; mismatch is a B2 fail with evidence)
- region-restricted access / license → D2 flag + E3
- `sha256` on FileObjects → E2 credit; verify one sampled file's hash
  against its first bytes? No — hashing needs the whole file; instead
  record "declared, unverified (byte-capped run)".

## 2. As an output emitter (`--emit-croissant`)

After assessment, emit `croissant.jsonld` (GeoCroissant profile when
geospatial fields are known) so the dataset is immediately
discoverable/loadable in ML tooling (Google Dataset Search indexing,
PyTorch/TF/JAX Croissant loaders, NeurIPS-style dataset documentation).
The skill doesn't just grade AI-readiness — it manufactures the ML-ready
metadata artifact.

Populate from **verified** findings only:
- `distribution`: FileObjects with verified `contentUrl`,
  `contentSize` (from HEAD Content-Length), `sha256` slot filled from
  strong ETags when they are MD5-shaped (note the caveat: multipart ETags
  are not MD5 — then use `description` to record the raw ETag instead),
  `encodingFormat` from detected media type.
- `recordSet` sketches for array variables: one RecordSet per
  array/variable group with fields for dims/coords/dtype/units gleaned
  from the lazy open. Mark as sketch (`description: "auto-generated from
  cloud-readiness assessment"`).
- GeoCroissant fields when known: bbox, temporal extent, CRS.

## 3. As the vocabulary for AI-readiness findings

Attach rubric results as **namespaced custom properties** in the emitted
record, under a `cloudReadiness` prefix defined in `@context`
(e.g., `"cloudReadiness": "https://example.org/ns/cloud-readiness#"` —
use the skill-configured namespace):

```json
"cloudReadiness:tier": "B",
"cloudReadiness:score": 81,
"cloudReadiness:confidence": "high",
"cloudReadiness:chunkProfileInteractive": "pass (2.1 MB tiles, overviews)",
"cloudReadiness:chunkProfileTraining": "partial (4 MB chunks; recommend 32–64 MB shards)",
"cloudReadiness:chunkProfileAgentic": "pass (2 requests to first subset)",
"cloudReadiness:smokeTest": "pass",
"cloudReadiness:findings": "findings.json"
```

Use Croissant's **RAI extension** slots for provenance/bias-relevant
notes: record the assessment's **sampling frame** (which items/assets were
tested, selection rule) and any **coverage gaps found** (e.g., "polar
regions absent", "2019 missing") under `rai:dataCollection` /
`rai:dataLimitations` style properties.

## Emitter isolation rule

All Croissant/GeoCroissant serialization lives in one module
(`emit_croissant()` in `scripts/assess.py`) reading only `findings.json`
structures. It must import nothing from, and be imported by nothing in,
the scoring engine. Spec churn ⇒ touch the emitter only.
