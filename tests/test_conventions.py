"""Offline tests for check_cloud_ready.conventions.

No network, no heavy deps. Zarr-store-shaped tests use the same
dict-backed fake-fs pattern as tests/test_formats.py's FakeFS
(``.exists()``/``.cat_file()`` only).
"""
import json
import unittest

from check_cloud_ready import conventions

check_cf = conventions.check_cf
check_geozarr = conventions.check_geozarr


class FakeFS:
    """Minimal dict-backed fake fs: .exists()/.cat_file() only (same
    shape as tests/test_formats.py's FakeFS)."""

    def __init__(self, files=None):
        self.files = files or {}

    def exists(self, path):
        return path in self.files

    def cat_file(self, path):
        return self.files[path]


def _checks_by_id(result):
    return {c["id"]: c for c in result["checks"]}


# --------------------------------------------------------------- fixtures

def _cmip_like():
    variables = [
        {"name": "/tas", "dims": ["time", "lat", "lon"], "shape": [10, 5, 5],
         "dtype": "float32", "chunks": None,
         "attrs": {"units": "K", "standard_name": "air_temperature",
                   "grid_mapping": "crs"},
         "size_bytes": None, "codec": None},
        {"name": "/time", "dims": ["time"], "shape": [10], "dtype": "float64",
         "chunks": None,
         "attrs": {"units": "days since 1850-01-01", "calendar": "noleap",
                   "standard_name": "time"},
         "size_bytes": None, "codec": None},
        {"name": "/lat", "dims": ["lat"], "shape": [5], "dtype": "float32",
         "chunks": None,
         "attrs": {"units": "degrees_north", "standard_name": "latitude"},
         "size_bytes": None, "codec": None},
        {"name": "/lon", "dims": ["lon"], "shape": [5], "dtype": "float32",
         "chunks": None,
         "attrs": {"units": "degrees_east", "standard_name": "longitude"},
         "size_bytes": None, "codec": None},
        {"name": "/crs", "dims": [], "shape": [], "dtype": "int32",
         "chunks": None,
         "attrs": {"grid_mapping_name": "rotated_latitude_longitude"},
         "size_bytes": None, "codec": None},
    ]
    global_attrs = {"Conventions": "CF-1.7"}
    return variables, global_attrs


class CheckCFCmipLikeTests(unittest.TestCase):
    def test_pass_all_checks_ok(self):
        variables, global_attrs = _cmip_like()
        out = check_cf(variables, global_attrs)
        self.assertEqual(out["status"], "pass")
        for c in out["checks"]:
            self.assertIn(c["ok"], (True, None), msg=c)
        # At least one check must be genuinely applicable (True), not
        # every check vacuously N/A.
        self.assertTrue(any(c["ok"] is True for c in out["checks"]))

    def test_determinism_dict_order_independent(self):
        variables, global_attrs = _cmip_like()
        # Rebuild with reversed attrs dict insertion order (dicts compare
        # equal regardless of order, but iteration order could leak into
        # evidence strings/checks if the implementation is careless).
        import copy
        variables2 = copy.deepcopy(variables)
        for v in variables2:
            v["attrs"] = dict(reversed(list(v["attrs"].items())))
        global_attrs2 = dict(reversed(list(global_attrs.items())))

        out1 = check_cf(variables, global_attrs)
        out2 = check_cf(variables2, global_attrs2)
        self.assertEqual(out1["status"], out2["status"])
        self.assertEqual([c["id"] for c in out1["checks"]], [c["id"] for c in out2["checks"]])
        self.assertEqual([c["ok"] for c in out1["checks"]], [c["ok"] for c in out2["checks"]])


class CheckCFMissingUnitsTests(unittest.TestCase):
    def test_missing_units_on_data_var_warns(self):
        variables, global_attrs = _cmip_like()
        # Strip units from the one data variable.
        for v in variables:
            if v["name"] == "/tas":
                del v["attrs"]["units"]
        out = check_cf(variables, global_attrs)
        self.assertEqual(out["status"], "warn")
        checks = _checks_by_id(out)
        self.assertFalse(checks["units_and_name"]["ok"])
        self.assertIn("/tas", checks["units_and_name"]["evidence"])


