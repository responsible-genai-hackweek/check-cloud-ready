#!/usr/bin/env python3
"""Render assessment-report.md (and optional HTML) from findings.json.

Usage:
    python report.py findings.json [--template ../assets/report-template.md]
                     [-o assessment-report.md] [--html]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

TIER_LABELS = {"A": "Cloud-Native", "B": "Cloud-Optimized",
               "C": "Cloud-Friendly with gaps", "D": "Cloud-Hosted only",
               "F": "Not cloud-ready"}

DIM_NAMES = {"A": "Format & structure", "B": "Metadata locality & richness",
             "C": "Chunking & AI-workflow fit", "D": "Access & transport",
             "E": "Reproducibility & governance"}


def _md_escape(s):
    return str(s).replace("|", "\\|").replace("\n", " ")


def verdict(f):
    ru = f["rollup"]
    if ru.get("dataset_score") is None:
        reasons = ru.get("unassessable_reasons") or ["input unreachable"]
        return ("**No assessment was possible**: no assets could be gathered "
                "from the input. Likely causes: network egress blocked from "
                "this environment, authentication required, or the input has "
                "no reachable items. Details: " + "; ".join(reasons[:3]) +
                ". Re-run from an environment with network access to the "
                "catalog and data hosts (an org owner can update the "
                "network allowlist), or upload the catalog/collection/items "
                "JSON directly for a static-only assessment.")

    ru = f["rollup"]
    tier, conf = ru["dataset_tier"], ru["confidence"]
    dom = ru.get("dominant_format")
    parts = []
    parts.append(f"The dataset grades **{tier} ({ru['dataset_tier_label']})** at "
                 f"{ru['dataset_score']}/100 (confidence: {conf}), assessed on "
                 f"{len(f['assets'])} sampled asset(s), dominant format "
                 f"**{dom}**.")
    d_scores = [a["dimensions"]["D"]["score"] for a in f["assets"]] or [0]
    a_scores = [a["dimensions"]["A"]["score"] for a in f["assets"]] or [0]
    if max(d_scores) >= 10 and max(a_scores) < 15:
        parts.append("Note the distinction: this data is **cloud-hosted, not "
                     "cloud-optimized** — it sits on reachable object storage "
                     "(which by itself earns zero points), but its internal layout "
                     "fights ranged access. The remediation plan below closes that gap.")
    if ru["smoke_test_overall"] == "fail":
        parts.append("The live smoke test **failed**, which caps the score at C-tier "
                     "regardless of static checks.")
    if ru["smoke_test_overall"] == "skipped":
        parts.append("The live smoke test was **skipped** (network or auth "
                     "unavailable); scores reflect static analysis and the report "
                     "confidence is downgraded — not the score.")
    fails = [c for a in f["assets"] for c in a["checks"] if c["status"] == "fail"]
    if fails:
        worst = sorted(fails, key=lambda c: c["points_possible"], reverse=True)[:2]
        parts.append("Biggest gaps: " + "; ".join(
            f"{c['id']} ({_md_escape(c['evidence'])[:100]})" for c in worst) + ".")
    return " ".join(parts)


def scorecard(f):
    rows = ["| Dimension | What | Score | Max |", "|---|---|---|---|"]
    # average dimension scores across assets
    for d in "ABCDE":
        vals = [a["dimensions"][d]["score"] for a in f["assets"]] or [0]
        mx = f["assets"][0]["dimensions"][d]["max"] if f["assets"] else \
            {"A": 30, "B": 20, "C": 25, "D": 15, "E": 10}[d]
        rows.append(f"| {d} | {DIM_NAMES[d]} | {round(sum(vals)/len(vals),1)} | {mx} |")
    rows.append(f"| **Total** | | **{f['rollup']['dataset_score']}** | **100** |")
    return "\n".join(rows)


def mixed_catalog(f):
    bm = f["rollup"]["by_media_type"]
    if len(bm) <= 1:
        return ""
    rows = ["## Per-format distribution (mixed catalog — graded per media type, "
            "never averaged)", "",
            "| Format | Assets | Median score | Tier |", "|---|---|---|---|"]
    for fmt, v in sorted(bm.items(), key=lambda kv: -kv[1]["median_score"]):
        rows.append(f"| {fmt} | {v['count']} | {v['median_score']} | {v['tier']} |")
    return "\n".join(rows)


def per_asset(f):
    out = []
    for a in f["assets"]:
        out.append(f"### `{a['id']}` — {a['format']} — {a['score']}/100 "
                   f"({a['tier']}){' — score capped by smoke-test failure' if a.get('score_capped_by_smoke_fail') else ''}")
        out.append(f"URL: `{a['url']}`")
        out.append("")
        out.append("| Check | Dim | Status | Pts | Evidence |")
        out.append("|---|---|---|---|---|")
        for c in a["checks"]:
            out.append(f"| {c['id']} | {c['dimension']} | {c['status']} | "
                       f"{c['points_awarded']}/{c['points_possible']} | "
                       f"{_md_escape(c['evidence'])[:180]} |")
        out.append("")
    return "\n".join(out)


def smoke_table(f):
    rows = ["| Asset | Status | Requests-to-open | Bytes-to-open | Time-to-open | "
            "Subset TTFB | Throughput | Total bytes |",
            "|---|---|---|---|---|---|---|---|"]
    for a in f["assets"]:
        t = a.get("smoke_test") or {}
        lo = t.get("lazy_open") or {}
        sr = t.get("subset_read") or {}
        rows.append("| " + " | ".join(map(str, [
            a["id"], t.get("status"),
            lo.get("requests_to_open", "—"), lo.get("bytes_to_open", "—"),
            f"{lo.get('time_to_open_s', '—')} s",
            f"{sr.get('ttfb_s_approx', '—')} s",
            f"{sr.get('throughput_MBps', '—')} MB/s",
            (t.get("budget") or {}).get("bytes", "—")])) + " |")
    return "\n".join(rows)


def compression_section(f):
    rows = []
    for a in f["assets"]:
        for t in (a.get("smoke_test") or {}).get("compression_trials", [])[:5]:
            rows.append(f"| {a['id']} | {t['codec']} | {t['ratio']}x | "
                        f"{t.get('compress_MBps') or '—'} | "
                        f"{t.get('decompress_MBps') or '—'} |")
    if not rows:
        return ""
    return ("## Compression trials (measured on one sampled chunk per asset)\n\n"
            "Grid per read-heavy-archive conventions (zstd-1/3/5, blosc-lz4, "
            "blosc-zstd-3, ± byte shuffle); judge with the transfer model "
            "`TTFB + compressed/network_bw + uncompressed/decompress_speed` — "
            "over egress, ratio wins; in-region, decode speed can dominate. "
            "For floats, precision filters (bit rounding via xbitinfo) beat any "
            "codec change; see `references/compression.md`.\n\n"
            "| Asset | Codec | Ratio | Compress MB/s | Decompress MB/s |\n"
            "|---|---|---|---|---|\n" + "\n".join(rows) + "\n")


def smoke_skip_note(f):
    if f["rollup"]["smoke_test_overall"] != "skipped":
        return ""
    reasons = {(a.get("smoke_test") or {}).get("reason") for a in f["assets"]}
    return ("> **Smoke test skipped, confidence: reduced.** Reason(s): "
            + "; ".join(str(r) for r in reasons if r)
            + ". Static checks were used where possible; live Range/latency "
              "verification was not performed.")


def chunking_table(f):
    rows = ["| Profile | Current (per sampled asset) | Recommended | "
            "Expected requests: canonical pattern |", "|---|---|---|---|"]
    profs = [("Interactive / visualization", "C1-interactive", "~1–4 MB chunks/tiles + overviews",
              "one viewport tile: 1"),
             ("AI training (throughput)", "C2-training",
              "10–100 MB (sweet spot 32–64 MB) chunks/shards, shape aligned with sampling",
              "one ~64 MB batch: 1–2"),
             ("AI agentic", "C3-agentic",
              "1–16 MB chunks; schema in ONE request; stable HTTPS URLs",
              "catalog → variables → subset: ≤3")]
    for label, cid, rec, canon in profs:
        cur = []
        for a in f["assets"]:
            c = next((c for c in a["checks"] if c["id"] == cid), None)
            if c:
                mark = {"pass": "✅", "partial": "⚠️", "fail": "❌",
                        "skipped": "❔"}.get(c["status"], "")
                cur.append(f"{a['format']}: {mark} { _md_escape(c['evidence'])[:90]}")
        cur_txt = "<br>".join(sorted(set(cur))) or "—"
        rec_txt = rec
        if all("✅" in c for c in cur) and cur:
            rec_txt = "keep as-is (" + rec + ")"
        rows.append(f"| {label} | {cur_txt} | {rec_txt} | {canon} |")
    return "\n".join(rows)


def chunking_notes(f):
    hint = f["input"].get("profile_hint")
    n = []
    if hint:
        n.append(f"Primary intended profile (user-stated): **{hint}**.")
    else:
        n.append("No primary profile was stated; scores assume the most plausible "
                 "profile per format and the table above covers all three.")
    return " ".join(n)


def remediations(f):
    items = []
    for a in f["assets"]:
        for c in a["checks"]:
            if c["status"] in ("fail", "partial") and c.get("remediation"):
                items.append((c["points_possible"], a["id"], c["id"],
                              c["remediation"]))
    if not items:
        return "No failed checks — no remediation required. 🎉"
    items.sort(key=lambda x: -x[0])
    seen, out = set(), []
    for pts, aid, cid, rem in items:
        key = (cid, rem)
        if key in seen:
            continue
        seen.add(key)
        out.append(f"{len(out)+1}. **[{cid}, {pts} pts at stake]** {rem} "
                   f"_(e.g., asset `{aid}`)_")
    return "\n".join(out)


def sample_frame(f):
    return "```json\n" + json.dumps(f["sample_frame"], indent=2)[:3000] + "\n```"


def render(findings_path, template_path, out_path, html=False):
    f = json.loads(Path(findings_path).read_text())
    tpl = Path(template_path).read_text()
    ru = f["rollup"]
    doc = tpl.format(
        dataset_name=f["input"]["raw"],
        timestamp=f["generated"],
        input_raw=f["input"]["raw"],
        input_type=f["input"]["type"],
        confidence=ru["confidence"],
        tier=ru["dataset_tier"] or "—",
        tier_label=ru.get("dataset_tier_label", TIER_LABELS.get(ru["dataset_tier"], "")),
        score=(ru["dataset_score"] if ru["dataset_score"] is not None else "—"),
        verdict_paragraph=verdict(f),
        scorecard_table=scorecard(f),
        mixed_catalog_section=mixed_catalog(f),
        per_asset_findings=per_asset(f),
        smoke_telemetry_table=smoke_table(f),
        smoke_skip_note=smoke_skip_note(f),
        compression_section=compression_section(f),
        chunking_table=chunking_table(f),
        chunking_notes=chunking_notes(f),
        remediation_list=remediations(f),
        sample_frame=sample_frame(f),
    )
    Path(out_path).write_text(doc)
    print(f"[report] wrote {out_path}")
    if html:
        try:
            import markdown  # type: ignore
            html_doc = ("<html><head><meta charset='utf-8'><style>body{font-family:"
                        "system-ui;max-width:60rem;margin:2rem auto;padding:0 1rem}"
                        "table{border-collapse:collapse}td,th{border:1px solid #ccc;"
                        "padding:4px 8px}</style></head><body>"
                        + markdown.markdown(doc, extensions=["tables"])
                        + "</body></html>")
            hp = str(out_path).rsplit(".", 1)[0] + ".html"
            Path(hp).write_text(html_doc)
            print(f"[report] wrote {hp}")
        except ImportError:
            print("[report] python-markdown not installed; skipping HTML")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("findings")
    ap.add_argument("--template",
                    default=str(Path(__file__).parent.parent / "assets" /
                                "report-template.md"))
    ap.add_argument("-o", "--output", default="assessment-report.md")
    ap.add_argument("--html", action="store_true")
    args = ap.parse_args()
    render(args.findings, args.template, args.output, args.html)


if __name__ == "__main__":
    main()
