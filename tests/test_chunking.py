"""Offline tests for check_cloud_ready.chunking.

All fixtures are built in tmp dirs; no network access. h5py/zarr are
installed in this venv via the dev extra.
"""
import math
import os
import tempfile
import unittest

import numpy as np

from check_cloud_ready import chunking
from check_cloud_ready.telemetry import Budget

try:
    import h5py
except ImportError:
    h5py = None

try:
    import zarr
except ImportError:
    zarr = None

grade = chunking.grade
pick_interior_chunks = chunking.pick_interior_chunks
is_data_bearing = chunking.is_data_bearing
declare_orientation = chunking.declare_orientation
sample_chunk_sizes = chunking.sample_chunk_sizes
assess_chunking = chunking.assess_chunking


# --------------------------------------------------------------------- grade

class GradeTests(unittest.TestCase):
    def test_below_1mb_fails(self):
        band, note = grade(0.9)
        self.assertEqual(band, "fail")

    def test_1mb_edge_is_warn(self):
        band, _ = grade(1.0)
        self.assertEqual(band, "warn")

    def test_3_9mb_is_warn(self):
        band, _ = grade(3.9)
        self.assertEqual(band, "warn")

    def test_4mb_edge_is_pass(self):
        band, _ = grade(4.0)
        self.assertEqual(band, "pass")

    def test_16mb_edge_is_pass(self):
        band, _ = grade(16.0)
        self.assertEqual(band, "pass")

    def test_63_9mb_is_pass(self):
        band, _ = grade(63.9)
        self.assertEqual(band, "pass")

    def test_64_1mb_is_warn(self):
        band, _ = grade(64.1)
        self.assertEqual(band, "warn")

    def test_lean_note_present_16_to_64(self):
        _, note = grade(32.0)
        self.assertIn("lean", note.lower())

    def test_no_lean_note_in_middle_band(self):
        _, note = grade(8.0)
        self.assertNotIn("lean", note.lower())

    def test_none_is_unknown(self):
        band, note = grade(None)
        self.assertEqual(band, "unknown")


# --------------------------------------------------------- pick_interior_chunks

class PickInteriorChunksTests(unittest.TestCase):
    def test_first_candidate_is_centroid(self):
        out = pick_interior_chunks((100, 100), (10, 10), 5)
        self.assertEqual(out[0], (5, 5))

    def test_spiral_covers_distinct_indices(self):
        out = pick_interior_chunks((100, 100), (10, 10), 20)
        self.assertEqual(len(out), len(set(out)))

    def test_n_respected(self):
        out = pick_interior_chunks((100, 100), (10, 10), 7)
        self.assertEqual(len(out), 7)

    def test_n_larger_than_grid_returns_all(self):
        out = pick_interior_chunks((20, 20), (10, 10), 100)
        self.assertEqual(len(out), 4)
        self.assertEqual(len(set(out)), 4)

    def test_1d_shape(self):
        out = pick_interior_chunks((100,), (10,), 3)
        self.assertEqual(out[0], (5,))
        self.assertEqual(len(out), 3)


# -------------------------------------------------------------- is_data_bearing

class IsDataBearingTests(unittest.TestCase):
    def test_all_nan_rejected(self):
        arr = np.full((10, 10), np.nan, dtype="float32")
        self.assertFalse(is_data_bearing(arr))

    def test_95_percent_fill_rejected(self):
        arr = np.zeros((10, 10), dtype="float32")
        arr[0, 0:5] = np.arange(5)  # 5/100 = 5% real data
        self.assertFalse(is_data_bearing(arr, fill_value=0.0))

    def test_50_percent_fill_accepted(self):
        arr = np.zeros((10, 10), dtype="float32")
        arr[0:5, :] = np.random.rand(5, 10)  # 50% real data
        self.assertTrue(is_data_bearing(arr, fill_value=0.0))

    def test_integer_fill_value(self):
        arr = np.full((10, 10), -9999, dtype="int32")
        arr[0:5, :] = 42
        self.assertTrue(is_data_bearing(arr, fill_value=-9999))
        arr2 = np.full((10, 10), -9999, dtype="int32")
        arr2[0, 0] = 42
        self.assertFalse(is_data_bearing(arr2, fill_value=-9999))


