"""Offline tests for check_cloud_ready.compression.

No network. numcodecs is installed via the dev extra (same as zarr's dev
group), so the benchmark grid runs for real rather than being skipped in
this environment.
"""
import unittest

import numpy as np

from check_cloud_ready import compression

inspect_codec = compression.inspect_codec
benchmark = compression.benchmark
assess_compression = compression.assess_compression

try:
    import numcodecs  # noqa: F401
    HAVE_NUMCODECS = True
except ImportError:
    HAVE_NUMCODECS = False


def _big_array(seed=0, n=200_000, dtype="f4"):
    # n=200_000 * 4 bytes = ~800 KB, comfortably over the 64 KB guard.
    rng = np.random.default_rng(seed)
    return rng.random(n).astype(dtype)


# --------------------------------------------------------------- inspect_codec

class InspectCodecTests(unittest.TestCase):
    def test_s3_regression_gzip_reported_per_variable_not_top_level_bug(self):
        # The old bug read a single top-level "compression" key (e.g.
        # lazy_open["compression"]) and reported "no compression observed"
        # on a fully gzip-compressed file. inspect_codec must read the
        # per-variable "codec" field on each record instead.
        variables = [
            {"name": "/a", "codec": "gzip(4)+shuffle"},
            {"name": "/b", "codec": "gzip(4)+shuffle"},
            {"name": "/c", "codec": "gzip(4)+shuffle"},
        ]
        out = inspect_codec(variables)
        self.assertEqual(len(out), 3)
        for rec in out:
            self.assertEqual(rec["status"], "warn")
            self.assertIn("gzip", rec["codec"])
            self.assertNotIn("no compression observed", rec["note"].lower())
            self.assertIsNotNone(rec["remediation"])
            self.assertIn("zstd", rec["remediation"].lower())
            self.assertIn("shuffle", rec["remediation"].lower())

    def test_uncompressed_fails_with_remediation(self):
        variables = [{"name": "/x", "codec": None}]
        out = inspect_codec(variables)
        self.assertEqual(out[0]["status"], "fail")
        self.assertIsNotNone(out[0]["remediation"])

    def test_zstd_shuffle_passes(self):
        variables = [{"name": "/x", "codec": "zstd+shuffle"}]
        out = inspect_codec(variables)
        self.assertEqual(out[0]["status"], "pass")

    def test_blosc_shuffle_passes(self):
        variables = [{"name": "/x", "codec": "blosc-zstd-3+shuffle"}]
        out = inspect_codec(variables)
        self.assertEqual(out[0]["status"], "pass")

    def test_unknown_codec_warns_with_note(self):
        variables = [{"name": "/x", "codec": "some-exotic-codec-xyz"}]
        out = inspect_codec(variables)
        self.assertEqual(out[0]["status"], "warn")
        self.assertIsNotNone(out[0]["note"])

    def test_output_shape_per_record(self):
        variables = [{"name": "/x", "codec": "zstd"}]
        out = inspect_codec(variables)
        rec = out[0]
        for key in ("name", "codec", "status", "note", "remediation"):
            self.assertIn(key, rec)
        self.assertEqual(rec["name"], "/x")


# ------------------------------------------------------------------ benchmark

