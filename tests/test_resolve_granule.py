"""Offline tests for resolve_granule.py.

The module under test is imported from the check-cloud-ready copy:
    .claude/skills/check-cloud-ready/scripts/resolve_granule.py
A separate test (test_copies_are_identical) asserts the
earth-science-cloud-readiness copy is byte-identical to it, so testing
one copy covers both.

Live network check (not run by default):
    RUN_LIVE=1 python3 -m unittest tests.test_resolve_granule -k live
"""
import importlib.util
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHECK_CLOUD_READY_PATH = os.path.join(
    REPO_ROOT, ".claude", "skills", "check-cloud-ready", "scripts", "resolve_granule.py")
EARTH_SCIENCE_PATH = os.path.join(
    REPO_ROOT, ".claude", "skills", "earth-science-cloud-readiness", "scripts",
    "resolve_granule.py")

_spec = importlib.util.spec_from_file_location("resolve_granule", CHECK_CLOUD_READY_PATH)
resolve_granule = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(resolve_granule)

parse_related_urls = resolve_granule.parse_related_urls
resolve = resolve_granule.resolve


class TestParseRelatedUrls(unittest.TestCase):
    def test_happy_path(self):
        umm = {
            "RelatedUrls": [
                {
                    "URL": "https://nisar.asf.earthdatacloud.nasa.gov/s3credentials",
                    "Type": "VIEW RELATED INFORMATION",
                    "Description": "S3 credentials endpoint for direct in-region bucket access",
                },
                {
                    "URL": "s3://sds-n-cumulus-prod-nisar-products/foo/bar1.h5",
                    "Type": "GET DATA VIA DIRECT ACCESS",
                },
                {
                    "URL": "s3://sds-n-cumulus-prod-nisar-products/foo/bar2.h5",
                    "Type": "GET DATA VIA DIRECT ACCESS",
                },
                {
                    "URL": "https://nisar.asf.earthdatacloud.nasa.gov/foo/bar1.h5",
                    "Type": "GET DATA",
                },
            ]
        }
        result = parse_related_urls(umm)
        self.assertEqual(
            result["credentials_url"],
            "https://nisar.asf.earthdatacloud.nasa.gov/s3credentials",
        )
        self.assertEqual(result["credentials_url_type"], "VIEW RELATED INFORMATION")
        self.assertEqual(
            result["credentials_url_description"],
            "S3 credentials endpoint for direct in-region bucket access",
        )
        self.assertEqual(
            result["s3_urls"],
            [
                "s3://sds-n-cumulus-prod-nisar-products/foo/bar1.h5",
                "s3://sds-n-cumulus-prod-nisar-products/foo/bar2.h5",
            ],
        )
        self.assertEqual(
            result["https_urls"],
            ["https://nisar.asf.earthdatacloud.nasa.gov/foo/bar1.h5"],
        )
        self.assertIn("https_urls are for reference only", result["note"])

    def test_no_credentials_entry(self):
        umm = {
            "RelatedUrls": [
                {
                    "URL": "s3://some-bucket/foo/bar1.h5",
                    "Type": "GET DATA VIA DIRECT ACCESS",
                },
            ]
        }
        result = parse_related_urls(umm)
        self.assertIsNone(result["credentials_url"])
        self.assertEqual(result["s3_urls"], ["s3://some-bucket/foo/bar1.h5"])

    def test_trailing_slash_credentials_url(self):
        umm = {
            "RelatedUrls": [
                {
                    "URL": "https://example.earthdatacloud.nasa.gov/s3credentials/",
                    "Type": "VIEW RELATED INFORMATION",
                    "Description": "S3 credentials endpoint",
                },
            ]
        }
        result = parse_related_urls(umm)
        self.assertEqual(
            result["credentials_url"],
            "https://example.earthdatacloud.nasa.gov/s3credentials/",
        )

    def test_missing_related_urls(self):
        result = parse_related_urls({})
        self.assertIsNone(result["credentials_url"])
        self.assertEqual(result["s3_urls"], [])
        self.assertEqual(result["https_urls"], [])

        result_empty = parse_related_urls({"RelatedUrls": []})
        self.assertIsNone(result_empty["credentials_url"])
        self.assertEqual(result_empty["s3_urls"], [])
        self.assertEqual(result_empty["https_urls"], [])


class TestCopiesIdentical(unittest.TestCase):
    def test_copies_are_identical(self):
        with open(CHECK_CLOUD_READY_PATH, "rb") as f:
            check_cloud_ready_bytes = f.read()
        with open(EARTH_SCIENCE_PATH, "rb") as f:
            earth_science_bytes = f.read()
        self.assertEqual(check_cloud_ready_bytes, earth_science_bytes)


class TestResolveValidation(unittest.TestCase):
    def test_invalid_granule_id_exits(self):
        with self.assertRaises(SystemExit):
            resolve("not-a-valid-id")


@unittest.skipUnless(os.environ.get("RUN_LIVE") == "1", "set RUN_LIVE=1 to run live CMR check")
class TestResolveLive(unittest.TestCase):
    def test_live_nisar_granule(self):
        result = resolve("G4289749526-ASF")
        self.assertEqual(
            result["credentials_url"],
            "https://nisar.asf.earthdatacloud.nasa.gov/s3credentials",
        )
        self.assertTrue(
            any(u.startswith("s3://sds-n-cumulus-prod-nisar-products/") for u in result["s3_urls"])
        )


if __name__ == "__main__":
    unittest.main()
