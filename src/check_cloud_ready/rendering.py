"""findings dict -> assessment-report.md, in one of three styles.

Pure rendering: no scoring, no measurement, no recomputation. Every
number/verdict/status printed here was already computed by an upstream
module (``scoring.score()`` for checks/dimensions/tier/verdict,
``chunking.assess_chunking()`` for chunk grading/orientation,
``compression.assess_compression()`` for codec inspection/benchmark,
...) -- this module only formats what it is handed, and degrades to a
"not assessed" placeholder instead of raising when a section is
missing/``None`` (a real asset may have skipped an assessment, e.g. no
network for the smoke test, or ``--no-benchmark``).

Ported from (read, not imported; ``.claude/skills/`` is read-only
reference material, retired in a later task):
``.claude/skills/earth-science-cloud-readiness/scripts/report.py`` --
all section renderers (``verdict``, ``scorecard``, ``per_asset``,
``smoke_table``, ``compression_section``, ``smoke_skip_note``,
``chunking_table`` w/ its three-profile +2705/2705/2705 marks,
``chunking_notes``, ``remediations``, ``sample_frame``) and
``.../assets/report-template.md``. Dropped per the task brief:
``mixed_catalog`` (this package assesses a single asset, never a mixed
catalog), any STAC/catalog section, and ``--html`` mode.

Findings shape (Ruling I-4; ``assets`` is always length 1 -- Task 12's
CLI orchestrator assembles the real one; see ``tests/test_rendering.py``
for a hand-written canned fixture built to this shape)::

    {
      "input": {"raw": str, "type": str, "profile_hint": str|None},
      "environment": {...},                 # not rendered directly
      "assets": [{
          "url": str, "format": str,
          "checks": [scoring.C()-shaped dicts]|None,
          "dimensions": {"A": {"score", "max"}, ...}|None,   # scoring.score() output
          "score": float|None, "tier": str|None,
          "smoke": {"status", "lazy_open", "subset_read", "budget", "reason"}|None,
          "inventory": [...]|None,
          "chunking": chunking.assess_chunking()-shaped dict|None,
          "compression": compression.assess_compression()-shaped dict|None,
          "conventions": {"cf": {...}}|None,
          "telemetry": {"requests_to_open", "bytes_to_open",
                        "metadata_walk": {"objects_visited", "requests",
                                          "bytes", "complete", "capped_at"}|None}|None,
      }],
      "score": float|None, "tier": str|None,
      "verdict": "READY"|"READY WITH CAVEATS"|"NOT READY"|None,
      "verdict_reason": str|None, "confidence": "High"|"Reduced"|"Low"|None,
      "sample_frame": {...}|None,           # reproducibility metadata
      "generated": str|None,                # timestamp, optional
    }

New vs. the old renderer (R1, R2 -- see the task brief):

- **R1**: line 1 of every rendered report is the verdict line,
  ``**Verdict: <verdict> — <verdict_reason>**``, in ALL three styles.
  The old ``verdict()`` function's paragraph-building heuristics
  (dominant format, biggest-gaps summary, cloud-hosted-vs-optimized
  note...) are NOT ported: ``scoring.verdict_for()`` already computes a
  categorical verdict + reason from the single-asset findings, so
  rendering it is a one-line format, not a re-derivation.
- **R2**: the scorecard carries a metadata-dispersal row (requests-to-
  open + the S7 full-metadata-walk request count), threaded through
  ``asset["telemetry"]``.
- A new "Chunk layout — what it's optimized for" subsection renders
  each variable's ``chunking`` orientation prose (Task 7); present in
  ALL three styles.
- Three ``style`` values, all built from the SAME findings dict (no
  recomputation, ever):
    - ``"verdict"``: verdict line + criterion table (one row per check:
      id, PASS/WARN/FAIL/SKIPPED/N-A, evidence, remediation) +
      orientation + remediations. No numeric score/tier anywhere.
    - ``"score"``: old-style tier/score scorecard + dimensions + the
      rest of the old sections (per-asset checks table, smoke
      telemetry, compression, chunking table/notes, remediations,
      sample frame) + orientation.
    - ``"both"``: everything from both of the above.
"""
from __future__ import annotations

import importlib.resources
import json
from pathlib import Path

__all__ = ["render", "write_report"]

DIM_NAMES = {"A": "Format & structure", "B": "Metadata locality & richness",
             "C": "Chunking & AI-workflow fit", "D": "Access & transport",
             "E": "Reproducibility & governance"}
_DIM_MAX = {"A": 30, "B": 20, "C": 25, "D": 15, "E": 10}

