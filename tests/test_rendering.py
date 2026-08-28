"""Golden-file + robustness tests for check_cloud_ready.rendering.

``FINDINGS`` below is one hand-written findings dict, built to the
Ruling I-4 shape (see rendering.py's module docstring): a zarr asset
with one chunking FAIL carrying a remediation (C1-interactive), one
SKIPPED check (B2-crs), an orientation prose string, a codec inspection
plus a small benchmark including an ``is_current`` row, and metadata-
dispersal numbers (requests-to-open + a full-metadata-walk request
count) under ``telemetry``.

Regenerating the golden files
------------------------------
The goldens under ``tests/golden/`` are a snapshot of
``rendering.render(FINDINGS, style)`` for each of the three styles. If
you deliberately change the renderer's output format, regenerate them
with:

    python -c "
    from pathlib import Path
    from tests.test_rendering import FINDINGS
    from check_cloud_ready.rendering import render
    for style in ('verdict', 'score', 'both'):
        Path(f'tests/golden/report-{style}.md').write_text(render(FINDINGS, style))
    "

then diff the new goldens by hand (``git diff tests/golden/``) to
confirm the change is the one you intended before committing.
"""
import copy
import json
import unittest
from pathlib import Path

from check_cloud_ready import rendering

GOLDEN_DIR = Path(__file__).parent / "golden"


def _check(id_, dim, pts, awarded, status, evidence, remediation=None):
    return {"id": id_, "dimension": dim, "points_possible": pts,
            "points_awarded": awarded, "status": status, "evidence": evidence,
            "remediation": remediation}


CHECKS = [
    _check("A-class", "A", 30, 26.0, "pass",
           "format=zarr, class=cloud-native; consolidated metadata (zarr_version=3)"),
    _check("B1-open", "B", 8, 8.0, "pass",
           "requests_to_open=2, bytes_to_open=4096; metadata_walk objects_visited=6, "
           "complete=True"),
    _check("B2-crs", "B", 3, 1.5, "skipped",
           "no machine-readable CRS found; live probe unavailable to confirm further"),
    _check("B3-semantics", "B", 5, 5.0, "pass", "conventions_attr=True; units_and_name=True"),
    _check("C1-interactive", "C", 10, 2.0, "fail",
           "chunks average 118.4 MB per sampled chunk, far above the 1-4 MB interactive band",
           "rewrite with ~2 MB chunks/tiles aligned to viewport access (e.g. "
           "1x180x360), or publish a lower-resolution overview variable for "
           "interactive use"),
    _check("C5-codec", "C", 5, 3.0, "partial",
           "gzip: works, but zstd/blosc dominate gzip/zlib/deflate on every axis",
           "recompress with zstd level 3 plus byte shuffle (faster decode at a "
           "similar ratio)"),
    _check("D1-range", "D", 8, 8.0, "pass", "HTTP Range (206) confirmed via live probe"),
    _check("E1-etag", "E", 5, 5.0, "pass", "stable ETag observed across two HEAD probes"),
]

