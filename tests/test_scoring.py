"""Offline tests for check_cloud_ready.scoring.

Pure-function tests against synthetic asset dicts shaped like the real
outputs of chunking.assess_chunking / compression.assess_compression /
conventions.check_cf / openers.open_dataset / access.workflow._finding
(see each module's docstring for the exact shapes reproduced here).
No I/O, no network.
"""
import copy
import unittest

from check_cloud_ready import chunking, scoring

score = scoring.score
rollup_checks = scoring.rollup_checks
tier_for = scoring.tier_for
verdict_for = scoring.verdict_for


# --------------------------------------------------------------- fixtures

def _cf_pass():
    return {"status": "pass",
            "checks": [{"id": "conventions_attr", "ok": True, "evidence": "CF-1.10"},
                       {"id": "units_and_name", "ok": True, "evidence": "units present"},
                       {"id": "grid_mapping", "ok": True, "evidence": "crs resolved"}],
            "notes": []}


def _cf_none():
    return {"status": "skipped", "checks": [], "notes": ["attributes unavailable"]}


def _variable(name="/temp", compressed_mb=8.0, shape=(1000, 1000), chunks=(200, 200)):
    """Fixture-build one chunking-output-shaped variable dict the way
    ``chunking.assess_chunking`` actually would for a given measured/
    estimated compressed chunk size: both ``grade`` (the single scored
    criterion C1/C2 now read -- fix round, final-review finding 1) and
    ``profiles`` (the informational three-profile overlay, still used by
    C3-agentic) are derived from the real ``chunking.grade()``/
    ``chunking._build_profiles()`` functions, never a hand-picked status
    string -- the pre-fix bug was exactly a fixture that set all three
    profile statuses to the same value, a state real chunking output can
    never produce for one chunk size (interactive/training bands are
    disjoint), which masked C1/C2 never being independently satisfiable.
    """
    grade_band, grade_note = chunking.grade(compressed_mb)
    return {"name": name, "shape": list(shape), "chunks": list(chunks),
            "grade": grade_band, "grade_note": grade_note,
            "profiles": chunking._build_profiles(compressed_mb)}


def _inspection_pass(name="/temp", codec="zstd"):
    return {"name": name, "codec": codec, "status": "pass",
            "note": f"{codec}: modern read-favoring codec", "remediation": None}


def _access_finding(id_, dim, status, evidence="", remediation=None):
    return {"id": id_, "dim": dim, "status": status, "evidence": evidence,
            "remediation": remediation, "pts": 0.0, "awarded": 0.0}


def _golden_zarr_asset():
    """A cloud-native (Icechunk-versioned Zarr) asset that should pass
    essentially everything -- Icechunk is used rather than plain Zarr so
    E1-version/E2-checksums (which rubric.md ties to snapshot/strong-etag
    versioning) can reach a full pass rather than being permanently
    capped at "partial" the way a non-Icechunk Zarr legitimately is."""
    return {
        "format": "icechunk",
        "smoke_status": "pass",
        "open": {
            "telemetry": {"requests_to_open": 2, "bytes_to_open": 4096},
            "inventory": [{"name": "/temp", "attrs": {"units": "K", "grid_mapping": "crs"}},
                          {"name": "/crs", "attrs": {}}],
            "format_checks": {"consolidated": True, "zarr_version": 3,
                              "crs_containers": [{"name": "/crs"}]},
            "metadata_walk": None,
        },
        "inventory": [{"name": "/temp", "attrs": {"units": "K"}}],
        "chunking": {"variables": [_variable("/temp", 8.0, (1000, 1000), (400, 400))]},
        "compression": {"inspection": [_inspection_pass("/temp", "zstd")]},
        "conventions": {"cf": _cf_pass()},
        "access_findings": [
            _access_finding("D1-range", "D", "pass", "206 with correct bytes"),
            _access_finding("D2-auth", "D", "pass", "anonymous access ok"),
            _access_finding("D3-https-cors", "D", "pass", "tls ok, CORS present"),
            _access_finding("D4-cleanpath", "D", "pass", "no redirects"),
        ],
        "etag": '"abc123"',
        "cmr_meta": {"license": "CC0", "doi": "10.5067/EXAMPLE", "contact": "daac@example.gov",
                     "doc_links": [{"href": "https://example.org/tutorial"}],
                     "n_keywords": 5, "description_len": 300},
        "profile": None,
    }


