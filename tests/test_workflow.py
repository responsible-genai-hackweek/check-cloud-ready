"""Offline tests for check_cloud_ready.access.workflow.

Every test mocks probe.probe_https/probe_s3, nasa_s3.get_fs, and
resolve_granule.resolve directly (patched on the shared module
objects), plus a FakePrompter that records every call. No real
network I/O, no real boto3/s3fs/obstore/earthaccess backend is ever
constructed.
"""
import importlib
import sys
import unittest
from pathlib import Path
from unittest import mock

from check_cloud_ready.access import nasa_s3 as nasa_s3_module
from check_cloud_ready.access import probe as probe_module
from check_cloud_ready.access import resolve_granule as resolve_granule_module
from check_cloud_ready.access import workflow


class FakePrompter:
    """Records every call; returns queued answers/choices/confirms in
    order, falling back to the caller-supplied default once the queue
    is exhausted.
    """

    def __init__(self, *, confirms=None, choices=None, answers=None):
        self._confirms = list(confirms or [])
        self._choices = list(choices or [])
        self._answers = list(answers or [])
        self.calls = []

    def ask(self, text, default=None):
        self.calls.append(("ask", text))
        return self._answers.pop(0) if self._answers else default

    def choose(self, text, options, default=None):
        self.calls.append(("choose", text, tuple(options)))
        return self._choices.pop(0) if self._choices else default

    def confirm(self, text, default=False):
        self.calls.append(("confirm", text))
        return self._confirms.pop(0) if self._confirms else default


class TestLocalPath(unittest.TestCase):
    def test_local_path_is_auth_local_with_hosting_finding_and_no_prompts(self):
        prompter = FakePrompter()
        result = workflow.resolve_access("/data/local/file.nc", prompter=prompter)

        self.assertEqual(result.auth, "local")
        self.assertIsNone(result.fs)
        self.assertTrue(any(f["id"] == "D0-hosting" for f in result.findings))
        self.assertEqual(prompter.calls, [])


class TestNoNetwork(unittest.TestCase):
    @mock.patch.object(probe_module, "probe_https",
                       side_effect=AssertionError("must not probe when no_network=True"))
    def test_no_network_skips_probes_entirely(self, _mock_probe):
        prompter = FakePrompter()
        result = workflow.resolve_access(
            "https://example.com/x.nc", prompter=prompter, no_network=True)

        self.assertIsNone(result.fs)
        self.assertTrue(any(f["status"] == "skipped" for f in result.findings))
        self.assertEqual(prompter.calls, [])

    @mock.patch.object(probe_module, "probe_https",
                       side_effect=AssertionError("must not probe when no_network=True"))
    def test_no_network_url_input_is_auth_no_network_not_local(self, _mock_probe):
        # Finding 1: a remote (non-local) input under no_network=True must
        # not be mislabeled auth="local" -- it was never a local path, it
        # was a URL that we were told not to touch over the network.
        prompter = FakePrompter()
        result = workflow.resolve_access(
            "https://example.com/x.nc", prompter=prompter, no_network=True)

        self.assertEqual(result.auth, "no-network")

    @mock.patch.object(resolve_granule_module, "resolve",
                       side_effect=AssertionError("must not resolve when no_network=True"))
    def test_no_network_granule_id_input_is_auth_no_network(self, _mock_resolve):
        prompter = FakePrompter()
        result = workflow.resolve_access(
            "G123-ASF", prompter=prompter, no_network=True)

        self.assertEqual(result.auth, "no-network")
        self.assertIsNone(result.fs)

    def test_local_path_under_no_network_is_still_auth_local(self):
        # A genuinely local path stays "local" even when no_network=True is
        # also passed -- "local" and "no-network" describe different
        # reasons for fs=None and must not collapse into one another.
        prompter = FakePrompter()
        result = workflow.resolve_access(
            "/data/local/file.nc", prompter=prompter, no_network=True)

        self.assertEqual(result.auth, "local")


class TestPublicHttps(unittest.TestCase):
    @mock.patch.object(probe_module, "probe_https")
    def test_public_https_is_anonymous_with_no_prompts(self, mock_probe_https):
        mock_probe_https.return_value = {
            "scheme": "https", "url": "https://example.com/x.tif",
            "classification": "ok", "range_classification": "ok",
        }
        prompter = FakePrompter()

        result = workflow.resolve_access("https://example.com/x.tif", prompter=prompter)

        self.assertEqual(result.auth, "anonymous")
        self.assertEqual(prompter.calls, [])


