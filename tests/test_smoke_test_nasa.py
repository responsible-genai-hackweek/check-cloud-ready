"""Offline tests for the NASA Earthdata S3 path in the
earth-science-cloud-readiness skill's smoke_test.py / assess.py.

Everything here runs with no network access: httpx and urllib are
monkeypatched to raise if anything tries to reach the network, and the
authenticated filesystem is a stub injected in place of
`nasa_s3.get_fs`. The point of most of these tests is exactly that: a
protected NASA s3:// URL must never be rewritten to a public
`*.s3.amazonaws.com` HTTPS URL and probed.
"""
import importlib.util
import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(
    REPO_ROOT, ".claude", "skills", "earth-science-cloud-readiness", "scripts")

NASA_URL = "s3://sds-n-cumulus-prod-nisar-products/x.h5"
CREDS_URL = "https://nisar.asf.earthdatacloud.nasa.gov/s3credentials"


def _load(name):
    path = os.path.join(SCRIPTS, name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


smoke_test = _load("smoke_test")
assess = _load("assess")


class _NoNetwork:
    """Any attempt to build an HTTP client is a test failure."""

    def __init__(self, testcase):
        self.testcase = testcase

    def Client(self, *a, **k):
        self.testcase.fail("smoke_test attempted an HTTP request "
                           "(NASA S3 must never be rerouted to HTTPS)")


class NasaS3Mixin:
    """Blocks all outbound HTTP for the duration of a test."""

    def block_network(self):
        original_httpx = smoke_test.httpx
        smoke_test.httpx = _NoNetwork(self)
        self.addCleanup(setattr, smoke_test, "httpx", original_httpx)

        def _no_urlopen(*a, **k):
            self.fail("smoke_test attempted a urllib request")

        original_token = smoke_test.nasa_s3.edl_bearer_token
        smoke_test.nasa_s3.edl_bearer_token = _no_urlopen
        self.addCleanup(setattr, smoke_test.nasa_s3, "edl_bearer_token",
                        original_token)

    def stub_get_fs(self, fs):
        calls = []

        def fake_get_fs(url, **kwargs):
            calls.append((url, kwargs))
            return fs, url

        original = smoke_test.nasa_s3.get_fs
        smoke_test.nasa_s3.get_fs = fake_get_fs
        self.addCleanup(setattr, smoke_test.nasa_s3, "get_fs", original)
        smoke_test._AUTH_FS_CACHE.clear()
        self.addCleanup(smoke_test._AUTH_FS_CACHE.clear)
        return calls


class _DeniedFS:
    """Credentials minted fine; S3 denies the read (the out-of-region case)."""

    def info(self, path):
        raise PermissionError("Access Denied")

    def cat_file(self, path, start=None, end=None):  # pragma: no cover
        raise PermissionError("Access Denied")


class _OkFS:
    """Succeeds for info + cat_file; the format open is out of scope here."""

    SIZE = 4 * 1024 * 1024

    def __init__(self):
        self.calls = []

    def info(self, path):
        self.calls.append(("info", path))
        return {"name": path, "size": self.SIZE, "type": "file",
                "ETag": '"deadbeef"'}

    def cat_file(self, path, start=None, end=None):
        self.calls.append(("cat_file", path, start, end))
        start = start or 0
        n = (end - start) if end is not None else 1024
        # HDF5 magic so the bytes look like a real header read
        return (b"\x89HDF\r\n\x1a\n" + b"\x00" * n)[:n]

    def open(self, path, mode="rb", **kwargs):
        raise RuntimeError("stub filesystem: open() not implemented")


class TestNoCredentials(NasaS3Mixin, unittest.TestCase):
    def test_nasa_s3_without_credentials_is_skipped_never_probed(self):
        self.block_network()
        tel = smoke_test.run_smoke_test(NASA_URL, "hdf5")
        self.assertEqual(tel["status"], "skipped")
        self.assertTrue(tel["reason"].startswith("nasa-credentials-required"),
                        tel["reason"])
        self.assertNotIn("head", tel)
        self.assertNotIn("ranged", tel)

    def test_counting_fs_returns_sentinel(self):
        cfs, path = smoke_test._counting_fs_for(NASA_URL, smoke_test.Budget())
        self.assertIsNone(cfs)
        self.assertEqual(path, smoke_test.NASA_CREDENTIALS_REQUIRED)

    def test_non_nasa_s3_still_uses_https_probe_url(self):
        # regression guard: _to_https is untouched for non-NASA object storage
        self.assertEqual(smoke_test._to_https("s3://sentinel-cogs/x.tif"),
                         "https://sentinel-cogs.s3.amazonaws.com/x.tif")


class TestCredentialedDenied(NasaS3Mixin, unittest.TestCase):
    def test_access_denied_is_skipped_in_region_only(self):
        self.block_network()
        calls = self.stub_get_fs(_DeniedFS())
        tel = smoke_test.run_smoke_test(NASA_URL, "hdf5",
                                        credentials_url=CREDS_URL)
        self.assertEqual(tel["status"], "skipped")
        self.assertTrue(tel["reason"].startswith("in-region-only"), tel["reason"])
        self.assertEqual(calls[0][1]["credentials_url"], CREDS_URL)
        self.assertNotIn("ranged", tel)

    def test_credentials_endpoint_auth_failure_is_skipped(self):
        self.block_network()

        class _EdlFailFS:
            def info(self, path):
                raise RuntimeError(
                    "401 Unauthorized from https://urs.earthdata.nasa.gov/oauth")

        self.stub_get_fs(_EdlFailFS())
        tel = smoke_test.run_smoke_test(NASA_URL, "hdf5",
                                        credentials_url=CREDS_URL)
        self.assertEqual(tel["status"], "skipped")
        self.assertTrue(tel["reason"].startswith("auth:"), tel["reason"])


class TestCredentialedSuccess(NasaS3Mixin, unittest.TestCase):
    def test_authenticated_telemetry_field_names(self):
        self.block_network()
        fs = _OkFS()
        self.stub_get_fs(fs)
        tel = smoke_test.run_smoke_test(NASA_URL, "hdf5",
                                        credentials_url=CREDS_URL)
        self.assertNotEqual(tel["status"], "fail")
        head = tel["head"]
        self.assertEqual(head["transport"], "s3-authenticated")
        self.assertEqual(head["status"], "pass")
        self.assertEqual(head["content_length"], _OkFS.SIZE)
        self.assertIn("latency_ms", head)

        ranged = tel["ranged"]
        self.assertEqual(ranged["status"], "pass")
        # same first-bytes field names the HTTPS ranged probe produces
        self.assertEqual(ranged["first_bytes_hex"],
                         b"\x89HDF\r\n\x1a\n\x00\x00\x00\x00\x00\x00\x00\x00".hex())
        self.assertNotIn("first_bytes", ranged)  # popped, exactly as for HTTPS
        self.assertEqual(ranged["reads"][0]["bytes"], smoke_test.HEADER_READ)
        # interior range too (object is > 4x the header read)
        self.assertEqual(len(ranged["reads"]), 2)
        # detect_format consumes first_bytes_hex from this telemetry
        self.assertEqual(assess.detect_format({"url": NASA_URL}, tel), "hdf5")

    def test_authenticated_probes_charge_the_budget(self):
        # The authenticated probes go through CountingFS, whose info/cat_file
        # wrappers spend exactly like the HTTPS probes do: 1 request + 0 bytes
        # for the HEAD, 1 request + n bytes per ranged read. Pinned here so a
        # future edit can neither drop nor double the accounting.
        self.block_network()
        self.stub_get_fs(_OkFS())
        tel = smoke_test.run_smoke_test(NASA_URL, "hdf5",
                                        credentials_url=CREDS_URL)
        self.assertEqual(tel["budget"]["requests"], 3)  # info + 2 cat_file
        self.assertEqual(tel["budget"]["bytes"], 2 * smoke_test.HEADER_READ)

    def test_credentialed_zarr_store_root_still_emits_head(self):
        self.block_network()

        class _StoreFS:
            """Root is a prefix (no object); the metadata document exists."""

            _strip_protocol = staticmethod(lambda p: p)  # for fsspec.FSMap

            def info(self, path):
                if path.endswith(".zmetadata"):
                    return {"name": path, "size": 10551, "type": "file"}
                raise FileNotFoundError(path)

            def cat_file(self, path, start=None, end=None):
                raise FileNotFoundError(path)

        self.stub_get_fs(_StoreFS())
        tel = smoke_test.run_smoke_test("s3://podaac-ops-cumulus-protected/x.zarr",
                                        "zarr", credentials_url=CREDS_URL)
        head = tel["head"]
        self.assertEqual(head["transport"], "s3-authenticated")
        self.assertEqual(head["status"], "pass")
        self.assertEqual(head["content_length"], 10551)
        self.assertNotIn("ranged", tel)  # store roots: ranged stays exempt

        # assess.py must not claim this was a local file
        checks = assess.score_asset({"url": "s3://podaac-ops-cumulus-protected/x.zarr"},
                                    "zarr", tel)
        d_evidence = " ".join(c["evidence"] for c in checks
                              if c["dimension"] == "D")
        self.assertNotIn("local file", d_evidence)
        self.assertNotIn("not live-verified", d_evidence)

        # D2-auth: credentialed heads have no http_status; evidence must not
        # render the misleading "HTTP None".
        d2 = next(c for c in checks if c["id"] == "D2-auth")
        self.assertNotIn("HTTP None", d2["evidence"])
        self.assertEqual(d2["status"], "pass")

        # D3-https-cors: SDK-transport S3 has no CORS concept, so the
        # credentialed path must not be dinged for missing CORS headers nor
        # tell the provider to "serve over HTTPS; add CORS" (both wrong for
        # this transport).
        d3 = next(c for c in checks if c["id"] == "D3-https-cors")
        self.assertEqual(d3["status"], "pass")
        self.assertEqual(d3["points_awarded"], d3["points_possible"])
        self.assertIsNone(d3["remediation"])
        self.assertNotIn("serve over HTTPS; add CORS", str(d3.get("remediation")))

    def test_credentialed_zarr_root_without_metadata_is_truthful(self):
        self.block_network()

        class _EmptyStoreFS:
            _strip_protocol = staticmethod(lambda p: p)  # for fsspec.FSMap

            def info(self, path):
                raise FileNotFoundError(path)

            def cat_file(self, path, start=None, end=None):
                raise FileNotFoundError(path)

        self.stub_get_fs(_EmptyStoreFS())
        tel = smoke_test.run_smoke_test("s3://podaac-ops-cumulus-protected/x.zarr",
                                        "zarr", credentials_url=CREDS_URL)
        self.assertEqual(tel["head"]["transport"], "s3-authenticated")
        self.assertEqual(tel["head"]["status"], "skipped")
        self.assertIn("store root", tel["head"]["reason"])

    def test_fs_is_cached_per_credentials_url(self):
        self.block_network()
        calls = self.stub_get_fs(_OkFS())
        smoke_test.run_smoke_test(NASA_URL, "hdf5", credentials_url=CREDS_URL)
        smoke_test.run_smoke_test(NASA_URL, "hdf5", credentials_url=CREDS_URL)
        self.assertEqual(len(calls), 1, "get_fs should be called once per endpoint")


class TestProtectedHttpsBearer(unittest.TestCase):
    """HTTPS inputs (only) may be retried with an EDL bearer token."""

    def _fake_token(self, value):
        original = smoke_test.nasa_s3.edl_bearer_token
        smoke_test.nasa_s3.edl_bearer_token = lambda: value
        self.addCleanup(setattr, smoke_test.nasa_s3, "edl_bearer_token", original)

    def _forbid_token(self):
        """Any attempt to mint/read an EDL token is a test failure."""
        original = smoke_test.nasa_s3.edl_bearer_token

        def _boom():
            self.fail("EDL bearer token requested for a non-NASA host")

        smoke_test.nasa_s3.edl_bearer_token = _boom
        self.addCleanup(setattr, smoke_test.nasa_s3, "edl_bearer_token", original)

    def _capture_head_probe(self, response):
        """Replace head_probe with a stub; records the headers it was given."""
        seen = []

        def fake_head_probe(url, budget, headers=None):
            seen.append(headers)
            return dict(response)

        original = smoke_test.head_probe
        smoke_test.head_probe = fake_head_probe
        self.addCleanup(setattr, smoke_test, "head_probe", original)
        return seen

    def test_non_nasa_401_never_receives_the_bearer_token(self):
        self._forbid_token()
        seen = self._capture_head_probe(
            {"check": "HEAD", "status": "skipped", "reason": "auth",
             "http_status": 401,
             "final_url": "https://data.example.com/private/x.h5"})
        tel = {}
        headers = smoke_test._https_probe_with_bearer(
            "https://data.example.com/private/x.h5", smoke_test.Budget(), tel)
        self.assertIsNone(headers)
        self.assertEqual(seen, [None])  # exactly one probe, no Authorization
        self.assertIn("not a NASA Earthdata host", tel["head"]["bearer_retry"])

    def test_nasa_401_does_receive_the_bearer_token(self):
        self._fake_token("test-token")
        seen = self._capture_head_probe(
            {"check": "HEAD", "status": "skipped", "reason": "auth",
             "http_status": 403,
             "final_url": "https://data.lpdaac.earthdatacloud.nasa.gov/x.h5"})
        tel = {}
        smoke_test._https_probe_with_bearer(
            "https://data.lpdaac.earthdatacloud.nasa.gov/x.h5",
            smoke_test.Budget(), tel)
        self.assertEqual(seen[0], None)
        self.assertEqual(seen[1], {"Authorization": "Bearer test-token"})

    def test_non_nasa_url_redirected_to_edl_may_use_the_token(self):
        # The heuristic misses plenty of NASA hosts; a URS redirect is proof.
        self._fake_token("test-token")
        seen = self._capture_head_probe(
            {"check": "HEAD", "status": "skipped", "reason": "auth",
             "http_status": 401,
             "final_url": "https://urs.earthdata.nasa.gov/oauth/authorize?x=1"})
        smoke_test._https_probe_with_bearer(
            "https://opaque.example.org/x.h5", smoke_test.Budget(), {})
        self.assertEqual(seen[1], {"Authorization": "Bearer test-token"})

    def test_non_nasa_ranged_401_is_skipped_without_a_token(self):
        self._forbid_token()
        ranged = {"check": "ranged_reads", "status": "fail",
                  "reads": [{"range": "0-16383", "http_status": 401, "bytes": 27,
                             "expected": 16384, "ok": False}]}
        out = smoke_test._ranged_auth_recovery(
            "https://data.example.com/private/x.h5", smoke_test.Budget(),
            ranged, None, None, bearer_allowed=False)
        self.assertEqual(out["status"], "skipped")
        self.assertTrue(out["auth_blocked"])
        self.assertIn("not a NASA Earthdata host", out["bearer_retry"])
        self.assertTrue(out["reason"].startswith("auth:"), out["reason"])

    def test_auth_blocked_reads_are_skipped_not_failed(self):
        self._fake_token(None)
        ranged = {"check": "ranged_reads", "status": "fail",
                  "reads": [{"range": "0-16383", "http_status": 401, "bytes": 27,
                             "expected": 16384, "ok": False}]}
        out = smoke_test._ranged_auth_recovery(
            "https://nisar.asf.earthdatacloud.nasa.gov/x.h5",
            smoke_test.Budget(), ranged, None, None, bearer_allowed=True)
        self.assertEqual(out["status"], "skipped")
        self.assertTrue(out["auth_blocked"])
        self.assertTrue(out["reason"].startswith("auth:"), out["reason"])
        self.assertIn("bearer_retry", out)  # explains why no retry happened

    def test_bearer_retry_recovers_reads(self):
        self._fake_token("test-token")
        seen = {}

        def fake_ranged_probe(url, budget, content_length=None, headers=None):
            seen["headers"] = headers
            return {"check": "ranged_reads", "status": "pass",
                    "reads": [{"range": "0-16383", "http_status": 206,
                               "bytes": 16384, "expected": 16384, "ok": True}]}

        original = smoke_test.ranged_probe
        smoke_test.ranged_probe = fake_ranged_probe
        self.addCleanup(setattr, smoke_test, "ranged_probe", original)

        blocked = {"check": "ranged_reads", "status": "fail",
                   "reads": [{"range": "0-16383", "http_status": 403,
                              "bytes": 0, "expected": 16384, "ok": False}]}
        out = smoke_test._ranged_auth_recovery(
            "https://nisar.asf.earthdatacloud.nasa.gov/x.h5",
            smoke_test.Budget(), blocked, None, None, bearer_allowed=True)
        self.assertEqual(out["status"], "pass")
        self.assertTrue(out["bearer_auth"])
        self.assertEqual(seen["headers"], {"Authorization": "Bearer test-token"})
        self.assertNotIn("auth_blocked", out)


class TestAssessCmrGranule(unittest.TestCase):
    def test_detect_input(self):
        self.assertEqual(assess.detect_input("G4289749526-ASF"), "cmr-granule")
        self.assertEqual(assess.detect_input("G1234567890-POCLOUD"), "cmr-granule")
        self.assertEqual(assess.detect_input("s3://sentinel-cogs/x.tif"), "asset")

    def test_gather_assets_sets_credentials_url(self):
        resolved = {
            "credentials_url": CREDS_URL,
            "s3_urls": ["s3://sds-n-cumulus-prod-nisar-products/a.h5",
                        "s3://sds-n-cumulus-prod-nisar-products/b.h5"],
            "https_urls": ["https://nisar.asf.earthdatacloud.nasa.gov/a.h5"],
            "provider": "ASF", "granule_id": "G4289749526-ASF",
            "note": "extra informational keys are ignored",
        }
        original = assess.resolve_granule.resolve
        assess.resolve_granule.resolve = lambda gid: dict(resolved, granule_id=gid)
        self.addCleanup(setattr, assess.resolve_granule, "resolve", original)

        notes = []
        assets, frame = assess.gather_assets(
            "G4289749526-ASF", "cmr-granule", None, False, False, notes)
        self.assertEqual(len(assets), 2)
        self.assertEqual([a["url"] for a in assets], resolved["s3_urls"])
        self.assertEqual(frame["credentials_url"], CREDS_URL)
        self.assertEqual(frame["granule_id"], "G4289749526-ASF")
        self.assertEqual(notes, [])

    def test_gather_assets_no_s3_urls_is_unassessable(self):
        original = assess.resolve_granule.resolve
        assess.resolve_granule.resolve = lambda gid: {
            "credentials_url": CREDS_URL, "s3_urls": [],
            "https_urls": ["https://example.gov/a.h5"], "provider": "ASF"}
        self.addCleanup(setattr, assess.resolve_granule, "resolve", original)

        notes = []
        assets, frame = assess.gather_assets(
            "G4289749526-ASF", "cmr-granule", None, False, False, notes)
        self.assertEqual(assets, [])
        self.assertTrue(any("G4289749526-ASF" in n for n in notes), notes)


if __name__ == "__main__":
    unittest.main()