class GoldenHappyPathTests(unittest.TestCase):
    """Scenario 1: cloud-native zarr, all pass -> READY, tier A-ish,
    confidence High, no caps."""

    def test_ready_high_confidence_no_caps(self):
        result = score(_golden_zarr_asset())
        self.assertEqual(result["verdict"], "READY")
        self.assertEqual(result["confidence"], "High")
        self.assertEqual(result["caps_applied"], [])
        self.assertGreaterEqual(result["score"], 90.0)
        self.assertEqual(result["tier"], "A")
        for c in result["checks"]:
            self.assertIn(c["status"], ("pass",), msg=c)


class GoodChunkSizeReachesReadyTests(unittest.TestCase):
    """Regression guard for final-review finding 1: at the tool's own
    advertised ~8 MB interactive/agentic sweet spot, real
    ``chunking.grade()``/``chunking._build_profiles()`` output (not a
    hand-picked/impossible profile-status fixture) fed through
    ``scoring.score()`` on an otherwise-clean asset must reach READY at
    a tier better than C.

    Before the fix, C1-interactive/C2-training scored against the
    three-profile overlay's disjoint interactive (1-4 MB) / training
    (10-100 MB) ``"within"`` bands -- at 8 MB, real
    ``chunking._build_profiles(8.0)`` yields interactive="above" and
    training="below", so NEITHER profile check could ever read "within"
    for this (or any single) chunk size. Both C1 and C2 scored "fail",
    both are consumer-visible-fail ids, and the score/verdict were
    force-capped to NOT READY / tier<=C regardless of how well-sized the
    chunk actually was.
    """

    def test_8mb_sweet_spot_chunk_reaches_ready_above_tier_c(self):
        grade_band, grade_note = chunking.grade(8.0)
        self.assertEqual(grade_band, "pass")  # sanity: 8 MB is in the 4-16 MB pass band

        asset = _golden_zarr_asset()
        asset["chunking"] = {"variables": [{
            "name": "/temp", "shape": [1000, 1000], "chunks": [400, 400],
            "grade": grade_band, "grade_note": grade_note,
            "profiles": chunking._build_profiles(8.0),
        }]}

        result = score(asset)

        self.assertEqual(result["verdict"], "READY", msg=result["verdict_reason"])
        self.assertNotIn(result["tier"], ("C", "D", "F"))
        c1 = next(c for c in result["checks"] if c["id"] == "C1-interactive")
        c2 = next(c for c in result["checks"] if c["id"] == "C2-training")
        self.assertEqual(c1["status"], "pass")
        self.assertEqual(c2["status"], "pass")
        self.assertEqual(result["caps_applied"], [])


class ChunkSizeFailTests(unittest.TestCase):
    """Scenario 2: chunk-size fail -> NOT READY, verdict_reason names
    chunking, cap applied, tier <= C even if raw sum were higher."""

    def test_chunk_fail_forces_not_ready_and_caps(self):
        asset = _golden_zarr_asset()
        # Every dimension besides C stays at (near-)full marks; C1/C2 fail
        # outright: 0.5 MB compressed is squarely in chunking.grade()'s
        # <1 MB "fail" band (too small, per-request overhead dominates).
        asset["chunking"] = {"variables": [_variable("/temp", 0.5, (1000, 1000),
                                                       (1000, 1000))]}
        result = score(asset)
        self.assertEqual(result["verdict"], "NOT READY")
        self.assertIn("C1-interactive", result["verdict_reason"] + str(result["checks"]))
        c1 = next(c for c in result["checks"] if c["id"] == "C1-interactive")
        c2 = next(c for c in result["checks"] if c["id"] == "C2-training")
        self.assertEqual(c1["status"], "fail")
        self.assertEqual(c2["status"], "fail")
        self.assertIn("consumer_visible_fail_cap_74", result["caps_applied"])
        self.assertLessEqual(result["score"], 74.0)
        self.assertIn(result["tier"], ("C", "D", "F"))

    def test_verdict_reason_names_a_consumer_visible_check(self):
        asset = _golden_zarr_asset()
        asset["chunking"] = {"variables": [_variable("/temp", 0.5)]}
        result = score(asset)
        self.assertIn("C1-interactive", result["verdict_reason"])