class TestPublicS3(unittest.TestCase):
    @mock.patch.object(nasa_s3_module, "get_fs")
    @mock.patch.object(probe_module, "probe_s3")
    def test_public_s3_anon_probe_ok_yields_anon_fs(self, mock_probe_s3, mock_get_fs):
        mock_probe_s3.return_value = {
            "scheme": "s3", "url": "s3://bucket/key", "classification": "ok",
        }
        mock_get_fs.return_value = ("FAKE_FS", "s3://bucket/key")
        prompter = FakePrompter()

        result = workflow.resolve_access("s3://bucket/key", prompter=prompter)

        self.assertEqual(result.auth, "anonymous")
        self.assertEqual(result.fs, "FAKE_FS")
        mock_get_fs.assert_called_once_with("s3://bucket/key", anon=True)
        self.assertEqual(prompter.calls, [])


class TestEtagThreading(unittest.TestCase):
    """Final-review finding 3: a live probe's captured ETag (when the
    server sent one) must be threaded through onto AccessResult.etag so
    the CLI orchestrator can populate scoring.py's asset["etag"] --
    before this fix, nothing ever set it, so E1-version/E2-checksums
    were permanently fail/partial for every non-Icechunk asset,
    regardless of what the endpoint actually served.
    """

    @mock.patch.object(probe_module, "probe_https")
    def test_public_https_with_etag_is_threaded_onto_access_result(self, mock_probe_https):
        mock_probe_https.return_value = {
            "scheme": "https", "url": "https://example.com/x.tif",
            "classification": "ok", "range_classification": "ok",
            "etag": '"abc123"',
        }
        prompter = FakePrompter()

        result = workflow.resolve_access("https://example.com/x.tif", prompter=prompter)

        self.assertEqual(result.etag, '"abc123"')

    @mock.patch.object(probe_module, "probe_https")
    def test_public_https_without_etag_leaves_it_none(self, mock_probe_https):
        mock_probe_https.return_value = {
            "scheme": "https", "url": "https://example.com/x.tif",
            "classification": "ok", "range_classification": "ok",
            "etag": None,
        }
        prompter = FakePrompter()

        result = workflow.resolve_access("https://example.com/x.tif", prompter=prompter)

        self.assertIsNone(result.etag)

    @mock.patch.object(nasa_s3_module, "get_fs")
    @mock.patch.object(probe_module, "probe_s3")
    def test_public_s3_with_etag_is_threaded_onto_access_result(
            self, mock_probe_s3, mock_get_fs):
        mock_probe_s3.return_value = {
            "scheme": "s3", "url": "s3://bucket/key", "classification": "ok",
            "etag": '"def456"',
        }
        mock_get_fs.return_value = ("FAKE_FS", "s3://bucket/key")
        prompter = FakePrompter()

        result = workflow.resolve_access("s3://bucket/key", prompter=prompter)

        self.assertEqual(result.etag, '"def456"')

    def test_local_path_has_no_etag(self):
        result = workflow.resolve_access("/data/local/file.nc", prompter=FakePrompter())
        self.assertIsNone(result.etag)