class CheckCFDanglingGridMappingTests(unittest.TestCase):
    def test_grid_mapping_to_nonexistent_variable(self):
        variables, global_attrs = _cmip_like()
        for v in variables:
            if v["name"] == "/tas":
                v["attrs"]["grid_mapping"] = "does_not_exist"
        # Drop the (now-unreferenced) crs container too so there is no
        # other way for the grid-mapping-ish check to pass.
        variables = [v for v in variables if v["name"] != "/crs"]
        out = check_cf(variables, global_attrs)
        checks = _checks_by_id(out)
        self.assertFalse(checks["grid_mapping"]["ok"])
        self.assertIn("does_not_exist", checks["grid_mapping"]["evidence"])


class CheckCFNisarLikeTests(unittest.TestCase):
    def test_no_conventions_attr_but_projection_container_passes_grid_mapping(self):
        variables = [
            {"name": "/science/LSAR/GCOV/grids/frequencyA/HHHH",
             "dims": ["dim_0", "dim_1"], "shape": [100, 100], "dtype": "float32",
             "chunks": [10, 10], "attrs": {"units": "1", "long_name": "HH covariance"},
             "size_bytes": None, "codec": None},
            {"name": "/science/LSAR/GCOV/grids/frequencyA/projection",
             "dims": [], "shape": [], "dtype": "int32", "chunks": None,
             "attrs": {"spatial_ref": "GEOGCS[\"WGS 84\", ...WKT...]"},
             "size_bytes": None, "codec": None},
        ]
        global_attrs = {}  # no Conventions attribute at all
        out = check_cf(variables, global_attrs)
        checks = _checks_by_id(out)
        self.assertTrue(checks["grid_mapping"]["ok"])
        self.assertIn("projection", checks["grid_mapping"]["evidence"])
        self.assertEqual(out["status"], "warn")
        self.assertFalse(checks["conventions_attr"]["ok"])


class CheckCFEmptyAttrsTests(unittest.TestCase):
    def test_empty_attrs_everywhere_is_skipped(self):
        variables = [
            {"name": "/x", "dims": ["dim_0"], "shape": [10], "dtype": "float32",
             "chunks": None, "attrs": {}, "size_bytes": None, "codec": None},
            {"name": "/y", "dims": ["dim_0"], "shape": [10], "dtype": "float32",
             "chunks": None, "attrs": {}, "size_bytes": None, "codec": None},
        ]
        out = check_cf(variables, {})
        self.assertEqual(out["status"], "skipped")
        self.assertTrue(any("attributes unavailable" in n for n in out["notes"]))

    def test_no_variables_no_global_attrs_is_skipped(self):
        out = check_cf([], {})
        self.assertEqual(out["status"], "skipped")


class CheckCFShapeTests(unittest.TestCase):
    def test_every_check_has_required_keys(self):
        variables, global_attrs = _cmip_like()
        out = check_cf(variables, global_attrs)
        self.assertIn(out["status"], ("pass", "warn", "fail", "skipped"))
        self.assertIsInstance(out["checks"], list)
        self.assertIsInstance(out["notes"], list)
        for c in out["checks"]:
            self.assertIn("id", c)
            self.assertIn("ok", c)
            self.assertIn("evidence", c)
            self.assertIn(c["ok"], (True, False, None))


# ----------------------------------------- Finding 1: axis/_CoordinateAxisType
# and `coordinates`-attribute auxiliary-coordinate resolution.

