"""Offline tests for check_cloud_ready.inventory.

Pure functions over inventory records (Ruling I-1 schema). No I/O, no
network — inventories are built in-test as plain dicts.
"""
import random
import unittest

from check_cloud_ready import inventory as inv

match_variables = inv.match_variables
rank_variables = inv.rank_variables
format_inventory_table = inv.format_inventory_table
VariableMatchError = inv.VariableMatchError


def var(name, dims=None, shape=None, dtype="float32", chunks=None,
        attrs=None, size_bytes=None, codec=None):
    return {
        "name": name,
        "dims": dims if dims is not None else [],
        "shape": shape if shape is not None else [],
        "dtype": dtype,
        "chunks": chunks,
        "attrs": attrs or {},
        "size_bytes": size_bytes,
        "codec": codec,
    }


# --------------------------------------------------------------- fixtures

NISAR_LIKE = [
    var("/science/LSAR/GCOV/grids/frequencyA/HHHH",
        dims=["yCoordinates", "xCoordinates"], shape=[8000, 6000], dtype="complex64"),
    var("/science/LSAR/GCOV/grids/frequencyA/HVHV",
        dims=["yCoordinates", "xCoordinates"], shape=[8000, 6000], dtype="complex64"),
    var("/science/LSAR/GCOV/grids/frequencyA/projection",
        dims=[], shape=[], dtype="int32"),
    var("/science/LSAR/GCOV/grids/frequencyA/numberOfLooks",
        dims=[], shape=[], dtype="int32"),
    var("/science/LSAR/GCOV/grids/frequencyA/mask",
        dims=["yCoordinates", "xCoordinates"], shape=[8000, 6000], dtype="int8"),
    var("/science/LSAR/GCOV/grids/frequencyA/qualityFlag",
        dims=["yCoordinates", "xCoordinates"], shape=[8000, 6000], dtype="int8"),
    var("/science/LSAR/GCOV/grids/frequencyA/xCoordinates",
        dims=["xCoordinates"], shape=[6000], dtype="float64"),
    var("/science/LSAR/GCOV/grids/frequencyA/yCoordinates",
        dims=["yCoordinates"], shape=[8000], dtype="float64"),
]

CMIP_LIKE = [
    var("tas", dims=["time", "lat", "lon"], shape=[12, 180, 360], dtype="float32"),
    var("tas_bnds", dims=["time", "bnds"], shape=[12, 2], dtype="float32"),
    var("time_bnds", dims=["time", "bnds"], shape=[12, 2], dtype="float64"),
    var("lat", dims=["lat"], shape=[180], dtype="float64"),
    var("lon", dims=["lon"], shape=[360], dtype="float64"),
    var("time", dims=["time"], shape=[12], dtype="float64"),
    var("height", dims=[], shape=[], dtype="float64"),
]


# --------------------------------------------------------------------- matcher