class SmokeFailTests(unittest.TestCase):
    """Scenario 3: smoke fail -> score capped 74, NOT READY."""

    def test_smoke_fail_caps_score_and_blocks_ready(self):
        asset = _golden_zarr_asset()
        asset["smoke_status"] = "fail"
        result = score(asset)
        self.assertEqual(result["verdict"], "NOT READY")
        self.assertIn("smoke_fail_cap_74", result["caps_applied"])
        self.assertLessEqual(result["score"], 74.0)


class WarnsOnlyTests(unittest.TestCase):
    """Scenario 4: warns only -> READY WITH CAVEATS, no cap."""

    def test_warns_only_gives_caveats_without_cap(self):
        asset = _golden_zarr_asset()
        # Downgrade one D-check to a "warn" (-> partial), nothing to "fail".
        asset["access_findings"] = [
            _access_finding("D1-range", "D", "pass"),
            _access_finding("D2-auth", "D", "pass"),
            _access_finding("D3-https-cors", "D", "warn", "CORS header missing"),
            _access_finding("D4-cleanpath", "D", "pass"),
        ]
        result = score(asset)
        self.assertEqual(result["verdict"], "READY WITH CAVEATS")
        self.assertEqual(result["caps_applied"], [])
        statuses = {c["status"] for c in result["checks"]}
        self.assertNotIn("fail", statuses)


class AuthSkippedAccessFindingTests(unittest.TestCase):
    """Scenario 5: auth-skipped access findings -> those checks skipped,
    score NOT zeroed, confidence downgraded, verdict from remaining
    checks."""

    def test_auth_skip_does_not_zero_score(self):
        asset = _golden_zarr_asset()
        asset["access_findings"] = [
            _access_finding("D1-range", "D", "skipped",
                            "https endpoint requires authentication before range "
                            "support could be verified"),
            _access_finding("D2-auth", "D", "skipped",
                            "Earthdata Login credentials missing"),
        ]
        result = score(asset)
        d1 = next(c for c in result["checks"] if c["id"] == "D1-range")
        d2 = next(c for c in result["checks"] if c["id"] == "D2-auth")
        self.assertEqual(d1["status"], "skipped")
        self.assertEqual(d2["status"], "skipped")
        self.assertGreater(d1["points_awarded"], 0.0)
        self.assertGreater(d2["points_awarded"], 0.0)
        self.assertIn(result["confidence"], ("Reduced", "Low"))
        # No fail anywhere -> not blocked from at least caveats-or-better.
        self.assertIn(result["verdict"], ("READY", "READY WITH CAVEATS"))


class NARenormalizationTests(unittest.TestCase):
    """Scenario 6: dimension with a check excluded as n/a still scores
    over the 0-weight range correctly (concrete number asserted)."""

    def test_b4_na_renormalizes_b_dimension(self):
        asset = _golden_zarr_asset()
        # Strip every file-attrs signal B4 looks at -> B4 becomes n/a.
        asset["format"] = None
        asset["conventions"] = {"cf": _cf_none()}
        asset["inventory"] = []
        asset["open"]["inventory"] = []
        result = score(asset)
        b4 = next(c for c in result["checks"] if c["id"] == "B4-catalog")
        self.assertEqual(b4["status"], "n/a")

        b_checks = [c for c in result["checks"] if c["dimension"] == "B"
                    and c["status"] != "n/a"]
        poss = sum(c["points_possible"] for c in b_checks)
        got = sum(c["points_awarded"] for c in b_checks)
        expected = round(got / poss * 20, 1)
        self.assertEqual(result["dimensions"]["B"]["score"], expected)
        self.assertEqual(result["dimensions"]["B"]["max"], 20)