class CheckCFAxisHintTests(unittest.TestCase):
    def test_axis_z_identifies_vertical_coordinate_regardless_of_name(self):
        # "elevation" matches no common coordinate name, but axis="Z"
        # is itself a valid CF coordinate-identification signal.
        variables = [
            {"name": "/elevation", "dims": ["elevation"], "shape": [3],
             "dtype": "float32", "chunks": None,
             "attrs": {"axis": "Z", "units": "m"},
             "size_bytes": None, "codec": None},
        ]
        out = check_cf(variables, {"Conventions": "CF-1.7"})
        checks = _checks_by_id(out)
        self.assertTrue(checks["coordinate_identification"]["ok"])
        self.assertIn("/elevation", checks["coordinate_identification"]["evidence"])

    def test_coordinate_axis_type_identifies_time_regardless_of_name(self):
        # Named "T" (not "time"), but _CoordinateAxisType="Time" is a
        # valid identification signal per conventions.md.
        variables = [
            {"name": "/T", "dims": ["T"], "shape": [5], "dtype": "float64",
             "chunks": None,
             "attrs": {"_CoordinateAxisType": "Time", "units": "hours since 2000-01-01"},
             "size_bytes": None, "codec": None},
        ]
        out = check_cf(variables, {"Conventions": "CF-1.7"})
        checks = _checks_by_id(out)
        self.assertTrue(checks["coordinate_identification"]["ok"])
        self.assertIn("time", checks["coordinate_identification"]["evidence"].lower())

    def test_axis_hint_present_but_time_units_still_missing_since_fails(self):
        variables = [
            {"name": "/T", "dims": ["T"], "shape": [5], "dtype": "float64",
             "chunks": None,
             "attrs": {"axis": "T", "units": "hours"},  # no "since ..."
             "size_bytes": None, "codec": None},
        ]
        out = check_cf(variables, {"Conventions": "CF-1.7"})
        checks = _checks_by_id(out)
        self.assertFalse(checks["coordinate_identification"]["ok"])
        self.assertIn("/T", checks["coordinate_identification"]["evidence"])


class CheckCFAuxiliaryCoordinatesTests(unittest.TestCase):
    def test_coordinates_attr_resolves_to_existing_variables(self):
        variables, global_attrs = _cmip_like()
        for v in variables:
            if v["name"] == "/tas":
                v["attrs"]["coordinates"] = "lat lon"
        out = check_cf(variables, global_attrs)
        checks = _checks_by_id(out)
        self.assertTrue(checks["coordinate_identification"]["ok"])
        self.assertIn("lat", checks["coordinate_identification"]["evidence"])

    def test_coordinates_attr_dangling_reference_fails(self):
        variables, global_attrs = _cmip_like()
        for v in variables:
            if v["name"] == "/tas":
                v["attrs"]["coordinates"] = "lat lon height_above_ground"
        out = check_cf(variables, global_attrs)
        checks = _checks_by_id(out)
        self.assertFalse(checks["coordinate_identification"]["ok"])
        self.assertIn("height_above_ground", checks["coordinate_identification"]["evidence"])


# --------------------------------------------- Finding 2: scale_factor/add_offset

class CheckCFScaleOffsetTests(unittest.TestCase):
    def test_absent_scale_offset_is_not_applicable(self):
        variables, global_attrs = _cmip_like()
        out = check_cf(variables, global_attrs)
        checks = _checks_by_id(out)
        self.assertIsNone(checks["scale_offset_typing"]["ok"])

    def test_numeric_scale_and_offset_pass(self):
        variables, global_attrs = _cmip_like()
        for v in variables:
            if v["name"] == "/tas":
                v["attrs"]["scale_factor"] = 0.01
                v["attrs"]["add_offset"] = 273.15
        out = check_cf(variables, global_attrs)
        checks = _checks_by_id(out)
        self.assertTrue(checks["scale_offset_typing"]["ok"])

    def test_stringly_typed_scale_factor_fails(self):
        variables, global_attrs = _cmip_like()
        for v in variables:
            if v["name"] == "/tas":
                v["attrs"]["scale_factor"] = "0.01"  # wrong type: should be numeric
        out = check_cf(variables, global_attrs)
        checks = _checks_by_id(out)
        self.assertFalse(checks["scale_offset_typing"]["ok"])
        self.assertIn("/tas", checks["scale_offset_typing"]["evidence"])
        self.assertIn("scale_factor", checks["scale_offset_typing"]["evidence"])


