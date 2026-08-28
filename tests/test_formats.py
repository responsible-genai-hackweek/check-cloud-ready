"""Offline tests for check_cloud_ready.formats.

No network access. Byte fixtures are built in-test; store layouts are
probed against a tiny dict-backed fake fs.
"""
import json
import sys
import unittest
from unittest import mock

from check_cloud_ready import formats

detect_input = formats.detect_input
ext_hint = formats.ext_hint
sniff_bytes = formats.sniff_bytes
sniff_store = formats.sniff_store
refine_hdf5 = formats.refine_hdf5
format_class = formats.format_class

HDF5_MAGIC = b"\x89HDF\r\n\x1a\n"


class FakeFS:
    """Minimal dict-backed fake fs: .exists()/.cat_file() only."""

    def __init__(self, paths=(), files=None):
        self.paths = set(paths)
        self.files = files or {}

    def exists(self, path):
        return path in self.paths

    def cat_file(self, path):
        return self.files[path]


class TestSniffBytesMagic(unittest.TestCase):
    def test_hdf5_at_offset_zero(self):
        head = HDF5_MAGIC + b"\x00" * 24
        out = sniff_bytes(head)
        self.assertEqual(out["format"], "hdf5")
        self.assertEqual(out["confidence"], "magic")
        self.assertIsInstance(out["notes"], list)

    def test_hdf5_at_offset_512_via_offset_reads(self):
        head = b"\x00" * 32  # no signature in the head itself (user block)

        def offset_reads(offset, size):
            if offset == 512:
                return HDF5_MAGIC + b"\x00" * (size - len(HDF5_MAGIC))
            return b"\x00" * size

        out = sniff_bytes(head, offset_reads=offset_reads)
        self.assertEqual(out["format"], "hdf5")
        self.assertEqual(out.get("hdf5_superblock_offset"), 512)

    def test_hdf5_offset_not_found_without_offset_reads(self):
        head = b"\x00" * 32
        out = sniff_bytes(head)
        self.assertNotEqual(out["format"], "hdf5")

    def test_netcdf3_classic(self):
        out = sniff_bytes(b"CDF\x01" + b"\x00" * 20)
        self.assertEqual(out["format"], "netcdf3")

    def test_hdf4(self):
        out = sniff_bytes(b"\x0e\x03\x13\x01" + b"\x00" * 20)
        self.assertEqual(out["format"], "hdf4")

    def test_tiff_little_endian(self):
        out = sniff_bytes(b"II*\x00" + b"\x00" * 20)
        self.assertEqual(out["format"], "cog")

    def test_tiff_big_endian(self):
        out = sniff_bytes(b"MM\x00*" + b"\x00" * 20)
        self.assertEqual(out["format"], "cog")

    def test_bigtiff_little_endian(self):
        out = sniff_bytes(b"II+\x00" + b"\x00" * 20)
        self.assertEqual(out["format"], "cog")

    def test_bigtiff_big_endian(self):
        out = sniff_bytes(b"MM\x00+" + b"\x00" * 20)
        self.assertEqual(out["format"], "cog")

    def test_parquet(self):
        out = sniff_bytes(b"PAR1" + b"\x00" * 20)
        self.assertEqual(out["format"], "parquet")

    def test_grib(self):
        out = sniff_bytes(b"GRIB" + b"\x00" * 20)
        self.assertEqual(out["format"], "grib2")

    def test_flatgeobuf(self):
        out = sniff_bytes(b"fgb" + b"\x00" * 20)
        self.assertEqual(out["format"], "flatgeobuf")

    def test_las(self):
        out = sniff_bytes(b"LASF" + b"\x00" * 20)
        self.assertEqual(out["format"], "las")

    def test_pmtiles(self):
        out = sniff_bytes(b"PMTiles" + b"\x00" * 20)
        self.assertEqual(out["format"], "pmtiles")

    def test_zip(self):
        out = sniff_bytes(b"PK\x03\x04" + b"\x00" * 20)
        self.assertEqual(out["format"], "zip")

    def test_gzip(self):
        out = sniff_bytes(b"\x1f\x8b" + b"\x00" * 20)
        self.assertEqual(out["format"], "gzip")

    def test_shapefile(self):
        out = sniff_bytes(b"\x00\x00\x27\x0a" + b"\x00" * 20)
        self.assertEqual(out["format"], "shapefile")

    def test_tar_from_long_head(self):
        head = bytearray(300)
        head[257:262] = b"ustar"
        out = sniff_bytes(bytes(head))
        self.assertEqual(out["format"], "tar")

    def test_tar_via_offset_reads_when_head_short(self):
        head = b"\x00" * 32  # too short to contain byte 257 directly

        def offset_reads(offset, size):
            if offset == 257:
                return b"ustar"
            return b"\x00" * size

        out = sniff_bytes(head, offset_reads=offset_reads)
        self.assertEqual(out["format"], "tar")

    def test_short_head_not_mistaken_for_tar_without_offset_reads(self):
        head = b"\x00" * 32
        out = sniff_bytes(head)
        self.assertNotEqual(out["format"], "tar")

    def test_kerchunk_json_refs(self):
        blob = json.dumps({"refs": {"a": "b"}, "version": 1}).encode()
        out = sniff_bytes(blob)
        self.assertEqual(out["format"], "kerchunk")

    def test_unrecognized_json(self):
        blob = json.dumps({"hello": "world"}).encode()
        out = sniff_bytes(blob)
        self.assertNotEqual(out["format"], "kerchunk")

    def test_unrecognized_bytes(self):
        out = sniff_bytes(b"\xff\xfe\xfd\xfc" + b"\x00" * 20)
        self.assertEqual(out["format"], "unknown")
        self.assertEqual(out["confidence"], "magic")