class TestGranuleIdInput(unittest.TestCase):
    @mock.patch.object(nasa_s3_module, "get_fs")
    @mock.patch.object(resolve_granule_module, "resolve")
    def test_granule_id_input_resolves_via_cmr_and_builds_fs(self, mock_resolve, mock_get_fs):
        mock_resolve.return_value = {
            "granule_id": "G123-ASF",
            "credentials_url": "https://nisar.asf.earthdatacloud.nasa.gov/s3credentials",
            "s3_urls": ["s3://bucket/key.h5"],
            "https_urls": [],
        }
        mock_get_fs.return_value = ("FAKE_FS", "s3://bucket/key.h5")
        prompter = FakePrompter()

        result = workflow.resolve_access("G123-ASF", prompter=prompter)

        mock_resolve.assert_called_once_with("G123-ASF")
        mock_get_fs.assert_called_once_with(
            "s3://bucket/key.h5",
            credentials_url="https://nisar.asf.earthdatacloud.nasa.gov/s3credentials",
            earthaccess_fallback=False)
        self.assertEqual(result.auth, "obstore-cmr")
        self.assertEqual(result.fs, "FAKE_FS")
        self.assertEqual(prompter.calls, [])

    @mock.patch.object(nasa_s3_module, "get_fs")
    @mock.patch.object(resolve_granule_module, "resolve")
    def test_multiple_s3_urls_prompts_choose(self, mock_resolve, mock_get_fs):
        mock_resolve.return_value = {
            "granule_id": "G123-ASF",
            "credentials_url": "https://x.earthdata.nasa.gov/s3credentials",
            "s3_urls": ["s3://bucket/a.h5", "s3://bucket/b.h5"],
            "https_urls": [],
        }
        mock_get_fs.return_value = ("FAKE_FS", "s3://bucket/b.h5")
        prompter = FakePrompter(choices=["s3://bucket/b.h5"])

        result = workflow.resolve_access("G123-ASF", prompter=prompter)

        self.assertTrue(any(c[0] == "choose" for c in prompter.calls))
        mock_get_fs.assert_called_once_with(
            "s3://bucket/b.h5",
            credentials_url="https://x.earthdata.nasa.gov/s3credentials",
            earthaccess_fallback=False)
        self.assertEqual(result.fs, "FAKE_FS")

    @mock.patch.object(resolve_granule_module, "resolve")
    def test_granule_resolution_error_becomes_friendly_finding_not_traceback(self, mock_resolve):
        mock_resolve.side_effect = resolve_granule_module.GranuleResolutionError(
            "Invalid granule concept ID: 'G1-BADPROVIDER'")
        prompter = FakePrompter()

        result = workflow.resolve_access("G1-BADPROVIDER", prompter=prompter)

        self.assertIsNone(result.fs)
        self.assertTrue(any("Invalid granule concept ID" in f["evidence"]
                             for f in result.findings))
        self.assertEqual(prompter.calls, [])