# ------------------------------------------------------------ declare_orientation

class DeclareOrientationTests(unittest.TestCase):
    def test_timeseries_optimized(self):
        out = declare_orientation(["time", "y", "x"], [8760, 720, 1440], [8760, 32, 32])
        self.assertEqual(out["orientation"], "timeseries-optimized")
        # one map read touches ceil(720/32)*ceil(1440/32) chunks
        expected = math.ceil(720 / 32) * math.ceil(1440 / 32)
        self.assertIn(str(expected), out["prose"])
        self.assertIn("map_full_extent_one_time", out["queries"])
        self.assertEqual(out["queries"]["map_full_extent_one_time"]["chunks_touched"], expected)

    def test_map_optimized(self):
        out = declare_orientation(["time", "y", "x"], [8760, 720, 1440], [1, 720, 1440])
        self.assertEqual(out["orientation"], "map-optimized")
        expected = math.ceil(8760 / 1)
        self.assertIn(str(expected), out["prose"])
        self.assertEqual(
            out["queries"]["timeseries_full_depth_one_point"]["chunks_touched"], expected)

    def test_balanced(self):
        out = declare_orientation(["time", "y", "x"], [8760, 720, 1440], [168, 180, 360])
        self.assertEqual(out["orientation"], "balanced")
        map_touched = math.ceil(720 / 180) * math.ceil(1440 / 360)
        series_touched = math.ceil(8760 / 168)
        self.assertIn(str(map_touched), out["prose"])
        self.assertIn(str(series_touched), out["prose"])

    def test_positional_hedged(self):
        out = declare_orientation(["dim_0", "dim_1"], [1000, 50], [1, 50])
        self.assertIn("assuming axis 0 is the record dimension", out["prose"])

    def test_no_time_spatial_tiling(self):
        out = declare_orientation(["y", "x"], [10980, 10980], [512, 512])
        self.assertEqual(out["orientation"], "tiled")
        n_total = math.ceil(10980 / 512) * math.ceil(10980 / 512)
        self.assertIn(str(n_total), out["prose"])
        self.assertIn("512", out["prose"])

    def test_use_case_gates_amplification_grade(self):
        # map-optimized geometry: reading a timeseries at a point is bad.
        no_use_case = declare_orientation(
            ["time", "y", "x"], [8760, 720, 1440], [1, 720, 1440])
        self.assertIsNone(
            no_use_case["queries"]["timeseries_full_depth_one_point"]["grade"])

        graded = declare_orientation(
            ["time", "y", "x"], [8760, 720, 1440], [1, 720, 1440],
            use_case="timeseries")
        grade_val = graded["queries"]["timeseries_full_depth_one_point"]["grade"]
        self.assertIn(grade_val, ("warn", "fail"))

    def test_use_case_maps_grades_map_query(self):
        # timeseries-optimized geometry: reading a full map is bad.
        graded = declare_orientation(
            ["time", "y", "x"], [8760, 720, 1440], [8760, 32, 32],
            use_case="maps")
        grade_val = graded["queries"]["map_full_extent_one_time"]["grade"]
        self.assertIn(grade_val, ("warn", "fail"))

    def test_itemsize_scales_bytes_needed_and_amplification(self):
        # Finding 3: the amplification query's "bytes needed" baseline
        # must scale with the real per-variable itemsize, not stay
        # pinned to the 8-byte placeholder.
        dims, shape, chunks = ["time", "y", "x"], [8760, 720, 1440], [1, 720, 1440]
        out_default = declare_orientation(dims, shape, chunks)
        out_int16 = declare_orientation(dims, shape, chunks, itemsize=2)
        out_float64 = declare_orientation(dims, shape, chunks, itemsize=8)

        q_default = out_default["queries"]["timeseries_full_depth_one_point"]
        q_int16 = out_int16["queries"]["timeseries_full_depth_one_point"]
        q_float64 = out_float64["queries"]["timeseries_full_depth_one_point"]

        # default (no itemsize given) matches the explicit 8-byte case
        self.assertEqual(q_default["bytes_needed_estimate"], q_float64["bytes_needed_estimate"])
        # bytes needed scales linearly with itemsize (2 vs 8 bytes -> 4x)
        self.assertAlmostEqual(
            q_float64["bytes_needed_estimate"] / q_int16["bytes_needed_estimate"], 4.0, places=3)
        # amplification (bytes_transferred / bytes_needed) scales inversely,
        # so the narrower dtype reports a *larger* amplification for the
        # same transferred bytes -- this is exactly the "can flip the grade"
        # effect the finding describes.
        self.assertGreater(q_int16["amplification"], q_float64["amplification"])
        self.assertAlmostEqual(
            q_int16["amplification"] / q_float64["amplification"], 4.0, places=2)

    def test_itemsize_default_preserves_three_positional_arg_call(self):
        # The 3-positional-arg call form (no itemsize/use_case/avg_chunk_mb)
        # must keep working unchanged.
        out = declare_orientation(["y", "x"], [10980, 10980], [512, 512])
        self.assertEqual(out["orientation"], "tiled")