# Criterion-table status labels (new in this task; distinct from the
# old per-asset table, which prints the raw pass/partial/fail/skipped/
# n/a status alongside dimension + points instead).
_STATUS_LABELS = {"pass": "PASS", "partial": "WARN", "fail": "FAIL",
                  "skipped": "SKIPPED", "n/a": "N-A"}

# chunking_table's three-profile marks, ported verbatim from report.py.
_STATUS_MARKS = {"pass": "\u2705", "partial": "\u26a0\ufe0f",
                  "fail": "\u274c", "skipped": "\u2754"}

_PROFILE_ROWS = [
    ("Interactive / visualization", "C1-interactive",
     "~1-4 MB chunks/tiles + overviews", "one viewport tile: 1"),
    ("AI training (throughput)", "C2-training",
     "10-100 MB (sweet spot 32-64 MB) chunks/shards, shape aligned with sampling",
     "one ~64 MB batch: 1-2"),
    ("AI agentic", "C3-agentic",
     "1-16 MB chunks; schema in ONE request; stable HTTPS URLs",
     "catalog -> variables -> subset: <=3"),
]


def _md_escape(s) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def _not_assessed(reason: str) -> str:
    return f"_not assessed — {reason}._"


def _first_asset(findings: dict) -> dict:
    assets = findings.get("assets") or [{}]
    return assets[0] or {}


# ------------------------------------------------------------- R1: verdict

def _verdict_block(findings: dict, style: str) -> str:
    verdict = findings.get("verdict") or "\u2014"
    reason = findings.get("verdict_reason") or "no reason recorded"
    line = f"**Verdict: {verdict} \u2014 {reason}**"
    if style == "verdict":
        return line + "\n"
    tier = findings.get("tier") or "\u2014"
    score = findings.get("score")
    score_txt = f"{score}/100" if score is not None else "\u2014/100"
    confidence = findings.get("confidence") or "\u2014"
    return line + f"\nTier {tier} \u00b7 {score_txt} \u00b7 confidence {confidence}\n"


# --------------------------------------------------------------- scorecard

def _dispersal_row(asset: dict) -> str:
    tel = asset.get("telemetry")
    if not tel:
        return ("| Metadata dispersal | requests-to-open + full-metadata-walk requests | "
                "not assessed — no telemetry recorded | \u2014 |")
    r2o = tel.get("requests_to_open", "\u2014")
    mw = tel.get("metadata_walk")
    if mw:
        mw_txt = (f"{mw.get('requests', '\u2014')} requests "
                  f"(objects_visited={mw.get('objects_visited', '\u2014')}, "
                  f"complete={mw.get('complete', '\u2014')})")
    else:
        mw_txt = "not walked"
    return (f"| Metadata dispersal | requests-to-open={r2o}; full-metadata-walk={mw_txt} "
            f"| \u2014 | \u2014 |")


def scorecard(asset: dict) -> str:
    dims = asset.get("dimensions")
    if not dims:
        return _not_assessed("no dimension scores were recorded for this asset")
    rows = ["| Dimension | What | Score | Max |", "|---|---|---|---|"]
    for d in "ABCDE":
        entry = dims.get(d) or {}
        rows.append(f"| {d} | {DIM_NAMES[d]} | {entry.get('score', '\u2014')} | "
                    f"{entry.get('max', _DIM_MAX[d])} |")
    total = asset.get("score")
    rows.append(f"| **Total** | | **{total if total is not None else '\u2014'}** | **100** |")
    rows.append(_dispersal_row(asset))
    return "\n".join(rows)


# --------------------------------------------------------- criterion table

def criterion_table(asset: dict) -> str:
    checks = asset.get("checks")
    if not checks:
        return _not_assessed("no checks were recorded for this asset")
    rows = ["| Check | Status | Evidence | Remediation |", "|---|---|---|---|"]
    for c in checks:
        label = _STATUS_LABELS.get(c.get("status"), str(c.get("status", "\u2014")).upper())
        rem = _md_escape(c["remediation"]) if c.get("remediation") else "\u2014"
        rows.append(f"| {c.get('id')} | {label} | {_md_escape(c.get('evidence'))[:180]} | "
                    f"{rem} |")
    return "\n".join(rows)


# ------------------------------------------------------- old-style per_asset

def per_asset(asset: dict) -> str:
    checks = asset.get("checks")
    if not checks:
        return _not_assessed("no checks were recorded for this asset")
    score = asset.get("score", "\u2014")
    lines = [f"### `{asset.get('url', '\u2014')}` \u2014 {asset.get('format', '\u2014')} \u2014 "
             f"{score}/100 ({asset.get('tier', '\u2014')})", "",
             "| Check | Dim | Status | Pts | Evidence |", "|---|---|---|---|---|"]
    for c in checks:
        lines.append(f"| {c.get('id')} | {c.get('dimension')} | {c.get('status')} | "
                     f"{c.get('points_awarded')}/{c.get('points_possible')} | "
                     f"{_md_escape(c.get('evidence'))[:180]} |")
    return "\n".join(lines)