class TestProtectedS3(unittest.TestCase):
    @mock.patch.object(nasa_s3_module, "get_fs")
    @mock.patch.object(resolve_granule_module, "resolve")
    @mock.patch.object(probe_module, "probe_s3")
    def test_protected_s3_user_provides_granule_id_via_prompt(
            self, mock_probe_s3, mock_resolve, mock_get_fs):
        mock_probe_s3.return_value = {
            "scheme": "s3", "url": "s3://podaac-ops-cumulus-protected/granule.h5", "classification": "auth-required",
        }
        mock_resolve.return_value = {
            "granule_id": "G999-ASF",
            "credentials_url": "https://x.earthdata.nasa.gov/s3credentials",
            "s3_urls": ["s3://podaac-ops-cumulus-protected/granule.h5"],
            "https_urls": [],
        }
        mock_get_fs.return_value = ("FAKE_FS", "s3://podaac-ops-cumulus-protected/granule.h5")
        prompter = FakePrompter(confirms=[True], answers=["G999-ASF"])

        result = workflow.resolve_access("s3://podaac-ops-cumulus-protected/granule.h5", prompter=prompter)

        mock_resolve.assert_called_once_with("G999-ASF")
        self.assertEqual(result.auth, "obstore-cmr")
        self.assertEqual(result.fs, "FAKE_FS")

    @mock.patch.object(nasa_s3_module, "get_fs")
    @mock.patch.object(probe_module, "probe_s3")
    def test_protected_s3_no_granule_id_uses_endpoint_whitelist_choose(
            self, mock_probe_s3, mock_get_fs):
        mock_probe_s3.return_value = {
            "scheme": "s3", "url": "s3://podaac-ops-cumulus-protected/granule.h5", "classification": "auth-required",
        }
        mock_get_fs.return_value = ("FAKE_FS", "s3://podaac-ops-cumulus-protected/granule.h5")
        chosen_label = next(iter(nasa_s3_module.KNOWN_CREDENTIALS_ENDPOINTS))
        prompter = FakePrompter(confirms=[False], choices=[chosen_label])

        result = workflow.resolve_access("s3://podaac-ops-cumulus-protected/granule.h5", prompter=prompter)

        self.assertTrue(any(c[0] == "choose" for c in prompter.calls))
        expected_endpoint = nasa_s3_module.KNOWN_CREDENTIALS_ENDPOINTS[chosen_label]
        mock_get_fs.assert_called_once_with(
            "s3://podaac-ops-cumulus-protected/granule.h5", credentials_url=expected_endpoint)
        self.assertEqual(result.auth, "obstore-endpoint")
        self.assertEqual(result.fs, "FAKE_FS")

    @mock.patch.object(nasa_s3_module, "get_fs")
    @mock.patch.object(probe_module, "probe_s3")
    def test_protected_s3_obstore_fails_user_confirms_earthaccess(
            self, mock_probe_s3, mock_get_fs):
        mock_probe_s3.return_value = {
            "scheme": "s3", "url": "s3://podaac-ops-cumulus-protected/granule.h5", "classification": "auth-required",
        }
        chosen_label = next(iter(nasa_s3_module.KNOWN_CREDENTIALS_ENDPOINTS))

        def get_fs_side_effect(url, **kwargs):
            if kwargs.get("credentials_url"):
                raise RuntimeError("obstore is required for credentials-endpoint auth")
            if kwargs.get("earthaccess_fallback"):
                return ("EA_FS", url)
            raise AssertionError(f"unexpected get_fs kwargs: {kwargs!r}")

        mock_get_fs.side_effect = get_fs_side_effect
        prompter = FakePrompter(confirms=[False, True], choices=[chosen_label])

        result = workflow.resolve_access("s3://podaac-ops-cumulus-protected/granule.h5", prompter=prompter)

        self.assertEqual(result.auth, "earthaccess")
        self.assertEqual(result.fs, "EA_FS")
        self.assertEqual(mock_get_fs.call_count, 2)

    @mock.patch.object(nasa_s3_module, "get_fs")
    @mock.patch.object(probe_module, "probe_s3")
    def test_in_region_only_error_is_skipped_finding_mentioning_us_west_2(
            self, mock_probe_s3, mock_get_fs):
        mock_probe_s3.return_value = {
            "scheme": "s3", "url": "s3://podaac-ops-cumulus-protected/granule.h5", "classification": "auth-required",
        }
        mock_get_fs.side_effect = RuntimeError(
            "An error occurred (AccessDenied) when calling GetObject: Forbidden")
        # Supplying credentials_url up front skips the granule/endpoint
        # prompts and goes straight to the failing get_fs call; decline
        # the earthaccess fallback afterwards.
        prompter = FakePrompter(confirms=[False])

        result = workflow.resolve_access(
            "s3://podaac-ops-cumulus-protected/granule.h5", prompter=prompter,
            credentials_url="https://example.daac.earthdata.nasa.gov/s3credentials")

        region_findings = [f for f in result.findings if "us-west-2" in f["evidence"]]
        self.assertTrue(region_findings, "expected a finding mentioning us-west-2")
        self.assertEqual(region_findings[0]["status"], "skipped")
        self.assertIsNone(result.fs)
        # The invariant, exercised on a failure path too: url/path must
        # still be the original s3:// URL, never rewritten to https.
        self.assertTrue(result.url.startswith("s3://"))
        self.assertTrue(result.path.startswith("s3://"))

    @mock.patch.object(nasa_s3_module, "get_fs")
    @mock.patch.object(probe_module, "probe_s3")
    def test_credentials_endpoint_auth_error_is_finding_not_crash(
            self, mock_probe_s3, mock_get_fs):
        mock_probe_s3.return_value = {
            "scheme": "s3", "url": "s3://podaac-ops-cumulus-protected/granule.h5", "classification": "auth-required",
        }
        mock_get_fs.side_effect = RuntimeError(
            "401 Unauthorized from https://x.earthdata.nasa.gov/s3credentials")
        prompter = FakePrompter(confirms=[False])

        result = workflow.resolve_access(
            "s3://podaac-ops-cumulus-protected/granule.h5", prompter=prompter,
            credentials_url="https://x.earthdata.nasa.gov/s3credentials")

        self.assertTrue(any("Earthdata Login credentials" in f["evidence"]
                             for f in result.findings))
        self.assertIsNone(result.fs)


