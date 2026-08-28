"""End-to-end tests for check_cloud_ready.cli.main: local zarr/HDF5
fixtures, no network, no mocks for the assessment pipeline itself
(only stdin/input() is mocked, for the interactive-path test)."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from check_cloud_ready import cli

try:
    import zarr
except ImportError:
    zarr = None

try:
    import h5py
except ImportError:
    h5py = None


def _make_zarr_store(root: str) -> str:
    """Small local zarr v3 store: one time-spanning chunked data
    variable + a time coordinate, consolidated metadata."""
    path = os.path.join(root, "store.zarr")
    g = zarr.open_group(path, mode="w", zarr_format=3)
    rng = np.random.default_rng(0)
    data = rng.random((24, 10, 10)).astype("f4")
    arr = g.create_array("temperature", shape=data.shape, chunks=(24, 5, 5), dtype="f4")
    arr[:] = data
    arr.attrs["units"] = "K"
    arr.attrs["standard_name"] = "air_temperature"
    arr.attrs["_ARRAY_DIMENSIONS"] = ["time", "y", "x"]
    t = g.create_array("time", shape=(24,), chunks=(24,), dtype="f8")
    t[:] = np.arange(24)
    t.attrs["units"] = "hours since 2020-01-01"
    t.attrs["standard_name"] = "time"
    zarr.consolidate_metadata(g.store)
    return path


def _make_hdf5_file(root: str) -> str:
    path = os.path.join(root, "data.h5")
    with h5py.File(path, "w") as f:
        rng = np.random.default_rng(1)
        data = rng.random((24, 10, 10)).astype("f4")
        ds = f.create_dataset("temperature", data=data, chunks=(24, 5, 5),
                              compression="gzip")
        ds.attrs["units"] = "K"
        ds.attrs["standard_name"] = "air_temperature"
        t = f.create_dataset("time", data=np.arange(24, dtype="f8"))
        t.attrs["units"] = "hours since 2020-01-01"
        t.attrs["standard_name"] = "time"
    return path


class _TmpDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()


@unittest.skipUnless(zarr, "zarr not installed")
class ZarrE2ETests(_TmpDirCase):
    def test_non_interactive_zarr_run_succeeds(self):
        store = _make_zarr_store(self.tmp)
        out_dir = os.path.join(self.tmp, "out")

        rc = cli.main([store, "--non-interactive", "--report-style", "both",
                       "--out", out_dir])

        self.assertEqual(rc, 0)
        findings_path = Path(out_dir) / "findings.json"
        self.assertTrue(findings_path.exists())
        findings = json.loads(findings_path.read_text())
        self.assertIn("verdict", findings)
        self.assertIn("score", findings)
        self.assertIn("tier", findings)
        # Bridge 2 regression guard: these must be the real computed
        # values (scoring.score()'s per-asset output lifted to the top
        # level of findings), never a silent None/placeholder fallback.
        self.assertIn(findings["verdict"], ("READY", "READY WITH CAVEATS", "NOT READY"))
        self.assertIsInstance(findings["score"], (int, float))
        self.assertIn(findings["tier"], ("A", "B", "C", "D", "F"))

        report_path = Path(out_dir) / "assessment-report.md"
        self.assertTrue(report_path.exists())
        report = report_path.read_text()
        self.assertTrue(report.startswith("**Verdict:"))
        # Bridge 2 regression guard, at the rendered-report level: the
        # verdict line must show the real verdict, never the "—"
        # placeholder rendering.py falls back to when findings["verdict"]
        # is missing.
        self.assertIn(f"**Verdict: {findings['verdict']}", report)
        self.assertNotIn("Verdict: \u2014", report)
        self.assertIn("Chunk layout", report)  # orientation section present

        # Degradation-adjacent: a local input's hosting/access checks are
        # n/a (D0-hosting, skipped) but the report still renders fully.
        asset = findings["assets"][0]
        d0 = [f for f in asset["access_findings"] if f["id"] == "D0-hosting"]
        self.assertEqual(len(d0), 1)
        self.assertEqual(d0[0]["status"], "skipped")
        self.assertIsNotNone(asset["chunking"])
        self.assertIsNotNone(asset["score"])

        # Bridge 1: metadata_walk must be nested inside telemetry (not a
        # silent "not walked" placeholder) for a format that measures it.
        self.assertIn("metadata_walk", asset["telemetry"])

        # The live opener handle (zarr.Group/h5py.File/...) must never
        # leak into the serialized findings.
        self.assertNotIn("handle", asset["open"])

    def test_time_cap_breach_surfaces_as_a_finding_not_silently_absorbed(self):
        """S8: Budget.check_stage always records a breach finding in
        Budget.breaches; the CLI must surface it (in findings.json AND
        the rendered report) rather than silently absorbing it just
        because the run didn't crash. --time-cap 0 guarantees the
        "open" stage's check_stage(..., raise_on_breach=False) call
        breaches (elapsed monotonic time since Budget construction is
        always > 0.0), without needing to fake the clock."""
        store = _make_zarr_store(self.tmp)
        out_dir = os.path.join(self.tmp, "out")

        rc = cli.main([store, "--non-interactive", "--time-cap", "0", "--out", out_dir])

        self.assertEqual(rc, 0)  # never crashes -- run still completes
        findings = json.loads((Path(out_dir) / "findings.json").read_text())
        asset = findings["assets"][0]
        breaches = asset.get("budget_breaches") or []
        self.assertGreater(len(breaches), 0)
        self.assertTrue(any(b.get("stage") == "open" for b in breaches))
        self.assertTrue(all(b.get("breached") for b in breaches))

        report = (Path(out_dir) / "assessment-report.md").read_text()
        self.assertIn("Budget / stage breaches", report)
        self.assertIn("open", report)
        self.assertNotIn("No budget breaches", report)

    def test_all_variables_selects_every_variable(self):
        store = _make_zarr_store(self.tmp)
        out_dir = os.path.join(self.tmp, "out")
        rc = cli.main([store, "--non-interactive", "--all-variables", "--out", out_dir])
        self.assertEqual(rc, 0)
        findings = json.loads((Path(out_dir) / "findings.json").read_text())
        names = {v["name"] for v in findings["assets"][0]["inventory"]}
        self.assertEqual(names, {"/temperature", "/time"})

    def test_variables_flag_selects_named_variable(self):
        store = _make_zarr_store(self.tmp)
        out_dir = os.path.join(self.tmp, "out")
        rc = cli.main([store, "--non-interactive", "--variables", "temperature",
                       "--out", out_dir])
        self.assertEqual(rc, 0)
        findings = json.loads((Path(out_dir) / "findings.json").read_text())
        names = {v["name"] for v in findings["assets"][0]["inventory"]}
        self.assertEqual(names, {"/temperature"})

    def test_bogus_variable_exits_4_and_lists_available(self):
        store = _make_zarr_store(self.tmp)
        out_dir = os.path.join(self.tmp, "out")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = cli.main([store, "--non-interactive", "--variables", "bogus",
                           "--out", out_dir])
        self.assertEqual(rc, 4)
        err = buf.getvalue()
        self.assertIn("bogus", err)
        self.assertIn("temperature", err)

    def test_json_flag_prints_valid_json_to_stdout(self):
        store = _make_zarr_store(self.tmp)
        out_dir = os.path.join(self.tmp, "out")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main([store, "--non-interactive", "--json", "--out", out_dir])
        self.assertEqual(rc, 0)
        stdout = buf.getvalue()
        # The JSON blob is one of possibly several printed lines/blocks;
        # find it by locating the outermost JSON object.
        start = stdout.index("{")
        end = stdout.rindex("}") + 1
        parsed = json.loads(stdout[start:end])
        self.assertIn("verdict", parsed)

    def test_suggest_llm_without_api_key_falls_back_without_error(self):
        store = _make_zarr_store(self.tmp)
        out_dir = os.path.join(self.tmp, "out")
        env = dict(os.environ)
        env.pop("ANTHROPIC_API_KEY", None)
        with mock.patch.dict(os.environ, env, clear=True):
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                rc = cli.main([store, "--non-interactive", "--suggest", "llm",
                               "--out", out_dir])
        self.assertEqual(rc, 0)
        self.assertIn("llm suggestion unavailable", buf.getvalue())

    def test_missing_input_non_interactive_hard_errors(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = cli.main(["--non-interactive"])
        self.assertEqual(rc, 5)
        self.assertIn("INPUT", buf.getvalue())

    def test_interactive_path_completes(self):
        store = _make_zarr_store(self.tmp)
        out_dir = os.path.join(self.tmp, "out")
        # format accept, variable pick by number, benchmark decline, style choice
        answers = iter(["y", "0", "n", ""])

        def fake_input(prompt=""):
            return next(answers)

        stdout = io.StringIO()
        with mock.patch("sys.stdin.isatty", return_value=True), \
             mock.patch("builtins.input", side_effect=fake_input), \
             contextlib.redirect_stdout(stdout):
            rc = cli.main([store, "--out", out_dir])

        self.assertEqual(rc, 0)
        transcript = stdout.getvalue()
        self.assertTrue(any(name in transcript for name in ("temperature", "time")))
        findings = json.loads((Path(out_dir) / "findings.json").read_text())
        self.assertEqual(len(findings["assets"][0]["inventory"]), 1)

    def test_local_zarr_input_gets_a_real_measured_chunk_size(self):
        """Finding-2 fix: a local-path input's access_result.fs is None
        (access.workflow's local branch never builds a filesystem) --
        that None used to be passed straight into
        chunking.assess_chunking, whose zarr sampler then failed
        internally (fs.find on None) and silently degraded to the
        2:1-ratio *estimate*, with the raw AttributeError string landing
        in findings.json's coordinate_chunking_notes. A local zarr store
        run through the real CLI path must get a genuinely measured
        chunk size instead."""
        store = _make_zarr_store(self.tmp)
        out_dir = os.path.join(self.tmp, "out")

        rc = cli.main([store, "--non-interactive", "--out", out_dir])

        self.assertEqual(rc, 0)
        raw = (Path(out_dir) / "findings.json").read_text()
        findings = json.loads(raw)
        variables = findings["assets"][0]["chunking"]["variables"]
        self.assertTrue(variables)
        for v in variables:
            self.assertTrue(v["measured"], msg=v)
        self.assertNotIn("AttributeError", raw)
        self.assertNotIn("NoneType", raw)

    def test_cf_checks_see_the_full_inventory_not_just_the_selection(self):
        """Finding-4 fix: CF conformance is a file-level property. The
        store fixture has a real CF time coordinate variable ("/time")
        that only shows up in the FULL inventory, not in a
        single-variable selection of "temperature". Before the fix,
        conventions.check_cf was called with only the selected
        variable(s), so coordinate_identification came back ok=None
        ("not evaluated") even though a real, resolvable time coordinate
        exists in the file. After the fix it must see the full
        inventory and correctly report ok=True."""
        store = _make_zarr_store(self.tmp)
        out_dir = os.path.join(self.tmp, "out")

        rc = cli.main([store, "--non-interactive", "--variables", "temperature",
                       "--out", out_dir])

        self.assertEqual(rc, 0)
        findings = json.loads((Path(out_dir) / "findings.json").read_text())
        asset = findings["assets"][0]
        self.assertEqual([v["name"] for v in asset["inventory"]], ["/temperature"])

        checks = {c["id"]: c for c in asset["conventions"]["cf"]["checks"]}
        self.assertIsNotNone(checks["coordinate_identification"]["ok"])
        self.assertTrue(checks["coordinate_identification"]["ok"],
                        msg=checks["coordinate_identification"])


@unittest.skipUnless(h5py, "h5py not installed")
class Hdf5E2ETests(_TmpDirCase):
    def test_non_interactive_hdf5_run_succeeds(self):
        path = _make_hdf5_file(self.tmp)
        out_dir = os.path.join(self.tmp, "out")

        rc = cli.main([path, "--non-interactive", "--report-style", "both",
                       "--out", out_dir])

        self.assertEqual(rc, 0)
        findings = json.loads((Path(out_dir) / "findings.json").read_text())
        self.assertIsNotNone(findings["verdict"])
        self.assertIsNotNone(findings["score"])
        self.assertIsNotNone(findings["tier"])

        report = (Path(out_dir) / "assessment-report.md").read_text()
        self.assertTrue(report.startswith("**Verdict:"))
        self.assertIn("Chunk layout", report)


@unittest.skipUnless(zarr, "zarr not installed")
class EtagThreadingTests(_TmpDirCase):
    """Final-review finding 3: a live probe's captured ETag must reach
    asset["etag"] in findings.json (nothing in cli.py used to set it at
    all, so E1-version/E2-checksums were permanently fail/partial
    regardless of what the endpoint actually served). Faithfully
    exercises the real cli.py wiring (asset["etag"] = access_result.etag)
    offline by monkeypatching access.workflow.resolve_access to return a
    canned AccessResult with a strong ETag set -- the same pattern
    FatalAccessErrorTests below uses to exercise auth="error" without
    live network access, since access.workflow.resolve_access is the
    sole producer of AccessResult.
    """

    def test_strong_etag_from_access_result_reaches_asset_and_unblocks_e1_e2(self):
        from check_cloud_ready.access.workflow import AccessResult

        store = _make_zarr_store(self.tmp)
        out_dir = os.path.join(self.tmp, "out")
        canned = AccessResult(
            fs=None, path=store, url=store, findings=[], auth="anonymous",
            notes=[], etag='"strong-etag-abc123"')

        with mock.patch("check_cloud_ready.access.workflow.resolve_access",
                        return_value=canned):
            rc = cli.main([store, "--non-interactive", "--out", out_dir])

        self.assertEqual(rc, 0)
        findings = json.loads((Path(out_dir) / "findings.json").read_text())
        asset = findings["assets"][0]
        self.assertEqual(asset["etag"], '"strong-etag-abc123"')

        checks = {c["id"]: c for c in asset["checks"]}
        self.assertNotEqual(checks["E1-version"]["status"], "fail")
        self.assertNotEqual(checks["E2-checksums"]["status"], "fail")

    def test_no_etag_from_access_result_leaves_asset_etag_none(self):
        store = _make_zarr_store(self.tmp)
        out_dir = os.path.join(self.tmp, "out")

        rc = cli.main([store, "--non-interactive", "--out", out_dir])

        self.assertEqual(rc, 0)
        findings = json.loads((Path(out_dir) / "findings.json").read_text())
        self.assertIsNone(findings["assets"][0]["etag"])


class FatalAccessErrorTests(_TmpDirCase):
    def test_auth_error_exits_2_with_guidance(self):
        """Constructing a genuine auth="error" result requires a live
        network classification (protected S3/HTTPS with no working
        credential path) -- out of scope for this offline suite. Since
        access.workflow.resolve_access is the sole producer of
        AccessResult and cli.py's contract is simply "auth == 'error'
        -> print findings, exit 2", monkeypatching resolve_access to
        return a canned auth="error" result is a faithful, offline way
        to exercise that contract without any network access.
        """
        from check_cloud_ready.access.workflow import AccessResult

        canned = AccessResult(
            fs=None, path="s3://bucket/key", url="s3://bucket/key",
            findings=[{"id": "D2-auth", "dim": "D", "status": "fail",
                      "evidence": "no working credential path",
                      "remediation": "supply a granule ID or credentials endpoint"}],
            auth="error", notes=[])

        buf = io.StringIO()
        with mock.patch("check_cloud_ready.access.workflow.resolve_access",
                        return_value=canned), \
             contextlib.redirect_stderr(buf):
            rc = cli.main(["s3://bucket/key", "--non-interactive",
                          "--out", os.path.join(self.tmp, "out")])

        self.assertEqual(rc, 2)
        err = buf.getvalue()
        self.assertIn("no working credential path", err)
        self.assertIn("supply a granule ID", err)


class DegradedOpenTests(_TmpDirCase):
    def test_no_opener_registered_still_produces_a_report(self):
        """A format with no opener (e.g. csv) yields OpenResult
        status="n/a" -- the whole pipeline must still complete and
        write a report rather than crashing."""
        path = os.path.join(self.tmp, "plain.csv")
        with open(path, "w") as f:
            f.write("a,b\n1,2\n")
        out_dir = os.path.join(self.tmp, "out")

        rc = cli.main([path, "--non-interactive", "--format", "csv", "--out", out_dir])

        self.assertEqual(rc, 0)
        findings = json.loads((Path(out_dir) / "findings.json").read_text())
        asset = findings["assets"][0]
        self.assertEqual(asset["smoke_status"], "skipped")
        self.assertIsNone(asset["chunking"])
        self.assertIsNotNone(asset["score"])  # scoring still ran, degraded
        self.assertTrue((Path(out_dir) / "assessment-report.md").exists())


if __name__ == "__main__":
    unittest.main()