# --------------------------------------------------------------- orientation

def orientation_section(asset: dict) -> str:
    heading = "## Chunk layout — what it's optimized for\n\n"
    chunking = asset.get("chunking")
    if chunking is None:
        return heading + _not_assessed("no chunking assessment was recorded for this asset")
    variables = chunking.get("variables") or []
    if not variables:
        return heading + _not_assessed("no variables were assessed")
    lines = []
    for v in variables:
        orient = v.get("orientation") or {}
        prose = orient.get("prose")
        name = v.get("name", "\u2014")
        if not prose:
            lines.append(f"- **`{name}`**: not assessed — orientation could not be classified")
        else:
            label = orient.get("orientation", "unknown")
            lines.append(f"- **`{name}`** ({label}): {prose}")
    return heading + "\n".join(lines)


# -------------------------------------------------------------- smoke test

def smoke_section(asset: dict) -> str:
    heading = "## Smoke-test telemetry\n\n"
    smoke = asset.get("smoke")
    if not smoke:
        return heading + _not_assessed("smoke test was not run for this asset")
    lo = smoke.get("lazy_open") or {}
    sr = smoke.get("subset_read") or {}
    budget = smoke.get("budget") or {}
    rows = ["| Status | Requests-to-open | Bytes-to-open | Time-to-open | Subset TTFB | "
            "Throughput | Total bytes |", "|---|---|---|---|---|---|---|"]
    rows.append("| " + " | ".join(str(v) for v in [
        smoke.get("status"),
        lo.get("requests_to_open", "\u2014"), lo.get("bytes_to_open", "\u2014"),
        f"{lo.get('time_to_open_s', '\u2014')} s",
        f"{sr.get('ttfb_s_approx', '\u2014')} s",
        f"{sr.get('throughput_MBps', '\u2014')} MB/s",
        budget.get("bytes", "\u2014")]) + " |")
    block = heading + "\n".join(rows)
    if smoke.get("status") == "skipped":
        reason = smoke.get("reason") or "unknown"
        block += ("\n\n> **Smoke test skipped, confidence: reduced.** Reason: " + str(reason) +
                  ". Static checks were used where possible; live Range/latency "
                  "verification was not performed.")
    return block


# --------------------------------------------------------------- compression

def compression_section(asset: dict) -> str:
    heading = "## Compression\n\n"
    comp = asset.get("compression")
    if comp is None:
        return heading + _not_assessed("no compression assessment was recorded for this asset")

    lines = []
    inspection = comp.get("inspection") or []
    if inspection:
        lines += ["### Codec inspection", "",
                  "| Variable | Codec | Status | Note |", "|---|---|---|---|"]
        for r in inspection:
            lines.append(f"| {r.get('name')} | {r.get('codec') or '\u2014'} | "
                         f"{r.get('status')} | {_md_escape(r.get('note') or '')} |")
    else:
        lines.append(_not_assessed("no codec inspection rows were recorded"))

    bench = comp.get("benchmark")
    if bench:
        lines += ["", "### Benchmark (measured on one sampled chunk)", "",
                  "Grid per read-heavy-archive conventions (zstd-1/3/5, blosc-lz4, "
                  "blosc-zstd-3, \u00b1 byte shuffle); judge with the transfer model "
                  "`TTFB + compressed/network_bw + uncompressed/decompress_speed` \u2014 "
                  "over egress, ratio wins; in-region, decode speed can dominate.", "",
                  "| Config | Ratio | Compress MB/s | Decompress MB/s |", "|---|---|---|---|"]
        for row in bench:
            if row.get("status") == "skipped":
                lines.append(f"| {row.get('reason', 'skipped')} | \u2014 | \u2014 | \u2014 |")
                continue
            config = row.get("config", "\u2014")
            if row.get("is_current"):
                config = f"**{config} (current)**"
            if row.get("error"):
                lines.append(f"| {config} | error: {_md_escape(row['error'])} | \u2014 | "
                             f"\u2014 |")
                continue
            ratio = row.get("ratio")
            ratio_txt = f"{ratio}x" if ratio is not None else "\u2014"
            lines.append(f"| {config} | {ratio_txt} | {row.get('compress_MBps') or '\u2014'} | "
                         f"{row.get('decompress_MBps') or '\u2014'} |")
    return heading + "\n".join(lines)


# ---------------------------------------------------------------- chunking