class TestNasaHeuristicGating(unittest.TestCase):
    """Finding 3: the NASA-specific credential flow (granule ID / DAAC
    whitelist / earthaccess) must be gated on
    nasa_s3.looks_like_nasa_earthdata(url); a non-NASA-looking protected
    bucket should get one confirm question, not the whole NASA UX.
    """

    NON_NASA_URL = "s3://acme-corp-private-bucket/data.h5"

    def setUp(self):
        # Sanity-check the fixture itself matches no NASA marker, so a
        # future edit to the marker list can't silently make this test
        # meaningless.
        self.assertFalse(nasa_s3_module.looks_like_nasa_earthdata(self.NON_NASA_URL))

    @mock.patch.object(probe_module, "probe_s3")
    def test_non_nasa_protected_bucket_confirm_no_yields_generic_finding_no_nasa_prompts(
            self, mock_probe_s3):
        mock_probe_s3.return_value = {
            "scheme": "s3", "url": self.NON_NASA_URL, "classification": "auth-required",
            "anonymous": {
                "error": "AccessDenied",
                "hint": "403 from S3 can mean: credentials required, requester-pays, ...",
            },
        }
        # Only one confirm queued: "is this NASA Earthdata data?" -> No.
        # If any NASA-specific prompt fired afterwards, the FakePrompter
        # would fall through to its default (False) rather than raising,
        # so the real assertion is on prompter.calls below, not on the
        # result alone.
        prompter = FakePrompter(confirms=[False])

        result = workflow.resolve_access(self.NON_NASA_URL, prompter=prompter)

        self.assertEqual(result.auth, "error")
        self.assertIsNone(result.fs)
        self.assertTrue(any("403 from S3 can mean" in f["evidence"] for f in result.findings))

        # Exactly one confirm call (the NASA-or-not question); no
        # granule-ID ask/confirm, no DAAC-endpoint choose, no earthaccess
        # confirm.
        confirm_texts = [c[1] for c in prompter.calls if c[0] == "confirm"]
        self.assertEqual(len(confirm_texts), 1)
        self.assertIn("NASA Earthdata", confirm_texts[0])
        self.assertFalse(any(c[0] == "choose" for c in prompter.calls))
        self.assertFalse(any(c[0] == "ask" for c in prompter.calls))

    @mock.patch.object(nasa_s3_module, "get_fs")
    @mock.patch.object(probe_module, "probe_s3")
    def test_non_nasa_protected_bucket_confirm_yes_proceeds_with_nasa_flow(
            self, mock_probe_s3, mock_get_fs):
        mock_probe_s3.return_value = {
            "scheme": "s3", "url": self.NON_NASA_URL, "classification": "auth-required",
        }
        mock_get_fs.return_value = ("FAKE_FS", self.NON_NASA_URL)
        chosen_label = next(iter(nasa_s3_module.KNOWN_CREDENTIALS_ENDPOINTS))
        # confirms: [0] "is this NASA data?" -> Yes, [1] "do you have a
        # granule ID?" -> No (falls through to the DAAC-endpoint choose).
        prompter = FakePrompter(confirms=[True, False], choices=[chosen_label])

        result = workflow.resolve_access(self.NON_NASA_URL, prompter=prompter)

        self.assertTrue(any(c[0] == "choose" for c in prompter.calls))
        self.assertEqual(result.auth, "obstore-endpoint")
        self.assertEqual(result.fs, "FAKE_FS")

    @mock.patch.object(nasa_s3_module, "get_fs")
    @mock.patch.object(probe_module, "probe_s3")
    def test_nasa_looking_bucket_skips_the_nasa_confirm_question(
            self, mock_probe_s3, mock_get_fs):
        mock_probe_s3.return_value = {
            "scheme": "s3", "url": "s3://podaac-ops-cumulus-protected/granule.h5",
            "classification": "auth-required",
        }
        mock_get_fs.return_value = ("FAKE_FS", "s3://podaac-ops-cumulus-protected/granule.h5")
        # No confirms queued at all: if the code asked "is this NASA
        # data?" it would fall through to the default (False) and derail
        # into the generic-bucket path instead of the granule-ID prompt;
        # asserting auth="error" here would be a false negative for a
        # bug where the gate fires even though the heuristic is
        # positive, so instead assert directly on what was asked.
        prompter = FakePrompter()

        workflow.resolve_access(
            "s3://podaac-ops-cumulus-protected/granule.h5", prompter=prompter)

        confirm_texts = [c[1] for c in prompter.calls if c[0] == "confirm"]
        self.assertNotIn(
            "This S3 bucket doesn't match known NASA Earthdata Cloud "
            "naming patterns -- is this NASA Earthdata data?",
            confirm_texts)


