"""Offline tests for check_cloud_ready.access.probe.

All tests here run offline: no real network access is made.
probe_https tests inject a fake ``session`` object; classify_access_failure
tests are pure. probe_s3's soft-import path is exercised against this
dev environment's real (missing) boto3 install, without mocking.
"""
import sys
import unittest
from unittest import mock

from check_cloud_ready.access import probe

classify_access_failure = probe.classify_access_failure
probe_https = probe.probe_https


class TestClassifyAccessFailure(unittest.TestCase):
    def test_401_at_head_is_auth_required(self):
        self.assertEqual(classify_access_failure("head", 401), "auth-required")

    def test_403_at_ranged_get_is_auth_required_not_range_support(self):
        # The S2 regression: NASA's TEA distribution answers HEAD
        # anonymously but gates the ranged GET with a 403. That must be
        # an auth failure, never a "server doesn't support ranges"
        # failure.
        self.assertEqual(classify_access_failure("range-get", 403), "auth-required")

    def test_urs_redirect_location_header_is_auth_required(self):
        self.assertEqual(
            classify_access_failure(
                "head", "https://urs.earthdata.nasa.gov/oauth/authorize?x"),
            "auth-required")

    def test_urs_redirect_at_range_get_stage_is_auth_required(self):
        self.assertEqual(
            classify_access_failure(
                "range-get", "redirected via urs.earthdata.nasa.gov"),
            "auth-required")

    def test_200_with_ignored_range_is_no_range_support(self):
        self.assertEqual(classify_access_failure("range-get", 200), "no-range-support")

    def test_206_is_ok(self):
        self.assertEqual(classify_access_failure("range-get", 206), "ok")

    def test_head_200_is_ok(self):
        self.assertEqual(classify_access_failure("head", 200), "ok")

    def test_403_exception_object_at_open_stage_is_auth_required(self):
        exc = Exception("An error occurred (403) when calling HeadObject: Forbidden")
        self.assertEqual(classify_access_failure("open", exc), "auth-required")

    def test_s3_accessdenied_code_at_range_get_is_auth_required(self):
        # S2 regression, S3-flavored: an anonymous HeadObject succeeds
        # but the ranged GetObject comes back AccessDenied.
        self.assertEqual(
            classify_access_failure("range-get", "AccessDenied"), "auth-required")

    def test_unrecognized_head_error_defaults_to_ok(self):
        # A HEAD failure that isn't recognizably an auth problem is
        # "informative, not fatal" -- never treated as fatal or as a
        # range-support failure (only "range-get" can produce that).
        self.assertEqual(
            classify_access_failure("head", ConnectionError("timed out")), "ok")


class _FakeRaw:
    def __init__(self, body):
        self._body = body

    def read(self, n):
        return self._body[:n]


class FakeResponse:
    def __init__(self, status_code, headers=None, body=b"", url="https://example.com/data",
                 history=None):
        self.status_code = status_code
        self.headers = headers or {}
        self.url = url
        self.history = history or []
        self.raw = _FakeRaw(body)
        self.closed = False

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, head_response, get_response):
        self._head_response = head_response
        self._get_response = get_response
        self.calls = []

    def head(self, url, timeout=None, allow_redirects=None):
        self.calls.append(("head", url))
        return self._head_response

    def get(self, url, headers=None, timeout=None, allow_redirects=None, stream=None):
        self.calls.append(("get", url, headers))
        return self._get_response


class TestProbeHttps(unittest.TestCase):
    def test_206_range_supported(self):
        head = FakeResponse(200, headers={"Accept-Ranges": "bytes", "Content-Length": "1000"})
        get = FakeResponse(206, headers={"Content-Range": "bytes 0-1023/1000"},
                            body=b"\x89HDF\r\n\x1a\n")
        session = FakeSession(head, get)

        result = probe_https("https://example.com/data.h5", session=session)

        self.assertEqual(result["head_classification"], "ok")
        self.assertEqual(result["range_classification"], "ok")
        self.assertEqual(result["classification"], "ok")
        self.assertEqual(result["range_get_status"], 206)
        self.assertEqual(result["range_requests"], "supported")

    def test_200_range_ignored_is_no_range_support(self):
        head = FakeResponse(200)
        get = FakeResponse(200, body=b"whole-body-not-ranged")
        session = FakeSession(head, get)

        result = probe_https("https://example.com/data.h5", session=session)

        self.assertEqual(result["range_classification"], "no-range-support")
        self.assertEqual(result["classification"], "no-range-support")
        self.assertIn("note", result)

    def test_urs_redirect_history_is_auth_required(self):
        head = FakeResponse(200)
        redirect_hop = FakeResponse(
            302, headers={"Location": "https://urs.earthdata.nasa.gov/oauth/authorize?x"})
        get = FakeResponse(200, history=[redirect_hop])
        session = FakeSession(head, get)

        result = probe_https("https://example.com/data.h5", session=session)

        self.assertEqual(result["range_classification"], "auth-required")
        self.assertEqual(result["classification"], "auth-required")
        self.assertEqual(result["auth_detected"], "earthdata-login")

    def test_urs_redirect_at_head_is_auth_required(self):
        head = FakeResponse(
            302, headers={"Location": "https://urs.earthdata.nasa.gov/oauth/authorize?x"})
        get = FakeResponse(401)
        session = FakeSession(head, get)

        result = probe_https("https://example.com/data.h5", session=session)

        self.assertEqual(result["head_classification"], "auth-required")
        self.assertEqual(result["classification"], "auth-required")

    def test_head_failure_is_not_fatal(self):
        class RejectsHead(FakeSession):
            def head(self, *a, **k):
                raise RuntimeError("HEAD not allowed")

        session = RejectsHead(None, FakeResponse(206, body=b"0123456789"))
        result = probe_https("https://example.com/data.h5", session=session)

        self.assertIn("head_error", result)
        self.assertEqual(result["classification"], "ok")

    def test_requests_missing_reports_skipped(self):
        with mock.patch.dict(sys.modules, {"requests": None}):
            result = probe_https("https://example.com/data.h5")
        self.assertIn("skipped", result)
        self.assertEqual(result["scheme"], "https")


class TestProbeS3SkippedWithoutBoto3(unittest.TestCase):
    def test_boto3_missing_reports_skipped(self):
        try:
            import boto3  # noqa: F401
        except ImportError:
            result = probe.probe_s3("s3://example-bucket/key")
            self.assertIn("skipped", result)
            self.assertEqual(result["scheme"], "s3")
        else:
            self.skipTest("boto3 is installed in this environment")


class TestProbeLocal(unittest.TestCase):
    def test_local_path_marks_not_assessable(self):
        result = probe.probe_local("/tmp/definitely-does-not-exist-check-cloud-ready.nc")
        self.assertEqual(result["scheme"], "local")
        self.assertFalse(result["exists"])
        self.assertEqual(result["classification"], "not-assessable")


if __name__ == "__main__":
    unittest.main()