ASSET = {
    "url": "s3://demo-bucket/ocean-temp.zarr",
    "format": "zarr",
    "score": 71.5,
    "tier": "C",
    "dimensions": {
        "A": {"score": 26.0, "max": 30},
        "B": {"score": 14.5, "max": 20},
        "C": {"score": 12.0, "max": 25},
        "D": {"score": 8.0, "max": 15},
        "E": {"score": 5.0, "max": 10},
    },
    "checks": CHECKS,
    "smoke": {
        "status": "pass",
        "lazy_open": {"requests_to_open": 2, "bytes_to_open": 4096, "time_to_open_s": 0.18},
        "subset_read": {"ttfb_s_approx": 0.09, "throughput_MBps": 42.3},
        "budget": {"bytes": 26214400},
    },
    "inventory": [
        {"name": "sea_water_temperature", "dims": ["time", "lat", "lon"],
         "shape": [365, 720, 1440], "chunks": [365, 720, 1440], "dtype": "float32",
         "codec": "gzip", "attrs": {"units": "degC"}, "size_bytes": 1512864000},
    ],
    "chunking": {
        "variables": [
            {
                "name": "sea_water_temperature",
                "chunks": [365, 720, 1440],
                "shape": [365, 720, 1440],
                "measured": True,
                "compressed_chunk_mb": {"median": 118.4, "min": 110.2, "max": 126.7,
                                        "n_sampled": 8},
                "grade": "fail",
                "grade_note": "chunk far exceeds the 1-4 MB interactive band",
                "orientation": {
                    "orientation": "map-optimized",
                    "prose": ("This layout stores one full spatial map per chunk and "
                              "slices time thinly: a full-extent map read at one time "
                              "step touches a single chunk (cheap), but a full time "
                              "series at one point touches all 365 chunks (expensive). "
                              "Good fit for map-at-a-time workflows; poor fit for "
                              "point-timeseries extraction."),
                    "queries": {"map_at_time": {"chunks_touched": 1},
                                "series_at_point": {"chunks_touched": 365}},
                },
                "profiles": {
                    "interactive": {"target_mb": [1.0, 4.0], "status": "above"},
                    "training": {"target_mb": [10.0, 100.0], "sweet_spot_mb": [32.0, 64.0],
                                 "status": "above"},
                    "agentic": {"target_mb": [1.0, 16.0], "status": "above",
                               "note": "schema must additionally be enumerable in <=3 requests"},
                },
                "coordinate_chunking_notes": [],
            },
        ],
    },
    "compression": {
        "inspection": [
            {"name": "sea_water_temperature", "codec": "gzip", "status": "warn",
             "note": "gzip: works, but zstd/blosc dominate gzip/zlib/deflate on every axis",
             "remediation": "recompress with zstd level 3 plus byte shuffle"},
        ],
        "benchmark": [
            {"config": "current (gzip)", "is_current": True, "ratio": 2.1,
             "compress_MBps": 80.0, "decompress_MBps": 210.0},
            {"config": "zstd-1", "is_current": False, "ratio": 2.4,
             "compress_MBps": 350.0, "decompress_MBps": 900.0},
            {"config": "zstd-3+shuffle", "is_current": False, "ratio": 2.9,
             "compress_MBps": 210.0, "decompress_MBps": 850.0},
        ],
        "notes": [],
    },
    "conventions": {
        "cf": {"status": "pass",
               "checks": [{"id": "conventions_attr", "ok": True, "evidence": "CF-1.10"}],
               "notes": []},
    },
    "telemetry": {
        "requests_to_open": 2,
        "bytes_to_open": 4096,
        "metadata_walk": {"objects_visited": 6, "requests": 6, "bytes": 512,
                          "complete": True, "capped_at": None},
    },
}

FINDINGS = {
    "input": {"raw": "s3://demo-bucket/ocean-temp.zarr", "type": "zarr",
              "profile_hint": "training"},
    "environment": {"network": "available", "python": "3.12.4"},
    "assets": [ASSET],
    "score": 71.5,
    "tier": "C",
    "verdict": "NOT READY",
    "verdict_reason": "consumer-visible failure(s): C1-interactive",
    "confidence": "Reduced",
    "generated": "2026-08-27T00:00:00Z",
    "sample_frame": {"variable": "sea_water_temperature", "chunk_index": [0, 3, 3],
                     "seed": 42},
}


class GoldenFileTests(unittest.TestCase):
    """render(FINDINGS, style) must byte-exact-match tests/golden/report-<style>.md."""

    def _golden(self, style):
        return (GOLDEN_DIR / f"report-{style}.md").read_text()

    def test_verdict_style_matches_golden(self):
        self.assertEqual(rendering.render(FINDINGS, "verdict"), self._golden("verdict"))

    def test_score_style_matches_golden(self):
        self.assertEqual(rendering.render(FINDINGS, "score"), self._golden("score"))

    def test_both_style_matches_golden(self):
        self.assertEqual(rendering.render(FINDINGS, "both"), self._golden("both"))

    def test_verdict_line_is_literally_line_one_in_all_styles(self):
        for style in ("verdict", "score", "both"):
            doc = rendering.render(FINDINGS, style)
            first_line = doc.splitlines()[0]
            self.assertTrue(first_line.startswith("**Verdict:"),
                            f"style={style!r}: line 1 was {first_line!r}")

    def test_verdict_style_has_no_numeric_score_or_tier(self):
        doc = rendering.render(FINDINGS, "verdict")
        self.assertNotIn("/100", doc)
        self.assertNotIn("Tier ", doc)

    def test_score_and_both_styles_have_score_and_tier(self):
        for style in ("score", "both"):
            doc = rendering.render(FINDINGS, style)
            self.assertIn("/100", doc, f"style={style!r}")
            self.assertIn("Tier ", doc, f"style={style!r}")

    def test_dispersal_row_present_in_score_and_both(self):
        for style in ("score", "both"):
            doc = rendering.render(FINDINGS, style)
            self.assertIn("Metadata dispersal", doc, f"style={style!r}")
            self.assertIn("requests-to-open=2", doc, f"style={style!r}")

    def test_dispersal_row_absent_in_verdict_style(self):
        # verdict style has no scorecard at all, so no dispersal row either.
        doc = rendering.render(FINDINGS, "verdict")
        self.assertNotIn("Metadata dispersal", doc)

    def test_orientation_prose_present_in_all_styles(self):
        prose_snippet = "map-at-a-time workflows"
        for style in ("verdict", "score", "both"):
            doc = rendering.render(FINDINGS, style)
            self.assertIn("Chunk layout", doc, f"style={style!r}")
            self.assertIn(prose_snippet, doc, f"style={style!r}")

    def test_remediation_text_present(self):
        doc = rendering.render(FINDINGS, "both")
        self.assertIn("rewrite with ~2 MB chunks/tiles", doc)

    def test_current_codec_marker_in_benchmark_table(self):
        for style in ("score", "both"):
            doc = rendering.render(FINDINGS, style)
            self.assertIn("(current)", doc, f"style={style!r}")