class TestProtectedHttps(unittest.TestCase):
    @mock.patch.object(nasa_s3_module, "edl_bearer_token", return_value="tok")
    @mock.patch.object(probe_module, "probe_https")
    def test_token_available_builds_https_fs_with_bearer_header(
            self, mock_probe_https, _mock_token):
        mock_probe_https.return_value = {
            "scheme": "https", "url": "https://protected.example/x.nc",
            "classification": "auth-required",
        }
        fake_fs = mock.Mock(name="fake_https_fs")
        prompter = FakePrompter()

        with mock.patch("fsspec.filesystem", return_value=fake_fs) as mock_fsspec:
            result = workflow.resolve_access(
                "https://protected.example/x.nc", prompter=prompter)

        self.assertEqual(result.auth, "edl-bearer")
        self.assertIs(result.fs, fake_fs)
        _, kwargs = mock_fsspec.call_args
        self.assertEqual(
            kwargs["client_kwargs"]["headers"]["Authorization"], "Bearer tok")
        self.assertEqual(prompter.calls, [])

    @mock.patch.object(nasa_s3_module, "edl_bearer_token", return_value="tok")
    @mock.patch.object(probe_module, "probe_https")
    def test_fsspec_build_failure_with_token_is_skipped_finding_not_silent_none(
            self, mock_probe_https, _mock_token):
        # e.g. aiohttp not installed (the real state of this dev venv):
        # fsspec.filesystem("https", ...) raises. fs=None must come with an
        # explanatory finding, not silently.
        mock_probe_https.return_value = {
            "scheme": "https", "url": "https://protected.example/x.nc",
            "classification": "auth-required",
        }
        prompter = FakePrompter()

        with mock.patch("fsspec.filesystem", side_effect=ImportError("no aiohttp")):
            result = workflow.resolve_access(
                "https://protected.example/x.nc", prompter=prompter)

        self.assertIsNone(result.fs)
        self.assertTrue(any(
            "could not be constructed" in f["evidence"] for f in result.findings))

    @mock.patch.object(nasa_s3_module, "edl_bearer_token", return_value=None)
    @mock.patch.object(probe_module, "probe_https")
    def test_no_token_is_skipped_finding_never_prompts_for_password(
            self, mock_probe_https, _mock_token):
        mock_probe_https.return_value = {
            "scheme": "https", "url": "https://protected.example/x.nc",
            "classification": "auth-required",
        }
        prompter = FakePrompter()

        result = workflow.resolve_access(
            "https://protected.example/x.nc", prompter=prompter)

        self.assertIsNone(result.fs)
        self.assertEqual(result.auth, "error")
        self.assertTrue(any(
            f["remediation"] and "EARTHDATA_TOKEN" in f["remediation"]
            for f in result.findings))
        self.assertEqual(prompter.calls, [])


class TestNeverS3ToHttpsInvariant(unittest.TestCase):
    def test_source_has_no_to_https_rewrite_helper(self):
        source = Path(workflow.__file__).read_text()
        self.assertNotIn("_to_https", source)
        self.assertNotIn('replace("s3://"', source)
        self.assertNotIn("replace('s3://'", source)

    @mock.patch.object(nasa_s3_module, "get_fs")
    @mock.patch.object(probe_module, "probe_s3")
    def test_s3_input_never_yields_an_https_url_or_path(self, mock_probe_s3, mock_get_fs):
        mock_probe_s3.return_value = {
            "scheme": "s3", "url": "s3://bucket/key", "classification": "ok",
        }
        mock_get_fs.return_value = ("FAKE_FS", "s3://bucket/key")
        prompter = FakePrompter()

        result = workflow.resolve_access("s3://bucket/key", prompter=prompter)

        self.assertTrue(result.url.startswith("s3://"))
        self.assertTrue(result.path.startswith("s3://"))
        self.assertNotIn("https://", result.url)
        self.assertNotIn("https://", result.path)