# ------------------------------------------------------------- sample_chunk_sizes

@unittest.skipUnless(zarr, "zarr not installed")
class SampleChunkSizesZarrTests(unittest.TestCase):
    def _make_store(self, tmp, all_fill=False):
        import fsspec
        storepath = os.path.join(tmp, "t.zarr")
        g = zarr.open_group(storepath, mode="w")
        arr = g.create_array(
            "x", shape=(40, 40), chunks=(10, 10), dtype="float32",
            fill_value=0.0, config={"write_empty_chunks": True},
        )
        data = np.zeros((40, 40), dtype="float32")
        if not all_fill:
            # interior chunks (rows/cols 10-30) get real data; corner stays fill
            rng = np.random.default_rng(0)
            data[10:30, 10:30] = rng.random((20, 20)).astype("float32")
        arr[:] = data
        fs = fsspec.filesystem("file")
        array_path = os.path.join(storepath, "x")
        return arr, fs, array_path

    def test_interior_data_bearing_measured(self):
        with tempfile.TemporaryDirectory() as tmp:
            arr, fs, array_path = self._make_store(tmp, all_fill=False)
            out = sample_chunk_sizes(arr, fs, array_path, engine="zarr", n=8)
            self.assertTrue(out["measured"])
            self.assertTrue(out["data_bearing"])
            self.assertTrue(len(out["sizes"]) > 0)
            self.assertTrue(all(s > 0 for s in out["sizes"]))

    def test_all_fill_store_reports_not_data_bearing(self):
        with tempfile.TemporaryDirectory() as tmp:
            arr, fs, array_path = self._make_store(tmp, all_fill=True)
            out = sample_chunk_sizes(arr, fs, array_path, engine="zarr", n=8)
            self.assertFalse(out["data_bearing"])
            self.assertIn("fill", out["note"].lower())

    def test_budget_cap_stops_sampling_gracefully(self):
        # Finding 4: S4 zarr sampling I/O must be budget-bounded. A tiny
        # byte_cap (smaller than a single stored interior chunk) should
        # make sampling stop after the first candidate fetch, rather than
        # reading unboundedly, and must not let BudgetExceeded escape.
        with tempfile.TemporaryDirectory() as tmp:
            arr, fs, array_path = self._make_store(tmp, all_fill=False)
            tiny_budget = Budget(byte_cap=50)

            out = sample_chunk_sizes(
                arr, fs, array_path, engine="zarr", n=8, budget=tiny_budget)

            # Graceful degradation: a note is present, no exception escaped
            # (the call above would have raised if it had), and sampling
            # did not proceed to read the whole store.
            self.assertIsNotNone(out["note"])
            self.assertIn("budget", out["note"].lower())
            # Real bytes were counted up to the point sampling stopped --
            # not zero (a measurement bug) and not unbounded (the whole
            # store, which is far larger than the 50-byte cap).
            self.assertGreater(tiny_budget.bytes, 0)
            self.assertLess(tiny_budget.bytes, 40 * 40 * 4)  # << full uncompressed array

    def test_generous_budget_does_not_cap_sampling(self):
        # Sanity check: a budget with ample headroom still measures
        # normally (the counting wrapper doesn't itself break sampling).
        with tempfile.TemporaryDirectory() as tmp:
            arr, fs, array_path = self._make_store(tmp, all_fill=False)
            roomy_budget = Budget(byte_cap=25 * 1024 * 1024)

            out = sample_chunk_sizes(
                arr, fs, array_path, engine="zarr", n=8, budget=roomy_budget)

            self.assertTrue(out["measured"])
            self.assertTrue(out["data_bearing"])
            self.assertTrue(len(out["sizes"]) > 0)
            self.assertGreater(roomy_budget.bytes, 0)
            self.assertGreater(roomy_budget.requests, 0)