class TierBoundaryTests(unittest.TestCase):
    """Scenario 7: tier boundaries exactly at 90/89.9, 75/74.9, 55/54.9,
    35/34.9."""

    def test_boundaries(self):
        self.assertEqual(tier_for(100.0), "A")
        self.assertEqual(tier_for(90.0), "A")
        self.assertEqual(tier_for(89.9), "B")
        self.assertEqual(tier_for(75.0), "B")
        self.assertEqual(tier_for(74.9), "C")
        self.assertEqual(tier_for(55.0), "C")
        self.assertEqual(tier_for(54.9), "D")
        self.assertEqual(tier_for(35.0), "D")
        self.assertEqual(tier_for(34.9), "F")
        self.assertEqual(tier_for(0.0), "F")


class FailRemediationTests(unittest.TestCase):
    """Scenario 8: every fail/partial check carries a remediation; a
    fail/partial constructed without one gets filled in from the
    fallback/migration table (rubric.md section 8: "every fail/partial
    check must carry at least one remediation")."""

    def test_fail_without_remediation_gets_filled(self):
        asset = _golden_zarr_asset()
        asset["access_findings"] = [
            _access_finding("D4-cleanpath", "D", "fail",
                            "redirect chain detected", remediation=None),
        ]
        result = score(asset)
        d4 = next(c for c in result["checks"] if c["id"] == "D4-cleanpath")
        self.assertEqual(d4["status"], "fail")
        self.assertTrue(d4["remediation"])

    def test_every_fail_has_remediation(self):
        asset = _golden_zarr_asset()
        asset["format"] = "hdf4"
        asset["chunking"] = {"variables": [_variable("/temp", 0.5)]}
        asset["compression"] = {"inspection": [
            {"name": "/temp", "codec": None, "status": "fail",
             "note": "no compression", "remediation": None},
        ]}
        result = score(asset)
        fails = [c for c in result["checks"] if c["status"] == "fail"]
        self.assertTrue(fails)
        for c in fails:
            self.assertTrue(c["remediation"], msg=c)

    def test_partial_warn_from_access_findings_without_remediation_gets_filled(self):
        """Fix round (review finding 1): reproduces the real
        access/workflow.py D2-auth path where ``anon=True`` forces
        anonymous S3 access despite a protected-bucket probe result
        (workflow.py ~l.300-305) -- that finding ships with
        ``status="warn"`` (-> this module's ``"partial"``) and
        ``remediation=None``. The backstop must fill it in, not just for
        outright fails.
        """
        asset = _golden_zarr_asset()
        asset["access_findings"] = [
            _access_finding("D2-auth", "D", "warn",
                            "anonymous access forced despite a protected-bucket "
                            "probe result", remediation=None),
        ]
        result = score(asset)
        d2 = next(c for c in result["checks"] if c["id"] == "D2-auth")
        self.assertEqual(d2["status"], "partial")
        self.assertTrue(d2["remediation"])

    def test_every_fail_or_partial_has_remediation(self):
        """Broader property check across several scenarios already used
        elsewhere in this file: no fail or partial check should ever
        ship with an empty remediation."""
        assets = [_golden_zarr_asset()]

        a = _golden_zarr_asset()
        a["access_findings"] = [
            _access_finding("D3-https-cors", "D", "warn", "CORS header missing",
                            remediation=None),
        ]
        assets.append(a)

        b = _golden_zarr_asset()
        b["format"] = "hdf4"
        b["chunking"] = {"variables": [_variable("/temp", 0.5)]}
        assets.append(b)

        for asset in assets:
            result = score(asset)
            for c in result["checks"]:
                if c["status"] in ("fail", "partial"):
                    self.assertTrue(c["remediation"], msg=c)


class CloudHostileFormatTests(unittest.TestCase):
    """Scenario 9: cloud-hostile format (hdf4) -> A-class baseline low,
    mandatory migration remediation present."""

    def test_hdf4_baseline_low_with_migration(self):
        asset = _golden_zarr_asset()
        asset["format"] = "hdf4"
        asset["open"]["format_checks"] = {}
        asset["open"]["inventory"] = []
        result = score(asset)
        a = next(c for c in result["checks"] if c["id"] == "A-class")
        self.assertLessEqual(a["points_awarded"], 10.0)
        self.assertIn("h4toh5convert", a["remediation"])