def chunking_table(asset: dict) -> str:
    if asset.get("chunking") is None:
        return _not_assessed("no chunking assessment was recorded for this asset")
    checks = {c.get("id"): c for c in (asset.get("checks") or [])}
    fmt = asset.get("format", "\u2014")
    rows = ["| Profile | Current (per sampled asset) | Recommended | "
            "Expected requests: canonical pattern |", "|---|---|---|---|"]
    for label, cid, rec, canon in _PROFILE_ROWS:
        c = checks.get(cid)
        if c:
            mark = _STATUS_MARKS.get(c.get("status"), "\u2754")
            cur_txt = f"{fmt}: {mark} {_md_escape(c.get('evidence'))[:90]}"
            rec_txt = f"keep as-is ({rec})" if mark == _STATUS_MARKS["pass"] else rec
        else:
            cur_txt, rec_txt = "\u2014", rec
        rows.append(f"| {label} | {cur_txt} | {rec_txt} | {canon} |")
    return "\n".join(rows)


def chunking_notes(findings: dict) -> str:
    hint = (findings.get("input") or {}).get("profile_hint")
    if hint:
        return f"Primary intended profile (user-stated): **{hint}**."
    return ("No primary profile was stated; scores assume the most plausible profile "
            "per format and the table above covers all three.")


def _chunking_block(findings: dict, asset: dict) -> str:
    header = ("## Chunking recommendations (all three profiles)\n\n"
              "> Within-chunk partial reads are impossible by design \u2014 the chunk is "
              "the\n"
              "> atomic read unit \u2014 so oversized chunks tax every partial read and\n"
              "> undersized chunks tax every bulk read.\n\n")
    return header + chunking_table(asset) + "\n\n" + chunking_notes(findings)


# ------------------------------------------------------------- remediations

def remediations(asset: dict) -> str:
    checks = asset.get("checks") or []
    items = []
    for c in checks:
        if c.get("status") in ("fail", "partial") and c.get("remediation"):
            items.append((c.get("points_possible", 0), c.get("id"), c.get("remediation")))
    if not items:
        return "No failed checks — no remediation required."
    items.sort(key=lambda x: -x[0])
    seen, out = set(), []
    for pts, cid, rem in items:
        key = (cid, rem)
        if key in seen:
            continue
        seen.add(key)
        out.append(f"{len(out) + 1}. **[{cid}, {pts} pts at stake]** {rem}")
    return "\n".join(out)


# ------------------------------------------------------------- sample frame

def sample_frame_block(findings: dict) -> str:
    sf = findings.get("sample_frame")
    if not sf:
        return _not_assessed("no sample frame was recorded for this run")
    return "```json\n" + json.dumps(sf, indent=2, sort_keys=True)[:3000] + "\n```"


# ------------------------------------------------------------------- render

def _body(findings: dict, asset: dict, style: str) -> str:
    blocks = []
    if style in ("score", "both"):
        blocks.append("## Scorecard\n\n" + scorecard(asset))
    if style in ("verdict", "both"):
        blocks.append("## Criteria\n\n" + criterion_table(asset))
    blocks.append(orientation_section(asset))
    if style in ("score", "both"):
        blocks.append("## Per-asset findings\n\n" + per_asset(asset))
        blocks.append(smoke_section(asset))
        blocks.append(compression_section(asset))
        blocks.append(_chunking_block(findings, asset))
    blocks.append("## Prioritized remediation\n\n" + remediations(asset))
    if style in ("score", "both"):
        blocks.append("## Sample frame (reproducibility)\n\n" + sample_frame_block(findings))
    return "\n\n".join(blocks)


def _load_template() -> str:
    return (importlib.resources.files("check_cloud_ready") / "assets"
            / "report-template.md").read_text()


def render(findings: dict, style: str = "both") -> str:
    """Render ``findings`` (Ruling I-4 shape) to markdown. ``style`` is
    one of ``"verdict"``, ``"score"``, ``"both"`` (see the module
    docstring). Pure function: no I/O, no recomputation -- every value
    printed already exists in ``findings``.
    """
    if style not in ("verdict", "score", "both"):
        raise ValueError(f"unknown style {style!r}; expected 'verdict', 'score', or 'both'")
    asset = _first_asset(findings)
    tpl = _load_template()
    return tpl.format(
        verdict_block=_verdict_block(findings, style),
        dataset_name=(findings.get("input") or {}).get("raw", "unknown input"),
        input_raw=(findings.get("input") or {}).get("raw", "\u2014"),
        input_type=(findings.get("input") or {}).get("type", "\u2014"),
        confidence=findings.get("confidence") or "\u2014",
        generated=findings.get("generated") or "not recorded",
        body=_body(findings, asset, style),
    )


def write_report(findings: dict, out_dir: Path, style: str = "both") -> Path:
    """Render and write ``assessment-report.md`` under ``out_dir``
    (created if needed). Returns the written path."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "assessment-report.md"
    out_path.write_text(render(findings, style))
    return out_path