@unittest.skipUnless(h5py, "h5py not installed")
class SampleChunkSizesHDF5Tests(unittest.TestCase):
    def test_measured_gzip_sizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.h5")
            with h5py.File(path, "w") as f:
                d = f.create_dataset(
                    "x", shape=(40, 40), chunks=(10, 10), dtype="f4",
                    compression="gzip", fillvalue=0.0,
                )
                rng = np.random.default_rng(1)
                d[:] = rng.random((40, 40)).astype("f4")
            with h5py.File(path, "r") as f:
                d = f["x"]
                out = sample_chunk_sizes(d, None, None, engine="hdf5", n=8)
                self.assertTrue(out["measured"])
                self.assertTrue(out["data_bearing"])
                self.assertGreater(len(out["sizes"]), 0)
                self.assertTrue(all(s > 0 for s in out["sizes"]))
                median_mb = np.median(out["sizes"]) / 2**20
                self.assertGreater(median_mb, 0)

    def test_contiguous_dataset_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.h5")
            with h5py.File(path, "w") as f:
                f.create_dataset("x", data=np.zeros((10, 10), dtype="f4"))
            with h5py.File(path, "r") as f:
                d = f["x"]
                out = sample_chunk_sizes(d, None, None, engine="hdf5", n=8)
                self.assertFalse(out["measured"])
                self.assertIsNotNone(out["note"])
                self.assertIn("contiguous", out["note"].lower())


# ------------------------------------------------------------- assess_chunking

