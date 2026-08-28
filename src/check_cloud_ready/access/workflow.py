"""Access-resolution workflow: get from a user-supplied input (a granule
ID, an ``s3://``/``https://`` URL, or a local path) to an open,
authenticated handle on the data.

Encodes the canonical decision flow documented in ``auth-workflow.md``
(the design doc; see that file for the full narrative and rationale).
In short:

1. Classify the input (``formats.detect_input``). A granule ID is
   resolved via CMR first; a local path is not network-assessable at
   all; a URL goes to step 2.
2. Probe anonymously first (``access.probe``). Public data needs no
   credential setup.
3. Protected ``s3://``: NEVER fall back to HTTPS (see the invariant
   note below). Prefer a CMR granule ID (round-trips to step 1); else a
   DAAC ``s3credentials`` endpoint (offered from a whitelist); else
   earthaccess, if the caller opts in.
4. Protected ``https://``: mint an Earthdata Login bearer token and use
   an ``Authorization: Bearer`` header. Never prompt for a password
   in-band; if no token is available (no ``EARTHDATA_TOKEN``, no
   ``EARTHDATA_USERNAME``/``EARTHDATA_PASSWORD``, no ``~/.netrc``
   entry), record a finding with setup guidance and stop.
5. ``no_network=True`` skips every probe/resolution step.

Invariant: no code path in this module may rewrite an ``s3://`` URL
into ``https://`` to route around a protected S3 bucket. NASA Earthdata
Cloud protected buckets are only correctly readable via S3 (in-region,
us-west-2) with minted credentials; an HTTPS fallback silently defeats
the point of an in-region cloud read (see ``access/nasa_s3.py``'s own
module docstring) and would also just be wrong for any other protected
bucket. There is deliberately no scheme-rewriting helper anywhere in
this file.

This module only imports core dependencies at module scope (``fsspec``,
plus the sibling ``formats``/``access.nasa_s3``/``access.resolve_granule``/
``access.probe`` modules, none of which hard-import boto3/s3fs/obstore/
earthaccess at *their* module scope either -- they soft-import inside
functions). It never constructs an authenticated backend itself; that
work is delegated to ``access.nasa_s3.get_fs`` and to ``fsspec``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import fsspec

from .. import formats
from . import nasa_s3
from . import probe
from . import resolve_granule
from .resolve_granule import GranuleResolutionError

__all__ = ["AccessResult", "resolve_access", "GranuleResolutionError"]


@dataclass
class AccessResult:
    fs: Any            # fsspec-like filesystem, or None for plain-local
    path: str          # path usable with fs (or local path)
    url: str           # original/canonical URL
    findings: list      # check-dicts (Dimension D evidence)
    auth: str          # "anonymous" | "obstore-cmr" | "obstore-endpoint" |
                        # "earthaccess" | "edl-bearer" | "local" | "error"
    notes: list = field(default_factory=list)


# --------------------------------------------------------------- findings

def _finding(id_, dim, status, evidence, remediation=None, pts=0.0, awarded=0.0):
    """Build one check-dict (Ruling I-4 shape). Scoring (Task 10) owns
    pts/awarded; this module always leaves them at 0.
    """
    return {
        "id": id_, "dim": dim, "status": status, "evidence": evidence,
        "remediation": remediation, "pts": pts, "awarded": awarded,
    }


_RANGE_STATUS_BY_CLASSIFICATION = {
    "ok": "pass",
    "no-range-support": "warn",
    "auth-required": "skipped",
    "not-assessable": "skipped",
}


def _range_finding(probe_result):
    """Turn a probe.py result dict into a D1 (range-support) finding."""
    scheme = probe_result.get("scheme", "?")
    if "skipped" in probe_result:
        return _finding("D1-range", "D", "skipped",
                         f"{scheme} probe skipped: {probe_result['skipped']}")

    classification = probe_result.get("classification", "unknown")
    status = _RANGE_STATUS_BY_CLASSIFICATION.get(classification, "skipped")
    remediation = None
    if classification == "ok":
        evidence = f"{scheme} endpoint supports ranged reads (probe: ok)."
    elif classification == "no-range-support":
        evidence = (f"{scheme} endpoint responded but ignored the Range "
                    "header (probe: no-range-support).")
        remediation = "Host data behind a server/proxy that honors HTTP Range requests."
    else:
        evidence = (f"{scheme} endpoint requires authentication before range "
                    "support could be verified (probe: auth-required).")
    return _finding("D1-range", "D", status, evidence, remediation)


def _append_s3_error_finding(findings, exc):
    """Classify an S3/auth exception (via nasa_s3.classify_s3_error) and
    append the corresponding D2 (auth) finding -- never let it surface
    as a raw traceback.
    """
    kind = nasa_s3.classify_s3_error(exc)
    if kind == "credentials-endpoint-auth":
        evidence = ("Earthdata Login credentials are missing, expired, or "
                    "lack access to this DAAC's s3credentials endpoint.")
        remediation = ("Check EARTHDATA_TOKEN / EARTHDATA_USERNAME+PASSWORD / "
                        "~/.netrc.")
    elif kind == "in-region-only":
        evidence = ("S3 credentials were minted successfully but access was "
                    "denied at the bucket/object level -- this almost always "
                    "means the request is not running inside AWS us-west-2.")
        remediation = "Re-run from in-region (us-west-2) compute."
    else:
        evidence = f"S3 access failed: {exc}"
        remediation = None
    findings.append(_finding("D2-auth", "D", "skipped", evidence, remediation))


# ---------------------------------------------------------------- top level

def resolve_access(input_str: str, *, prompter, anon=False, granule_id=None,
                    credentials_url=None, earthaccess_fallback=False,
                    no_network=False) -> AccessResult:
    """Resolve `input_str` (a granule ID, an s3:// or https:// URL, or a
    local path) to an ``AccessResult``.

    ``prompter`` exposes ``ask(text, default=None) -> str``,
    ``choose(text, options, default) -> str``, and
    ``confirm(text, default=False) -> bool``. In a non-interactive
    context the caller passes a prompter that raises or returns
    defaults -- this function just calls it; it never prompts for a
    password/credential value itself.
    """
    findings: list = []
    notes: list = []

    kind = formats.detect_input(input_str)

    if kind == "local":
        findings.append(_finding(
            "D0-hosting", "D", "skipped",
            f"{input_str!r} is a local path; hosting/access criteria "
            "cannot be assessed from a local copy.",
            remediation=("Point check-cloud-ready at the cloud-hosted URL "
                        "or granule ID instead of a local copy."),
        ))
        return AccessResult(fs=None, path=input_str, url=input_str,
                             findings=findings, auth="local", notes=notes)

    if no_network:
        findings.append(_finding(
            "D0-network", "D", "skipped",
            "no_network=True: all access probes and remote resolution "
            "were skipped.",
        ))
        return AccessResult(fs=None, path=input_str, url=input_str,
                             findings=findings, auth="local", notes=notes)

    if kind == "granule-id":
        return _resolve_from_granule(
            input_str, prompter=prompter, earthaccess_fallback=earthaccess_fallback,
            findings=findings, notes=notes)

    url = input_str
    if url.startswith("s3://"):
        return _resolve_s3(
            url, prompter=prompter, anon=anon, credentials_url=credentials_url,
            granule_id=granule_id, earthaccess_fallback=earthaccess_fallback,
            findings=findings, notes=notes)

    return _resolve_https(url, findings=findings, notes=notes)


# -------------------------------------------------------------------- step 1

def _resolve_from_granule(granule_id, *, prompter, earthaccess_fallback,
                           findings, notes):
    try:
        result = resolve_granule.resolve(granule_id)
    except GranuleResolutionError as e:
        findings.append(_finding(
            "D2-auth", "D", "fail",
            f"Could not resolve granule ID {granule_id!r} via CMR: {e}",
            remediation=("Verify the granule concept ID (format "
                        "G<digits>-<PROVIDER>), or supply a direct URL "
                        "instead."),
        ))
        return AccessResult(fs=None, path=granule_id, url=granule_id,
                             findings=findings, auth="error", notes=notes)

    credentials_url = result.get("credentials_url")
    s3_urls = result.get("s3_urls") or []
    https_urls = result.get("https_urls") or []

    url = None
    if len(s3_urls) == 1:
        url = s3_urls[0]
    elif len(s3_urls) > 1:
        url = prompter.choose(
            "Multiple S3 assets found for this granule -- which should be assessed?",
            s3_urls, s3_urls[0])
    elif https_urls:
        url = https_urls[0]
        notes.append("Granule has no S3 direct-access URL; falling back to an HTTPS URL.")

    if url is None:
        findings.append(_finding(
            "D2-auth", "D", "fail",
            f"Granule {granule_id!r} resolved via CMR but has no S3 or "
            "HTTPS data URLs.",
        ))
        return AccessResult(fs=None, path=granule_id, url=granule_id,
                             findings=findings, auth="error", notes=notes)

    if not url.startswith("s3://"):
        return _resolve_https(url, findings=findings, notes=notes)

    try:
        fs, path = nasa_s3.get_fs(url, credentials_url=credentials_url,
                                  earthaccess_fallback=earthaccess_fallback)
    except Exception as e:
        _append_s3_error_finding(findings, e)
        return AccessResult(fs=None, path=url, url=url, findings=findings,
                             auth="error", notes=notes)

    findings.append(_finding(
        "D2-auth", "D", "pass",
        f"S3 access resolved via CMR credentials endpoint {credentials_url}.",
    ))
    return AccessResult(fs=fs, path=path, url=url, findings=findings,
                         auth="obstore-cmr", notes=notes)


# ------------------------------------------------------------------- s3 path

def _resolve_s3(url, *, prompter, anon, credentials_url, granule_id,
                 earthaccess_fallback, findings, notes):
    probe_result = probe.probe_s3(url)
    findings.append(_range_finding(probe_result))

    public = probe_result.get("classification") == "ok"

    if public or anon:
        fs, path = nasa_s3.get_fs(url, anon=True)
        if public:
            evidence = "Anonymous S3 access succeeded (HeadObject + ranged GetObject)."
            status = "pass"
        else:
            evidence = "anon=True forced anonymous S3 access despite a protected probe result."
            status = "warn"
        findings.append(_finding("D2-auth", "D", status, evidence))
        return AccessResult(fs=fs, path=path, url=url, findings=findings,
                             auth="anonymous", notes=notes)

    notes.append("Anonymous S3 access failed or requires authentication.")

    if granule_id:
        return _resolve_from_granule(
            granule_id, prompter=prompter, earthaccess_fallback=earthaccess_fallback,
            findings=findings, notes=notes)

    if credentials_url:
        result = _try_build_via_credentials_url(url, credentials_url, findings, notes)
        if result is not None:
            return result
        return _maybe_earthaccess_fallback(url, prompter, earthaccess_fallback, findings, notes)

    # Protected S3, and the caller supplied neither a granule ID nor a
    # credentials endpoint up front: ask (per auth-workflow.md step 3 --
    # a granule ID is preferred since CMR resolves the exact endpoint).
    has_granule = prompter.confirm(
        "Do you have a CMR granule ID for this dataset?", default=False)
    if has_granule:
        gid = prompter.ask("Granule ID (e.g. G1234567890-PROVIDER):")
        return _resolve_from_granule(
            gid, prompter=prompter, earthaccess_fallback=earthaccess_fallback,
            findings=findings, notes=notes)

    endpoint = _choose_credentials_endpoint(url, prompter)
    if endpoint:
        result = _try_build_via_credentials_url(url, endpoint, findings, notes)
        if result is not None:
            return result

    return _maybe_earthaccess_fallback(url, prompter, earthaccess_fallback, findings, notes)


def _guess_credentials_endpoint_label(url):
    low = url.lower()
    for label, endpoint in nasa_s3.KNOWN_CREDENTIALS_ENDPOINTS.items():
        host = endpoint.split("/")[2].lower()
        root = host.split(".")[0]
        if root and root in low:
            return label
    return next(iter(nasa_s3.KNOWN_CREDENTIALS_ENDPOINTS))


def _choose_credentials_endpoint(url, prompter):
    options = list(nasa_s3.KNOWN_CREDENTIALS_ENDPOINTS.keys())
    default = _guess_credentials_endpoint_label(url)
    choice = prompter.choose(
        "Which Earthdata S3-credentials endpoint should be used for this bucket?",
        options, default)
    return nasa_s3.KNOWN_CREDENTIALS_ENDPOINTS.get(choice)


def _try_build_via_credentials_url(url, credentials_url, findings, notes):
    """Attempt obstore-via-credentials-endpoint S3 access. Returns an
    AccessResult on success, or None on failure (after recording a
    finding) so the caller can fall through to the next option.
    """
    try:
        fs, path = nasa_s3.get_fs(url, credentials_url=credentials_url)
    except Exception as e:
        _append_s3_error_finding(findings, e)
        notes.append(f"obstore credentials-endpoint access via {credentials_url} failed.")
        return None
    findings.append(_finding(
        "D2-auth", "D", "pass",
        f"S3 access resolved via credentials endpoint {credentials_url}.",
    ))
    return AccessResult(fs=fs, path=path, url=url, findings=findings,
                         auth="obstore-endpoint", notes=notes)


def _maybe_earthaccess_fallback(url, prompter, earthaccess_fallback, findings, notes):
    use_earthaccess = earthaccess_fallback or prompter.confirm(
        "Try earthaccess to mint temporary S3 credentials automatically?",
        default=False)
    if not use_earthaccess:
        findings.append(_finding(
            "D2-auth", "D", "fail",
            "S3 object is not anonymously accessible and no working "
            "credential path (granule ID, credentials endpoint, or "
            "earthaccess) was found.",
            remediation=("Supply a granule ID, an s3credentials endpoint, "
                        "or opt into earthaccess."),
        ))
        return AccessResult(fs=None, path=url, url=url, findings=findings,
                             auth="error", notes=notes)

    try:
        fs, path = nasa_s3.get_fs(url, earthaccess_fallback=True)
    except Exception as e:
        _append_s3_error_finding(findings, e)
        return AccessResult(fs=None, path=url, url=url, findings=findings,
                             auth="error", notes=notes)

    findings.append(_finding(
        "D2-auth", "D", "pass",
        "S3 access resolved via earthaccess-minted temporary credentials.",
    ))
    return AccessResult(fs=fs, path=path, url=url, findings=findings,
                         auth="earthaccess", notes=notes)


# ----------------------------------------------------------------- https path

def _resolve_https(url, *, findings, notes):
    probe_result = probe.probe_https(url)
    findings.append(_range_finding(probe_result))

    if probe_result.get("classification") != "auth-required":
        return _plain_https_fs(url, findings, notes)

    notes.append("HTTPS endpoint requires Earthdata Login (401/403 or URS redirect detected).")

    # Never prompt for a password in-band: only look at env/netrc-backed
    # bearer-token minting.
    token = nasa_s3.edl_bearer_token()
    if token is None:
        findings.append(_finding(
            "D2-auth", "D", "skipped",
            "HTTPS endpoint requires Earthdata Login and no bearer token "
            "is available from the environment.",
            remediation=("Set EARTHDATA_TOKEN, or EARTHDATA_USERNAME + "
                        "EARTHDATA_PASSWORD, or configure ~/.netrc for "
                        "urs.earthdata.nasa.gov, then retry."),
        ))
        return AccessResult(fs=None, path=url, url=url, findings=findings,
                             auth="error", notes=notes)

    fs = _build_fsspec_https(headers={"Authorization": f"Bearer {token}"})
    if fs is None:
        findings.append(_finding(
            "D2-auth", "D", "skipped",
            "An Earthdata Login bearer token is available, but the local "
            "HTTPS filesystem could not be constructed (fsspec https "
            "support unavailable -- likely missing aiohttp).",
            remediation="pip install aiohttp (or fsspec[http]).",
        ))
    else:
        findings.append(_finding(
            "D2-auth", "D", "pass",
            "HTTPS access authenticated via an Earthdata Login bearer token.",
        ))
    return AccessResult(fs=fs, path=url, url=url, findings=findings,
                         auth="edl-bearer", notes=notes)


def _plain_https_fs(url, findings, notes):
    fs = _build_fsspec_https(headers=None)
    if fs is None:
        findings.append(_finding(
            "D2-auth", "D", "skipped",
            "Anonymous HTTPS filesystem could not be constructed (fsspec "
            "https support unavailable -- likely missing aiohttp).",
            remediation="pip install aiohttp (or fsspec[http]).",
        ))
    return AccessResult(fs=fs, path=url, url=url, findings=findings,
                         auth="anonymous", notes=notes)


def _build_fsspec_https(headers):
    kwargs = {}
    if headers:
        kwargs["client_kwargs"] = {"headers": headers}
    try:
        return fsspec.filesystem("https", **kwargs)
    except Exception:
        # e.g. aiohttp not installed -- findings/metadata remain usable
        # even without a live fs.
        return None