class TestSniffStore(unittest.TestCase):
    def test_zarr_v3(self):
        fs = FakeFS(paths={"root/zarr.json"})
        out = sniff_store(fs, "root")
        self.assertEqual(out["format"], "zarr")
        self.assertEqual(out.get("zarr_version"), "v3")
        self.assertEqual(out["confidence"], "store-layout")

    def test_zarr_v2_consolidated(self):
        fs = FakeFS(paths={"root/.zmetadata", "root/.zgroup"})
        out = sniff_store(fs, "root")
        self.assertEqual(out["format"], "zarr")
        self.assertEqual(out.get("zarr_version"), "v2")
        self.assertTrue(out.get("consolidated_metadata"))

    def test_zarr_v2_unconsolidated_has_dispersal_note(self):
        fs = FakeFS(paths={"root/.zgroup"})
        out = sniff_store(fs, "root")
        self.assertEqual(out["format"], "zarr")
        self.assertFalse(out.get("consolidated_metadata"))
        self.assertTrue(any("dispersal" in n.lower() or "crawl" in n.lower()
                            for n in out["notes"]))

    def test_icechunk_snapshots(self):
        fs = FakeFS(paths={"root/refs", "root/snapshots"})
        out = sniff_store(fs, "root")
        self.assertEqual(out["format"], "icechunk")

    def test_icechunk_manifests(self):
        fs = FakeFS(paths={"root/refs", "root/manifests"})
        out = sniff_store(fs, "root")
        self.assertEqual(out["format"], "icechunk")

    def test_no_recognized_layout(self):
        fs = FakeFS(paths=set())
        out = sniff_store(fs, "root")
        self.assertEqual(out["format"], "unknown")
        self.assertEqual(out["confidence"], "store-layout")


class TestRefineHdf5(unittest.TestCase):
    def test_h5py_missing_returns_unrefined_with_note(self):
        # h5py may or may not be installed in this environment (the
        # openers.py dev extra pulls it in for tests/test_openers.py), so
        # force the ImportError branch explicitly rather than relying on
        # ambient absence: assigning None in sys.modules makes `import
        # h5py` raise ImportError regardless of what's actually installed.
        with mock.patch.dict(sys.modules, {"h5py": None}):
            out = refine_hdf5(None, None)
        self.assertEqual(out.get("refinement"), "skipped: h5py not installed")
        self.assertIn("format", out)


class TestDetectInput(unittest.TestCase):
    def test_granule_id_asf(self):
        self.assertEqual(detect_input("G4289749526-ASF"), "granule-id")

    def test_granule_id_pocloud(self):
        self.assertEqual(detect_input("G123-POCLOUD"), "granule-id")

    def test_https_url(self):
        self.assertEqual(detect_input("https://x/y.nc"), "url")

    def test_s3_url(self):
        self.assertEqual(detect_input("s3://b/k"), "url")

    def test_gs_url(self):
        self.assertEqual(detect_input("gs://b/k"), "url")

    def test_az_url(self):
        self.assertEqual(detect_input("az://b/k"), "url")

    def test_abfs_url(self):
        self.assertEqual(detect_input("abfs://b/k"), "url")

    def test_existing_local_file(self):
        import tempfile
        with tempfile.NamedTemporaryFile() as f:
            self.assertEqual(detect_input(f.name), "local")

    def test_not_a_granule_is_not_granule_id(self):
        self.assertNotEqual(detect_input("not-a-granule-G12"), "granule-id")


class TestExtHint(unittest.TestCase):
    def test_nc(self):
        self.assertEqual(ext_hint("foo.nc"), formats.EXT_FORMATS[".nc"])

    def test_zarr(self):
        self.assertEqual(ext_hint("foo.zarr"), "zarr")

    def test_tif(self):
        self.assertEqual(ext_hint("foo.tif"), "cog")

    def test_h5(self):
        self.assertEqual(ext_hint("foo.h5"), "hdf5")

    def test_unknown_ext(self):
        self.assertIsNone(ext_hint("foo.bogus-extension"))

    def test_url_with_path(self):
        self.assertEqual(ext_hint("https://example.com/data/foo.tif?x=1"), "cog")

    def test_copc_more_specific_than_laz(self):
        self.assertEqual(ext_hint("foo.copc.laz"), "copc")


class TestFormatClass(unittest.TestCase):
    def test_zarr_is_cloud_native(self):
        self.assertEqual(format_class("zarr"), "cloud-native")

    def test_netcdf4_is_cloud_optimizable(self):
        self.assertEqual(format_class("netcdf4"), "cloud-optimizable")

    def test_hdf4_is_cloud_hostile(self):
        self.assertEqual(format_class("hdf4"), "cloud-hostile")

    def test_unknown_format(self):
        self.assertEqual(format_class("something-made-up"), "unknown")


if __name__ == "__main__":
    unittest.main()