# --------------------------------------------- Finding 3: "fail" rollup state

class CheckCFFailRollupTests(unittest.TestCase):
    def test_no_cf_signal_at_all_is_fail(self):
        # Attrs are present (non-empty) so this isn't the "skipped"
        # case, but nothing in them is CF-shaped at all: no
        # Conventions, no units/standard_name/long_name, no
        # coordinate-like variables, no grid_mapping/CRS container, no
        # fill-value/scale-offset/bounds attrs.
        variables = [
            {"name": "/some_measurement", "dims": ["sample"], "shape": [10],
             "dtype": "float32", "chunks": None,
             "attrs": {"some_unrelated_attr": "1"},
             "size_bytes": None, "codec": None},
        ]
        out = check_cf(variables, {})
        self.assertEqual(out["status"], "fail")
        checks = _checks_by_id(out)
        self.assertFalse(checks["conventions_attr"]["ok"])
        self.assertFalse(checks["units_and_name"]["ok"])
        self.assertIn("no CF signal", "; ".join(out["notes"]))


# ------------------------------------------------------------- check_geozarr

def _zmetadata_bytes(metadata: dict) -> bytes:
    return json.dumps({"metadata": metadata, "zarr_consolidated_format": 1}).encode()


class CheckGeozarrTests(unittest.TestCase):
    def test_conformant_store_with_array_dimensions_and_crs_passes(self):
        metadata = {
            ".zattrs": {},
            ".zgroup": {"zarr_format": 2},
            "tas/.zattrs": {
                "_ARRAY_DIMENSIONS": ["time", "lat", "lon"],
                "units": "K",
                "standard_name": "air_temperature",
                "grid_mapping": "crs",
            },
            "tas/.zarray": {"shape": [10, 5, 5], "chunks": [10, 5, 5], "dtype": "<f4"},
            "crs/.zattrs": {"grid_mapping_name": "latitude_longitude"},
            "crs/.zarray": {"shape": [], "chunks": [], "dtype": "<i4"},
        }
        fs = FakeFS(files={"root/.zmetadata": _zmetadata_bytes(metadata),
                            "root/.zgroup": b"{}"})
        out = check_geozarr(fs, "root")
        self.assertIn(out["status"], ("pass", "warn"))
        checks = _checks_by_id(out)
        self.assertTrue(checks["dimension_names"]["ok"])
        self.assertTrue(checks["consolidated_metadata"]["ok"])
        # Never a hard fail.
        self.assertNotEqual(out["status"], "fail")

    def test_bare_store_warns_optional_spec_never_fails(self):
        fs = FakeFS(files={"root/.zgroup": b"{}"})
        out = check_geozarr(fs, "root")
        self.assertEqual(out["status"], "warn")
        self.assertTrue(any("optional spec" in n for n in out["notes"]))

    def test_empty_store_never_fails(self):
        fs = FakeFS(files={})
        out = check_geozarr(fs, "root")
        self.assertNotEqual(out["status"], "fail")

    def test_zarr_meta_override_skips_fs_reads(self):
        fs = FakeFS(files={})  # would otherwise look empty
        zarr_meta = {
            "consolidated": True,
            "root_attrs": {},
            "arrays": {
                "tas": {"attrs": {"_ARRAY_DIMENSIONS": ["time", "lat", "lon"],
                                   "units": "K", "standard_name": "air_temperature"}},
            },
        }
        out = check_geozarr(fs, "root", zarr_meta=zarr_meta)
        checks = _checks_by_id(out)
        self.assertTrue(checks["dimension_names"]["ok"])

    def test_shape_has_required_keys(self):
        fs = FakeFS(files={})
        out = check_geozarr(fs, "root")
        self.assertIn(out["status"], ("pass", "warn", "fail", "skipped"))
        self.assertIsInstance(out["checks"], list)
        self.assertIsInstance(out["notes"], list)
        for c in out["checks"]:
            self.assertIn("id", c)
            self.assertIn("ok", c)
            self.assertIn("evidence", c)


if __name__ == "__main__":
    unittest.main()