class TestAccessResultInvariants(unittest.TestCase):
    """Controller-ruling invariants on AccessResult:
    (a) fs is None => auth in {"local", "no-network", "error"}.
    (b) auth == "error" is always paired with a fail/skipped finding.
    Exercised across every terminal (fs=None) path this module has.
    """

    def _assert_invariants(self, result):
        if result.fs is None:
            self.assertIn(result.auth, {"local", "no-network", "error"})
        if result.auth == "error":
            self.assertIsNone(result.fs)
            self.assertTrue(
                any(f["status"] in ("fail", "skipped") for f in result.findings),
                "auth='error' must be paired with a fail/skipped finding")

    def test_local_path(self):
        result = workflow.resolve_access("/data/local/file.nc", prompter=FakePrompter())
        self._assert_invariants(result)
        self.assertEqual(result.auth, "local")

    def test_no_network_url(self):
        with mock.patch.object(probe_module, "probe_https",
                                side_effect=AssertionError("no probing")):
            result = workflow.resolve_access(
                "https://example.com/x.nc", prompter=FakePrompter(), no_network=True)
        self._assert_invariants(result)
        self.assertEqual(result.auth, "no-network")

    @mock.patch.object(resolve_granule_module, "resolve")
    def test_granule_resolution_failure(self, mock_resolve):
        mock_resolve.side_effect = resolve_granule_module.GranuleResolutionError("bad id")
        result = workflow.resolve_access("G1-BADPROVIDER", prompter=FakePrompter())
        self._assert_invariants(result)
        self.assertEqual(result.auth, "error")

    @mock.patch.object(probe_module, "probe_s3")
    def test_no_credential_path_found(self, mock_probe_s3):
        url = "s3://acme-corp-private-bucket/data.h5"
        mock_probe_s3.return_value = {
            "scheme": "s3", "url": url, "classification": "auth-required",
        }
        # confirm-no to "is this NASA data?" -> generic protected-bucket
        # terminal failure, no credential path at all.
        result = workflow.resolve_access(
            url, prompter=FakePrompter(confirms=[False]))
        self._assert_invariants(result)
        self.assertEqual(result.auth, "error")

    @mock.patch.object(nasa_s3_module, "edl_bearer_token", return_value="tok")
    @mock.patch.object(probe_module, "probe_https")
    def test_https_fs_build_failure(self, mock_probe_https, _mock_token):
        mock_probe_https.return_value = {
            "scheme": "https", "url": "https://protected.example/x.nc",
            "classification": "auth-required",
        }
        with mock.patch("fsspec.filesystem", side_effect=ImportError("no aiohttp")):
            result = workflow.resolve_access(
                "https://protected.example/x.nc", prompter=FakePrompter())
        self._assert_invariants(result)
        # Finding 2: a token was available and auth was determined to be
        # edl-bearer in spirit, but with no usable fs this must not
        # report auth="edl-bearer" -- that would violate invariant (a).
        self.assertEqual(result.auth, "error")

    @mock.patch.object(probe_module, "probe_https")
    def test_anonymous_https_fs_build_failure(self, mock_probe_https):
        mock_probe_https.return_value = {
            "scheme": "https", "url": "https://example.com/x.tif",
            "classification": "ok",
        }
        with mock.patch("fsspec.filesystem", side_effect=ImportError("no aiohttp")):
            result = workflow.resolve_access(
                "https://example.com/x.tif", prompter=FakePrompter())
        self._assert_invariants(result)
        self.assertEqual(result.auth, "error")


class TestModuleImportSafety(unittest.TestCase):
    def test_workflow_has_no_module_level_optional_backend_import(self):
        source = Path(workflow.__file__).read_text()
        forbidden_prefixes = (
            "import boto3", "import s3fs", "import obstore", "import earthaccess",
            "from boto3", "from s3fs", "from obstore", "from earthaccess",
        )
        for line in source.splitlines():
            for needle in forbidden_prefixes:
                self.assertFalse(
                    line.startswith(needle),
                    f"module-level import of an optional backend: {line!r}")

    def test_workflow_module_already_imported_without_boto3_s3fs_earthaccess(self):
        # This dev environment genuinely lacks boto3/s3fs/earthaccess (see
        # test_nasa_s3.py / test_probe.py). The fact that
        # check_cloud_ready.access.workflow imported cleanly at the top of
        # this file, in this same environment, is itself the evidence that
        # module import does not require any of them.
        for name in ("boto3", "s3fs", "earthaccess"):
            self.assertNotIn(
                name, sys.modules,
                f"{name} unexpectedly present in sys.modules in this dev venv "
                "-- import-safety evidence above may be invalid")


if __name__ == "__main__":
    unittest.main()