class TestMatchVariables(unittest.TestCase):
    def test_full_path_match(self):
        out = match_variables(NISAR_LIKE, ["/science/LSAR/GCOV/grids/frequencyA/HHHH"])
        self.assertEqual([v["name"] for v in out],
                          ["/science/LSAR/GCOV/grids/frequencyA/HHHH"])

    def test_no_leading_slash_variant(self):
        out = match_variables(NISAR_LIKE, ["science/LSAR/GCOV/grids/frequencyA/HHHH"])
        self.assertEqual([v["name"] for v in out],
                          ["/science/LSAR/GCOV/grids/frequencyA/HHHH"])

    def test_basename_match(self):
        out = match_variables(NISAR_LIKE, ["HHHH"])
        self.assertEqual([v["name"] for v in out],
                          ["/science/LSAR/GCOV/grids/frequencyA/HHHH"])

    def test_case_insensitive_substring_match(self):
        out = match_variables(NISAR_LIKE, ["hvhv"])
        self.assertEqual([v["name"] for v in out],
                          ["/science/LSAR/GCOV/grids/frequencyA/HVHV"])

    def test_bogus_token_raises_with_available_names(self):
        with self.assertRaises(VariableMatchError) as ctx:
            match_variables(NISAR_LIKE, ["nope"])
        msg = str(ctx.exception)
        self.assertIn("nope", msg)
        for v in NISAR_LIKE:
            self.assertIn(v["name"], msg)

    def test_multi_match_substring_returns_all(self):
        out = match_variables(NISAR_LIKE, ["Coordinates"])
        names = {v["name"] for v in out}
        self.assertEqual(names, {
            "/science/LSAR/GCOV/grids/frequencyA/xCoordinates",
            "/science/LSAR/GCOV/grids/frequencyA/yCoordinates",
        })

    def test_result_order_is_inventory_order_and_deduped(self):
        # "HVHV" and "frequencyA" both match overlapping sets; requesting
        # both should not duplicate entries and should preserve inventory
        # order.
        out = match_variables(NISAR_LIKE, ["HVHV", "frequencyA"])
        names = [v["name"] for v in out]
        self.assertEqual(names, [v["name"] for v in NISAR_LIKE])  # all match "frequencyA"
        self.assertEqual(len(names), len(set(names)))

    def test_empty_inventory_raises_no_variables_available(self):
        with self.assertRaises(VariableMatchError) as ctx:
            match_variables([], ["anything"])
        self.assertIn("no variables available", str(ctx.exception))


# --------------------------------------------------------------------- ranking

class TestRankVariablesNisar(unittest.TestCase):
    def test_data_vars_outrank_excluded_and_aux(self):
        ranked = rank_variables(NISAR_LIKE)
        names = [v["name"] for v in ranked]
        self.assertEqual(names[:2], [
            "/science/LSAR/GCOV/grids/frequencyA/HHHH",
            "/science/LSAR/GCOV/grids/frequencyA/HVHV",
        ])

    def test_excluded_names_absent(self):
        ranked = rank_variables(NISAR_LIKE)
        names = {v["name"] for v in ranked}
        for excluded in ("projection", "numberOfLooks", "mask", "qualityFlag",
                         "xCoordinates", "yCoordinates"):
            self.assertFalse(any(excluded in n for n in names),
                              msg=f"{excluded!r} should not appear in ranked output")


class TestRankVariablesCmip(unittest.TestCase):
    def test_tas_is_first_and_only_survivor(self):
        ranked = rank_variables(CMIP_LIKE)
        names = [v["name"] for v in ranked]
        self.assertEqual(names, ["tas"])


class TestRankDeterminism(unittest.TestCase):
    def test_shuffle_input_same_output(self):
        base = list(NISAR_LIKE) + list(CMIP_LIKE)
        expected = rank_variables(base)
        rng = random.Random(42)
        for _ in range(5):
            shuffled = list(base)
            rng.shuffle(shuffled)
            self.assertEqual(rank_variables(shuffled), expected)

    def test_name_tiebreaker(self):
        # Same ndim, same element count -> lexicographic by name.
        a = var("bravo", dims=["x"], shape=[10])
        b = var("alpha", dims=["x"], shape=[10])
        ranked = rank_variables([a, b])
        self.assertEqual([v["name"] for v in ranked], ["alpha", "bravo"])