@unittest.skipUnless(HAVE_NUMCODECS, "numcodecs not installed")
class BenchmarkTests(unittest.TestCase):
    def test_gzip_current_codec_row_one(self):
        data = _big_array()
        rows = benchmark(data, current_codec="gzip")
        self.assertGreater(len(rows), 0)
        row0 = rows[0]
        self.assertTrue(row0.get("is_current"))
        self.assertIn("gzip", row0["config"].lower())
        self.assertIsNotNone(row0.get("ratio"))
        self.assertGreater(row0["ratio"], 0)

    def test_all_ratios_positive(self):
        data = _big_array()
        rows = benchmark(data, current_codec="gzip")
        for row in rows:
            if "ratio" in row and row["ratio"] is not None:
                self.assertGreater(row["ratio"], 0)

    def test_grid_contains_zstd_levels_and_blosc(self):
        data = _big_array()
        rows = benchmark(data, current_codec="gzip")
        configs = {row["config"] for row in rows}
        for expected in ("zstd-1", "zstd-3", "zstd-5"):
            self.assertTrue(any(expected in c for c in configs),
                             f"missing {expected} in {configs}")
        self.assertTrue(any("blosc-lz4" in c for c in configs))
        self.assertTrue(any("blosc-zstd-3" in c for c in configs))
        # shuffle and no-shuffle variants both present
        self.assertTrue(any("shuffle" in c and "noshuffle" not in c for c in configs))

    def test_current_codec_none_is_uncompressed_baseline(self):
        data = _big_array()
        rows = benchmark(data, current_codec=None)
        row0 = rows[0]
        self.assertTrue(row0["is_current"])
        self.assertEqual(row0["ratio"], 1.0)
        self.assertIn("uncompressed", row0["config"].lower())

    def test_exotic_codec_placeholder_row_grid_still_runs(self):
        data = _big_array()
        rows = benchmark(data, current_codec="lzf")
        row0 = rows[0]
        self.assertTrue(row0.get("is_current"))
        self.assertIn("lzf", row0["config"].lower())
        self.assertEqual(row0.get("error"), "cannot reproduce locally")
        # grid still ran after the placeholder
        self.assertGreater(len(rows), 1)
        configs = {row["config"] for row in rows[1:]}
        self.assertTrue(any("zstd-1" in c for c in configs))

    def test_keepbits_adds_lossy_row_with_positive_max_abs_error(self):
        data = _big_array()
        rows = benchmark(data, current_codec="gzip", keepbits=8)
        lossy_rows = [r for r in rows if r.get("lossy")]
        self.assertTrue(len(lossy_rows) > 0)
        for r in lossy_rows:
            if "max_abs_error" in r:
                self.assertGreater(r["max_abs_error"], 0)

    def test_no_keepbits_means_no_lossy_rows(self):
        data = _big_array()
        rows = benchmark(data, current_codec="gzip", keepbits=None)
        self.assertFalse(any(r.get("lossy") for r in rows))

    def test_small_array_returns_empty(self):
        data = np.zeros(10, dtype="f4")  # 40 bytes, well under 64 KB
        rows = benchmark(data, current_codec="gzip")
        self.assertEqual(rows, [])

    def test_determinism_same_configs_and_ratios(self):
        data = _big_array(seed=42)
        rows1 = benchmark(data, current_codec="zstd")
        rows2 = benchmark(data, current_codec="zstd")
        configs1 = [r["config"] for r in rows1]
        configs2 = [r["config"] for r in rows2]
        self.assertEqual(configs1, configs2)
        ratios1 = [r.get("ratio") for r in rows1]
        ratios2 = [r.get("ratio") for r in rows2]
        self.assertEqual(ratios1, ratios2)


# ------------------------------------------------------------ assess_compression

class AssessCompressionTests(unittest.TestCase):
    def test_inspection_always_runs(self):
        variables = [{"name": "/a", "codec": "gzip"}]
        out = assess_compression(variables, sample=None, run_benchmark=False)
        self.assertEqual(len(out["inspection"]), 1)
        self.assertIsNone(out["benchmark"])

    @unittest.skipUnless(HAVE_NUMCODECS, "numcodecs not installed")
    def test_benchmark_runs_when_requested_and_sample_given(self):
        variables = [{"name": "/a", "codec": "gzip"}]
        sample = _big_array()
        out = assess_compression(variables, sample=sample, run_benchmark=True)
        self.assertIsNotNone(out["benchmark"])
        self.assertGreater(len(out["benchmark"]), 0)

    def test_benchmark_not_run_when_sample_is_none_even_if_requested(self):
        variables = [{"name": "/a", "codec": "gzip"}]
        out = assess_compression(variables, sample=None, run_benchmark=True)
        self.assertIsNone(out["benchmark"])

    @unittest.skipUnless(HAVE_NUMCODECS, "numcodecs not installed")
    def test_small_sample_note_surfaced(self):
        variables = [{"name": "/a", "codec": "gzip"}]
        sample = np.zeros(10, dtype="f4")
        out = assess_compression(variables, sample=sample, run_benchmark=True)
        self.assertEqual(out["benchmark"], [])
        self.assertTrue(any("64" in n for n in out["notes"]))


if __name__ == "__main__":
    unittest.main()