class RobustnessTests(unittest.TestCase):
    """A real asset may have skipped an assessment; the renderer must
    degrade to a placeholder, never raise."""

    def _findings_with_none(self, **overrides):
        findings = copy.deepcopy(FINDINGS)
        findings["assets"][0].update(overrides)
        return findings

    def test_none_chunking_and_compression_render_placeholders(self):
        findings = self._findings_with_none(chunking=None, compression=None)
        for style in ("verdict", "score", "both"):
            doc = rendering.render(findings, style)
            self.assertIn("not assessed", doc, f"style={style!r}")

    def test_none_chunking_does_not_raise_in_orientation_or_chunking_table(self):
        findings = self._findings_with_none(chunking=None)
        doc = rendering.render(findings, "both")
        self.assertIn("not assessed", doc)

    def test_missing_smoke_and_telemetry_render_placeholders(self):
        findings = self._findings_with_none(smoke=None, telemetry=None)
        doc = rendering.render(findings, "both")
        self.assertIn("not assessed", doc)
        self.assertIn("no telemetry recorded", doc)

    def test_empty_assets_list_does_not_raise(self):
        findings = copy.deepcopy(FINDINGS)
        findings["assets"] = []
        for style in ("verdict", "score", "both"):
            doc = rendering.render(findings, style)
            self.assertTrue(doc.startswith("**Verdict:"))

    def test_missing_top_level_verdict_fields_do_not_raise(self):
        findings = {"assets": [{}]}
        doc = rendering.render(findings, "both")
        self.assertTrue(doc.startswith("**Verdict:"))

    def test_invalid_style_raises_value_error(self):
        with self.assertRaises(ValueError):
            rendering.render(FINDINGS, "html")

    def test_no_budget_breaches_renders_reassuring_placeholder(self):
        for style in ("verdict", "score", "both"):
            doc = rendering.render(FINDINGS, style)
            self.assertIn("No budget breaches", doc, f"style={style!r}")

    def test_budget_breach_is_visible_in_every_style(self):
        findings = self._findings_with_none(budget_breaches=[
            {"stage": "open", "elapsed_s": 12.3, "cap_s": 5.0, "breached": True,
             "message": "time cap 5.0s exceeded at stage 'open' (12.3s) - dataset "
                        "forces oversized reads"},
        ])
        for style in ("verdict", "score", "both"):
            doc = rendering.render(findings, style)
            self.assertIn("Budget / stage breaches", doc, f"style={style!r}")
            self.assertIn("open", doc, f"style={style!r}")
            self.assertIn("time cap 5.0s exceeded", doc, f"style={style!r}")


class WriteReportTests(unittest.TestCase):

    def test_write_report_writes_file_and_returns_path(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "cloud-readiness-out"
            path = rendering.write_report(FINDINGS, out_dir, "both")
            self.assertEqual(path, out_dir / "assessment-report.md")
            self.assertTrue(path.exists())
            content = path.read_text()
            self.assertEqual(content, rendering.render(FINDINGS, "both"))

    def test_write_report_json_roundtrip_of_findings_is_independent(self):
        # write_report doesn't mutate findings, and findings stays json-serializable
        # (sanity check the fixture matches what the CLI orchestrator will json.dump).
        import tempfile
        before = copy.deepcopy(FINDINGS)
        with tempfile.TemporaryDirectory() as tmp:
            rendering.write_report(FINDINGS, Path(tmp), "score")
        self.assertEqual(FINDINGS, before)
        json.dumps(FINDINGS)  # must not raise


if __name__ == "__main__":
    unittest.main()
