"""Offline tests for check_cloud_ready.openers.

All fixtures are built in tmp dirs; no network access. Each dependency
class skips itself if the heavy dep isn't installed in this venv
(rasterio is expected to be absent -- its absence is itself a test
case, see MissingDepTests).
"""
import json
import os
import tempfile
import unittest

import fsspec

from check_cloud_ready import openers
from check_cloud_ready.telemetry import Budget

try:
    import h5py
except ImportError:
    h5py = None

try:
    import zarr
except ImportError:
    zarr = None

try:
    import pyarrow as pa
    import pyarrow.parquet as papq
except ImportError:
    pa = None
    papq = None

try:
    import rasterio
except ImportError:
    rasterio = None


def _local_fs():
    return fsspec.filesystem("file")


class FakeAuthFS:
    """Fake fs whose open() always raises an auth-shaped error, so the
    "PermissionError during open => skipped, not failed" behavior can
    be tested without any real dependency or network access."""

    protocol = "fake"

    def open(self, path, mode="rb", **kwargs):
        raise PermissionError("403 Forbidden")


@unittest.skipUnless(h5py, "h5py not installed")
class HDF5OpenerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "test.h5")
        with h5py.File(self.path, "w") as f:
            data = f.create_dataset(
                "science/grids/data", data=[[1.0] * 10 for _ in range(10)],
                chunks=(5, 5), compression="gzip", shuffle=True,
            )
            data.attrs["grid_mapping"] = "projection"
            data.attrs["units"] = "m"
            proj = f.create_dataset("projection", shape=(), dtype="i4")
            proj.attrs["spatial_ref"] = "GEOGCRS[\"WGS 84\",...]"
            ident = f.create_group("identification")
            ident.attrs["shortName"] = "TESTPRODUCT"
            other = f.create_group("other/nested/deep")
            other.attrs["foo"] = "bar"

    def _open(self):
        budget = Budget()
        result = openers.open_dataset("hdf5", _local_fs(), self.path, budget)
        return result, budget

    def test_status_ok(self):
        result, _ = self._open()
        self.assertEqual(result["status"], "ok")
        self.assertIsNotNone(result["handle"])
        result["handle"].close()

    def test_inventory_has_full_paths(self):
        result, _ = self._open()
        names = {v["name"] for v in result["inventory"]}
        self.assertIn("/science/grids/data", names)
        self.assertIn("/projection", names)
        result["handle"].close()

    def test_per_variable_codec_mentions_gzip_and_shuffle(self):
        result, _ = self._open()
        rec = next(v for v in result["inventory"] if v["name"] == "/science/grids/data")
        self.assertIn("gzip", rec["codec"])
        self.assertIn("shuffle", rec["codec"])
        proj_rec = next(v for v in result["inventory"] if v["name"] == "/projection")
        self.assertIsNone(proj_rec["codec"])
        result["handle"].close()

    def test_ordinary_dataset_attrs_are_also_sampled(self):
        """S6 covers CRS containers *in addition to* ordinary sampled
        attrs -- a plain non-CRS attr on the main dataset must still be
        captured, not only attrs on recognized CRS containers."""
        result, _ = self._open()
        rec = next(v for v in result["inventory"] if v["name"] == "/science/grids/data")
        self.assertEqual(rec["attrs"].get("units"), "m")
        self.assertEqual(rec["attrs"].get("grid_mapping"), "projection")
        result["handle"].close()

    def test_projection_attrs_captured_in_crs_containers(self):
        result, _ = self._open()
        containers = result["format_checks"]["crs_containers"]
        names = {c["name"] for c in containers}
        self.assertIn("/projection", names)
        proj = next(c for c in containers if c["name"] == "/projection")
        self.assertIn("spatial_ref", proj["attrs"])
        result["handle"].close()

    def test_metadata_walk_visits_identification_early_and_completes(self):
        result, _ = self._open()
        walk = result["metadata_walk"]
        self.assertIsNotNone(walk)
        self.assertGreater(walk["requests"], 0)
        self.assertTrue(walk["complete"])
        self.assertIsNone(walk["capped_at"])
        order = walk["visit_order"]
        self.assertIn("/identification", order)
        self.assertIn("/other", order)
        self.assertLess(order.index("/identification"), order.index("/other"))
        result["handle"].close()

    def test_requests_and_bytes_to_open(self):
        result, _ = self._open()
        self.assertGreaterEqual(result["telemetry"]["requests_to_open"], 1)
        self.assertGreater(result["telemetry"]["bytes_to_open"], 0)
        result["handle"].close()