class TestRankExclusionRules(unittest.TestCase):
    def test_string_and_bool_dtypes_excluded(self):
        s = var("label", dims=["x"], shape=[10], dtype="|S10")
        u = var("name_str", dims=["x"], shape=[10], dtype="<U10")
        b = var("valid", dims=["x"], shape=[10], dtype="bool")
        keep = var("data", dims=["x"], shape=[10], dtype="float32")
        ranked = rank_variables([s, u, b, keep])
        self.assertEqual([v["name"] for v in ranked], ["data"])

    def test_zero_d_scalar_excluded(self):
        scalar = var("scalar_thing", dims=[], shape=[], dtype="float32")
        keep = var("data", dims=["x"], shape=[10], dtype="float32")
        ranked = rank_variables([scalar, keep])
        self.assertEqual([v["name"] for v in ranked], ["data"])

    def test_coordinate_variable_equal_to_own_dim_excluded(self):
        # name equals one of its own dims
        coord = var("depth_level", dims=["depth_level"], shape=[5], dtype="float32")
        keep = var("data", dims=["x"], shape=[10], dtype="float32")
        ranked = rank_variables([coord, keep])
        self.assertEqual([v["name"] for v in ranked], ["data"])

    def test_grid_mapping_crs_names_excluded(self):
        for name in ("crs", "spatial_ref", "grid_projection"):
            v = var(name, dims=[], shape=[], dtype="int32")
            self.assertEqual(rank_variables([v]), [])

    def test_ranking_secondary_key_is_element_count(self):
        small = var("small", dims=["x", "y"], shape=[2, 2], dtype="float32")
        big = var("big", dims=["x", "y"], shape=[100, 100], dtype="float32")
        ranked = rank_variables([small, big])
        self.assertEqual([v["name"] for v in ranked], ["big", "small"])

    def test_ranking_primary_key_is_ndim(self):
        one_d = var("one_d", dims=["x"], shape=[10000], dtype="float32")
        two_d = var("two_d", dims=["x", "y"], shape=[10, 10], dtype="float32")
        ranked = rank_variables([one_d, two_d])
        self.assertEqual([v["name"] for v in ranked], ["two_d", "one_d"])


class TestEmptyInventory(unittest.TestCase):
    def test_empty_inventory_ranking_is_empty(self):
        self.assertEqual(rank_variables([]), [])


class TestRankExclusionFullPathScope(unittest.TestCase):
    """QA/flag/quality/mask, bounds, and CRS/projection patterns must be
    matched against the FULL path, not just the leaf/basename — a
    keyword living in a parent group segment is just as disqualifying
    as one in the leaf name itself. Only the coordinate-variable rule
    (name-equals-own-dim / common-coord-name) stays scoped to the
    basename.
    """

    def test_qa_keyword_in_parent_path_segment_excludes_leaf(self):
        # "quality" appears only in a parent group segment; the leaf
        # name "pixelValidityMap" itself contains none of the QA/flag/
        # bounds/CRS keywords. Same shape as a legitimate science var,
        # so if this survived exclusion it would out-rank or tie with
        # real science data.
        qa_leaf = var("/science/LSAR/GCOV/quality/pixelValidityMap",
                       dims=["y", "x"], shape=[8000, 6000], dtype="float32")
        science = var("/science/LSAR/GCOV/grids/frequencyA/HHHH",
                       dims=["y", "x"], shape=[8000, 6000], dtype="complex64")
        ranked = rank_variables([qa_leaf, science])
        self.assertEqual([v["name"] for v in ranked], [science["name"]])

    def test_bounds_keyword_in_parent_path_segment_excludes_leaf(self):
        bounds_leaf = var("/science/LSAR/GCOV/bounds/edgeMap",
                           dims=["y", "x"], shape=[8000, 6000], dtype="float32")
        science = var("/science/LSAR/GCOV/grids/frequencyA/HHHH",
                       dims=["y", "x"], shape=[8000, 6000], dtype="complex64")
        ranked = rank_variables([bounds_leaf, science])
        self.assertEqual([v["name"] for v in ranked], [science["name"]])

    def test_crs_keyword_in_parent_path_segment_excludes_leaf(self):
        crs_leaf = var("/science/LSAR/GCOV/projection/gridValues",
                        dims=["y", "x"], shape=[8000, 6000], dtype="float32")
        science = var("/science/LSAR/GCOV/grids/frequencyA/HHHH",
                       dims=["y", "x"], shape=[8000, 6000], dtype="complex64")
        ranked = rank_variables([crs_leaf, science])
        self.assertEqual([v["name"] for v in ranked], [science["name"]])

    def test_science_var_with_no_keyword_anywhere_in_path_is_ranked(self):
        science = var("/science/LSAR/GCOV/grids/frequencyA/HHHH",
                       dims=["y", "x"], shape=[8000, 6000], dtype="complex64")
        ranked = rank_variables([science])
        self.assertEqual([v["name"] for v in ranked], [science["name"]])

    def test_boundary_layer_height_not_excluded_by_bounds_rule(self):
        # Plain substring matching, not word-boundary matching:
        # "bounds" (b-o-u-n-d-s) is not a substring of "boundary"
        # (b-o-u-n-d-a-r-y — "bound" is followed by "a", never "s"),
        # so this word-ish name is untouched by the bounds exclusion
        # even though it starts with "bound".
        v = var("boundaryLayerHeight", dims=["x"], shape=[10], dtype="float32")
        ranked = rank_variables([v])
        self.assertEqual([r["name"] for r in ranked], ["boundaryLayerHeight"])


