"""Offline tests for nasa_s3.py.

The module under test is imported from the check-cloud-ready copy:
    .claude/skills/check-cloud-ready/scripts/nasa_s3.py
A separate test (test_copies_are_identical) asserts the
earth-science-cloud-readiness copy is byte-identical to it, so testing
one copy covers both.

All tests here run offline, with no network access, and do not require
obstore/earthaccess/s3fs to be installed (tests that need one of those
packages skip themselves when it's missing).
"""
import importlib.util
import os
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHECK_CLOUD_READY_PATH = os.path.join(
    REPO_ROOT, ".claude", "skills", "check-cloud-ready", "scripts", "nasa_s3.py")
EARTH_SCIENCE_PATH = os.path.join(
    REPO_ROOT, ".claude", "skills", "earth-science-cloud-readiness", "scripts",
    "nasa_s3.py")

_spec = importlib.util.spec_from_file_location("nasa_s3", CHECK_CLOUD_READY_PATH)
nasa_s3 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nasa_s3)

looks_like_nasa_earthdata = nasa_s3.looks_like_nasa_earthdata
classify_s3_error = nasa_s3.classify_s3_error
get_fs = nasa_s3.get_fs
edl_bearer_token = nasa_s3.edl_bearer_token


class TestLooksLikeNasaEarthdata(unittest.TestCase):
    def test_true_cases(self):
        self.assertTrue(looks_like_nasa_earthdata(
            "s3://sds-n-cumulus-prod-nisar-products/x.h5"))
        self.assertTrue(looks_like_nasa_earthdata(
            "https://data.lpdaac.earthdatacloud.nasa.gov/f"))
        self.assertTrue(looks_like_nasa_earthdata(
            "s3://podaac-ops-cumulus-protected/y"))

    def test_false_cases(self):
        self.assertFalse(looks_like_nasa_earthdata("s3://sentinel-cogs/x.tif"))
        self.assertFalse(looks_like_nasa_earthdata("https://example.com/data.nc"))


class TestCopiesIdentical(unittest.TestCase):
    def test_copies_are_identical(self):
        with open(CHECK_CLOUD_READY_PATH, "rb") as f:
            check_cloud_ready_bytes = f.read()
        with open(EARTH_SCIENCE_PATH, "rb") as f:
            earth_science_bytes = f.read()
        self.assertEqual(check_cloud_ready_bytes, earth_science_bytes)


class TestClassifyS3Error(unittest.TestCase):
    def test_credentials_endpoint_auth(self):
        exc = Exception(
            "HTTP 401 Unauthorized from "
            "https://nisar.asf.earthdatacloud.nasa.gov/s3credentials")
        self.assertEqual(classify_s3_error(exc), "credentials-endpoint-auth")

    def test_in_region_only(self):
        exc = PermissionError("Access Denied")
        self.assertEqual(classify_s3_error(exc), "in-region-only")

    def test_unknown(self):
        exc = ValueError("boom")
        self.assertEqual(classify_s3_error(exc), "unknown")

    def test_credentials_endpoint_auth_via_chained_cause(self):
        try:
            try:
                raise Exception("403 Forbidden calling urs.earthdata.nasa.gov")
            except Exception as inner:
                raise RuntimeError("fs construction failed") from inner
        except RuntimeError as outer:
            self.assertEqual(classify_s3_error(outer), "credentials-endpoint-auth")

    def test_forbidden_without_credentials_markers_is_in_region(self):
        exc = Exception("An error occurred (AccessDenied) when calling GetObject: Forbidden")
        self.assertEqual(classify_s3_error(exc), "in-region-only")


class TestGetFsLegacyAnon(unittest.TestCase):
    def test_anon_s3fs(self):
        try:
            import s3fs  # noqa: F401
        except ImportError:
            self.skipTest("s3fs not installed")
        fs, path = get_fs("s3://sentinel-cogs/x.tif", anon=True)
        self.assertEqual(path, "s3://sentinel-cogs/x.tif")
        self.assertEqual(type(fs).__name__, "S3FileSystem")


class TestGetFsCredentialsUrl(unittest.TestCase):
    def test_credentials_url_obstore_missing_or_present(self):
        try:
            import obstore  # noqa: F401
        except ImportError:
            with self.assertRaises(RuntimeError) as ctx:
                get_fs(
                    "s3://podaac-ops-cumulus-protected/y",
                    credentials_url="https://archive.podaac.earthdata.nasa.gov/s3credentials",
                )
            self.assertIn("obstore", str(ctx.exception))
        else:
            fs, path = get_fs(
                "s3://podaac-ops-cumulus-protected/y",
                credentials_url="https://archive.podaac.earthdata.nasa.gov/s3credentials",
            )
            self.assertEqual(path, "s3://podaac-ops-cumulus-protected/y")
            self.assertEqual(type(fs).__name__, "FsspecStore")


class TestGetFsGranuleIdInvalid(unittest.TestCase):
    def test_invalid_granule_id_raises_runtime_error_not_system_exit(self):
        # resolve_granule.resolve() is CLI-style and calls sys.exit() on
        # an invalid granule ID (fails its local regex check, no network
        # involved). get_fs must convert that SystemExit into a
        # catchable RuntimeError rather than letting it propagate and
        # kill the process out from under an `except Exception` caller.
        with self.assertRaises(RuntimeError) as ctx:
            get_fs("s3://some-bucket/x", granule_id="not-a-valid-id")
        self.assertNotIsInstance(ctx.exception, SystemExit)

        try:
            get_fs("s3://some-bucket/x", granule_id="not-a-valid-id")
        except SystemExit:
            self.fail("get_fs leaked a SystemExit instead of raising RuntimeError")
        except Exception:
            pass  # expected: caught as a plain Exception


class TestEdlBearerToken(unittest.TestCase):
    def setUp(self):
        self._saved_env = {
            k: os.environ.get(k)
            for k in ("EARTHDATA_TOKEN", "EARTHDATA_USERNAME", "EARTHDATA_PASSWORD", "NETRC")
        }

    def tearDown(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_env_token_returned_verbatim(self):
        os.environ["EARTHDATA_TOKEN"] = "sekrit-test-token-value"
        self.assertEqual(edl_bearer_token(), "sekrit-test-token-value")

    def test_no_creds_returns_none_without_network(self):
        os.environ.pop("EARTHDATA_TOKEN", None)
        os.environ.pop("EARTHDATA_USERNAME", None)
        os.environ.pop("EARTHDATA_PASSWORD", None)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".netrc") as f:
            # Empty netrc file: no "urs.earthdata.nasa.gov" machine entry,
            # and pointing NETRC here means a real ~/.netrc on this
            # machine cannot leak into the test.
            f.flush()
            os.environ["NETRC"] = f.name
            self.assertIsNone(edl_bearer_token())


if __name__ == "__main__":
    unittest.main()