class CmrMetaTests(unittest.TestCase):
    """Scenario 10: cmr_meta with license/DOI -> E3/E4 pass; cmr_meta
    None -> E3/E4 skipped (not fail), confidence effect visible."""

    def test_cmr_meta_present_passes_e3_e4(self):
        asset = _golden_zarr_asset()
        result = score(asset)
        e3 = next(c for c in result["checks"] if c["id"] == "E3-license")
        e4 = next(c for c in result["checks"] if c["id"] == "E4-citation")
        self.assertEqual(e3["status"], "pass")
        self.assertEqual(e4["status"], "pass")

    def test_cmr_meta_none_skips_not_fails(self):
        asset = _golden_zarr_asset()
        asset["cmr_meta"] = None
        result = score(asset)
        e3 = next(c for c in result["checks"] if c["id"] == "E3-license")
        e4 = next(c for c in result["checks"] if c["id"] == "E4-citation")
        self.assertEqual(e3["status"], "skipped")
        self.assertEqual(e4["status"], "skipped")
        self.assertNotEqual(e3["status"], "fail")
        self.assertNotEqual(e4["status"], "fail")
        # 5 catalog checks (E3-E7) skipped -> confidence visibly downgraded
        # from the golden (High) case.
        self.assertEqual(result["confidence"], "Low")


class DeterminismTests(unittest.TestCase):
    """Scenario 11: same asset dict scored twice -> identical output."""

    def test_same_input_same_output(self):
        asset = _golden_zarr_asset()
        asset_copy = copy.deepcopy(asset)
        result1 = score(asset)
        result2 = score(asset_copy)
        self.assertEqual(result1, result2)
        # scoring must not mutate its input
        self.assertEqual(asset, asset_copy)

    def test_repeated_calls_on_same_object_are_stable(self):
        asset = _golden_zarr_asset()
        result1 = score(asset)
        result2 = score(asset)
        self.assertEqual(result1, result2)


class RollupChecksTests(unittest.TestCase):
    def test_rollup_returns_dimensions_and_score(self):
        checks = [
            scoring.C("A-class", "A", 30, 30, "pass", "x"),
            scoring.C("B1-open", "B", 8, 8, "pass", "x"),
        ]
        dims, total = rollup_checks(checks)
        self.assertEqual(dims["A"]["score"], 30.0)
        self.assertEqual(dims["B"]["score"], 20.0)
        self.assertEqual(dims["C"]["score"], 0.0)
        self.assertEqual(total, 50.0)


class E1E2EtagTests(unittest.TestCase):
    """Final-review finding 3: _e1_e2_checks itself already handled an
    ``etag`` argument correctly -- the bug was that nothing upstream
    (cli.py) ever populated ``asset["etag"]`` in the first place, so
    these checks were permanently fail/partial regardless of what a
    live probe served. Direct unit coverage of the check function's
    etag-driven behavior, independent of the cli-level wiring test."""

    def test_no_etag_is_fail(self):
        e1, e2 = scoring._e1_e2_checks("zarr", None)
        self.assertEqual(e1["status"], "fail")
        self.assertEqual(e2["status"], "fail")

    def test_strong_etag_is_non_fail_for_both(self):
        e1, e2 = scoring._e1_e2_checks("zarr", '"abc123"')
        self.assertNotEqual(e1["status"], "fail")
        self.assertNotEqual(e2["status"], "fail")

    def test_weak_etag_unblocks_e1_but_not_e2(self):
        e1, e2 = scoring._e1_e2_checks("zarr", 'W/"abc123"')
        self.assertNotEqual(e1["status"], "fail")
        self.assertEqual(e2["status"], "fail")


class VerdictForTests(unittest.TestCase):
    def test_smoke_fail_always_not_ready(self):
        verdict, reason = verdict_for([], "fail")
        self.assertEqual(verdict, "NOT READY")
        self.assertIn("smoke", reason)

    def test_no_checks_ready(self):
        verdict, reason = verdict_for([], "pass")
        self.assertEqual(verdict, "READY")


if __name__ == "__main__":
    unittest.main()