class TestMatchVariablesEdgeCases(unittest.TestCase):
    def test_empty_requested_list_returns_empty(self):
        self.assertEqual(match_variables(NISAR_LIKE, []), [])

    def test_duplicate_name_inventory_is_not_double_counted(self):
        dup = [
            var("/a/HHHH", dims=["y", "x"], shape=[10, 10]),
            var("/a/HHHH", dims=["y", "x"], shape=[10, 10]),
            var("/a/HVHV", dims=["y", "x"], shape=[10, 10]),
        ]
        out = match_variables(dup, ["HHHH"])
        # Both inventory entries named "/a/HHHH" match the token; the
        # matcher dedupes by name, but since both records share that
        # name, filtering `inventory` by matched-name naturally
        # reproduces every record with that name (no swallowing of a
        # legitimately repeated record).
        self.assertEqual([v["name"] for v in out], ["/a/HHHH", "/a/HHHH"])


# --------------------------------------------------------------- table format

class TestFormatInventoryTable(unittest.TestCase):
    def test_contains_index_name_dims_shape_dtype(self):
        table = format_inventory_table(NISAR_LIKE[:2])
        self.assertIn("/science/LSAR/GCOV/grids/frequencyA/HHHH", table)
        self.assertIn("complex64", table)
        self.assertIn("8000", table)
        self.assertIn("6000", table)
        lines = table.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].strip().startswith("0"))
        self.assertTrue(lines[1].strip().startswith("1"))

    def test_empty_inventory_gives_empty_table(self):
        self.assertEqual(format_inventory_table([]), "")


class TestParseSelection(unittest.TestCase):
    def test_all_returns_every_name(self):
        self.assertEqual(inv.parse_selection(NISAR_LIKE, "all"),
                          [v["name"] for v in NISAR_LIKE])
        self.assertEqual(inv.parse_selection(NISAR_LIKE, "ALL"),
                          [v["name"] for v in NISAR_LIKE])

    def test_numeric_index_resolves_to_name(self):
        self.assertEqual(inv.parse_selection(NISAR_LIKE, "0"), [NISAR_LIKE[0]["name"]])

    def test_comma_separated_mix_of_index_and_name(self):
        tokens = inv.parse_selection(NISAR_LIKE, f"0, {NISAR_LIKE[1]['name']}")
        self.assertEqual(tokens, [NISAR_LIKE[0]["name"], NISAR_LIKE[1]["name"]])

    def test_out_of_range_index_passed_through_as_literal_token(self):
        # Not a valid index into the inventory -> treated as a literal
        # name/substring token for match_variables to resolve (and
        # potentially raise VariableMatchError on).
        tokens = inv.parse_selection(NISAR_LIKE, "999")
        self.assertEqual(tokens, ["999"])

    def test_blank_parts_ignored(self):
        self.assertEqual(inv.parse_selection(NISAR_LIKE, "0,,"), [NISAR_LIKE[0]["name"]])


if __name__ == "__main__":
    unittest.main()