@unittest.skipUnless(zarr, "zarr not installed")
class ZarrOpenerTests(unittest.TestCase):
    def _make_store(self, zarr_format):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "store.zarr")
        g = zarr.open_group(path, mode="w", zarr_format=zarr_format)
        arr = g.create_array("data", shape=(20, 20), chunks=(5, 5), dtype="f4")
        arr.attrs["grid_mapping"] = "projection"
        g.create_array("projection", shape=(), dtype="i4")
        zarr.consolidate_metadata(g.store)
        return path

    def _assert_common(self, result):
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["format_checks"]["consolidated"])
        self.assertGreater(result["telemetry"]["bytes_to_open"], 0)
        rec = next(v for v in result["inventory"] if v["name"] == "/data")
        self.assertEqual(rec["chunks"], [5, 5])
        self.assertIsNotNone(rec["codec"])
        crs_names = {c["name"] for c in result["format_checks"]["crs_containers"]}
        self.assertIn("/projection", crs_names)

    def test_v3_consolidated(self):
        path = self._make_store(3)
        budget = Budget()
        result = openers.open_dataset("zarr", _local_fs(), path, budget)
        self._assert_common(result)
        self.assertEqual(result["format_checks"]["zarr_version"], 3)

    def test_v2_consolidated(self):
        path = self._make_store(2)
        budget = Budget()
        result = openers.open_dataset("zarr", _local_fs(), path, budget)
        self._assert_common(result)
        self.assertEqual(result["format_checks"]["zarr_version"], 2)


@unittest.skipUnless(papq, "pyarrow not installed")
class ParquetOpenerTests(unittest.TestCase):
    def test_open_ok_columns_inventory(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "t.parquet")
        tbl = pa.table({"a": list(range(1000)), "b": [float(i) for i in range(1000)]})
        papq.write_table(tbl, path, compression="snappy")

        budget = Budget()
        result = openers.open_dataset("parquet", _local_fs(), path, budget)

        self.assertEqual(result["status"], "ok")
        names = {v["name"] for v in result["inventory"]}
        self.assertEqual(names, {"a", "b"})
        rec = next(v for v in result["inventory"] if v["name"] == "a")
        self.assertEqual(rec["dims"], [])
        self.assertEqual(rec["shape"], [1000])
        self.assertEqual(rec["codec"], "SNAPPY")
        self.assertGreater(result["telemetry"]["bytes_to_open"], 0)


class MissingDepOpenerTests(unittest.TestCase):
    def test_cog_opener_skips_when_rasterio_missing(self):
        self.assertIsNone(rasterio, "expected rasterio to be absent in this venv")
        budget = Budget()
        result = openers.open_dataset("cog", _local_fs(), "/nonexistent/path.tif", budget)
        self.assertEqual(result["status"], "skipped")
        self.assertIn("rasterio not installed", result["reason"])


@unittest.skipUnless(h5py, "h5py not installed")
class AuthErrorOpenerTests(unittest.TestCase):
    def test_auth_error_during_open_is_skipped_not_failed(self):
        budget = Budget()
        result = openers.open_dataset("hdf5", FakeAuthFS(), "/some/path.h5", budget)
        self.assertEqual(result["status"], "skipped")
        self.assertIn("403", result["reason"])


class UnknownFormatOpenerTests(unittest.TestCase):
    def test_unknown_format_is_na(self):
        budget = Budget()
        result = openers.open_dataset("some-bogus-format", _local_fs(), "/x", budget)
        self.assertEqual(result["status"], "n/a")


if __name__ == "__main__":
    unittest.main()