@unittest.skipUnless(h5py, "h5py not installed")
class AssessChunkingHDF5Tests(unittest.TestCase):
    def _open(self, path):
        from check_cloud_ready import openers
        budget = Budget()
        result = openers.open_dataset("hdf5", None, path, budget)
        return result, budget

    def test_use_case_gating_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.h5")
            with h5py.File(path, "w") as f:
                d = f.create_dataset(
                    "field", shape=(200, 100, 100), chunks=(1, 100, 100),
                    dtype="f4", compression="gzip",
                )
                rng = np.random.default_rng(2)
                d[:] = rng.random((200, 100, 100)).astype("f4")
            result, budget = self._open(path)
            self.assertEqual(result["status"], "ok")
            variables = [dict(v, dims=["time", "y", "x"]) for v in result["inventory"]]

            out_no_use_case = assess_chunking(
                result["handle"], None, path, variables, budget=budget,
                use_case=None, engine="hdf5")
            var0 = out_no_use_case["variables"][0]
            self.assertIsNone(
                var0["orientation"]["queries"]["timeseries_full_depth_one_point"]["grade"])

            out_gated = assess_chunking(
                result["handle"], None, path, variables, budget=budget,
                use_case="timeseries", engine="hdf5")
            var0g = out_gated["variables"][0]
            grade_val = var0g["orientation"]["queries"][
                "timeseries_full_depth_one_point"]["grade"]
            self.assertIn(grade_val, ("warn", "fail"))
            result["handle"].close()

    def test_contiguous_variable_reports_estimate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.h5")
            with h5py.File(path, "w") as f:
                f.create_dataset("plain", data=np.zeros((100, 100), dtype="f4"))
            result, budget = self._open(path)
            variables = [dict(v, dims=["y", "x"]) for v in result["inventory"]]
            out = assess_chunking(
                result["handle"], None, path, variables, budget=budget,
                use_case=None, engine="hdf5")
            var0 = out["variables"][0]
            self.assertFalse(var0["measured"])
            self.assertTrue(
                any("contiguous" in n.lower() for n in var0["coordinate_chunking_notes"]))
            result["handle"].close()

    def test_contiguous_variable_orientation_is_declare_orientation_shaped(self):
        # Finding 1: a contiguous (unchunked) variable must get a real
        # declare_orientation-shaped dict, not None -- otherwise any
        # consumer indexing variable["orientation"]["queries"]/["prose"]
        # would crash.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.h5")
            with h5py.File(path, "w") as f:
                f.create_dataset("contig", data=np.zeros((100, 100), dtype="f4"))
                f.create_dataset(
                    "chunked", shape=(100, 100), chunks=(20, 20),
                    dtype="f4", compression="gzip")
                f["chunked"][:] = np.random.default_rng(3).random((100, 100)).astype("f4")
            result, budget = self._open(path)
            variables = [dict(v, dims=["y", "x"]) for v in result["inventory"]]
            out = assess_chunking(
                result["handle"], None, path, variables, budget=budget,
                use_case=None, engine="hdf5")
            by_name = {v["name"]: v for v in out["variables"]}
            contig_orientation = by_name["/contig"]["orientation"]
            chunked_orientation = by_name["/chunked"]["orientation"]

            self.assertIsNotNone(contig_orientation)
            self.assertEqual(set(contig_orientation.keys()), set(chunked_orientation.keys()))
            self.assertIn("orientation", contig_orientation)
            self.assertIn("prose", contig_orientation)
            self.assertIn("queries", contig_orientation)
            # Must not crash on the documented access patterns.
            self.assertIsInstance(contig_orientation["queries"], dict)
            self.assertIsInstance(contig_orientation["prose"], str)
            result["handle"].close()


# --------------------------------------------------------------- profiles

class AgenticProfileNoteTests(unittest.TestCase):
    def test_agentic_note_via_assess_chunking(self):
        # Finding 2: the brief (task-7-brief.md line 13) says "<=3-requests
        # -to-schema"; the profile note must say 3, not 1 (transcription
        # error) -- exercised end-to-end via assess_chunking, which is
        # where the profile overlay is actually built.
        with tempfile.TemporaryDirectory() as tmp:
            if h5py is None:
                self.skipTest("h5py not installed")
            path = os.path.join(tmp, "t.h5")
            with h5py.File(path, "w") as f:
                d = f.create_dataset(
                    "x", shape=(40, 40), chunks=(10, 10), dtype="f4",
                    compression="gzip",
                )
                d[:] = np.random.default_rng(4).random((40, 40)).astype("f4")
            from check_cloud_ready import openers
            budget = Budget()
            result = openers.open_dataset("hdf5", None, path, budget)
            variables = [dict(v, dims=["y", "x"]) for v in result["inventory"]]
            out = assess_chunking(
                result["handle"], None, path, variables, budget=budget,
                use_case=None, engine="hdf5")
            note = out["variables"][0]["profiles"]["agentic"]["note"]
            self.assertIn("<=3 requests", note)
            self.assertNotIn("<=1 request", note)
            result["handle"].close()


if __name__ == "__main__":
    unittest.main()
