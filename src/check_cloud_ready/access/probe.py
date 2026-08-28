"""Transport probes: reachability, auth requirements, and HTTP
range-request support for https:// and s3:// data endpoints.

Ported from
``.claude/skills/check-cloud-ready/scripts/probe_access.py``, with one
behavioral fix (S2 -- see ``classify_access_failure``): NASA's TEA HTTPS
distribution answers HEAD anonymously but gates the ranged GET behind
Earthdata Login. The pre-fix classifier treated a 401/403 returned
specifically at the ranged-GET stage as a *range-support* failure ("no
range support") when it is actually an *auth* failure -- the server
never got a chance to say whether it supports ranges at all.
``classify_access_failure`` is now the single place that makes this
call, shared by both probes, so the distinction can't drift between
them.

Both probes soft-import their transport library (``requests`` /
``boto3``+``botocore``): if it isn't installed, the probe returns
``{"skipped": "<reason>", ...}`` rather than raising.
"""
from __future__ import annotations

import time

# Stages recognized by classify_access_failure. "open" covers any later
# lazy-open/read step (e.g. h5py/xarray/rasterio opening the resolved
# handle) that isn't a probe HEAD or ranged GET but can still fail with
# the same auth-vs-range ambiguity.
STAGES = ("head", "range-get", "open")

_AUTH_STATUS_CODES = (401, 403)
_AUTH_TEXT_MARKERS = ("401", "403", "unauthorized", "forbidden", "accessdenied")
_URS_MARKERS = (
    "urs.earthdata.nasa.gov", "urs-redirect", "earthdata-login", "earthdata login",
)


def classify_access_failure(stage: str, status_or_exc) -> str:
    """Classify one probe stage's outcome as ``"ok"``,
    ``"auth-required"``, or ``"no-range-support"``.

    ``stage`` is one of ``"head"``, ``"range-get"``, ``"open"``. Only
    ``"range-get"`` can ever produce ``"no-range-support"`` -- a HEAD or
    open failure that isn't recognizably an auth problem is treated as
    ``"ok"`` (uninformative, not fatal), matching the "some servers
    reject HEAD; that's informative, not fatal" behavior of the
    original probe.

    ``status_or_exc`` may be an HTTP status code (``int``), a
    redirect-target/marker string (e.g. a ``Location`` header value or
    an S3 error code such as ``"AccessDenied"``), or an exception
    instance -- anything ``str()``-able is accepted.

    The rule (S2 fix): a 401/403, or any redirect/marker pointing at
    Earthdata Login (URS), at ANY stage means ``"auth-required"`` --
    never ``"no-range-support"``, even when the failing stage is the
    ranged GET. Only a non-auth, non-206 response specifically to a
    range request means ``"no-range-support"``.
    """
    if isinstance(status_or_exc, int) and not isinstance(status_or_exc, bool):
        if status_or_exc in _AUTH_STATUS_CODES:
            return "auth-required"
        if status_or_exc == 206:
            return "ok"
        if stage == "range-get":
            return "no-range-support"
        return "ok"

    text = str(status_or_exc).lower()
    if any(marker in text for marker in _URS_MARKERS):
        return "auth-required"
    if any(marker in text for marker in _AUTH_TEXT_MARKERS):
        return "auth-required"
    if stage == "range-get":
        return "no-range-support"
    return "ok"


def _combine_classifications(head_classification, range_classification):
    """Combine a HEAD-stage and range-GET-stage classification into one
    overall verdict. Auth wins outright (found at either stage); the
    ranged GET is otherwise authoritative (it is the real test).
    """
    if "auth-required" in (head_classification, range_classification):
        return "auth-required"
    return range_classification


def probe_https(url, timeout=10, session=None):
    """Probe an ``https://`` URL: HEAD (informational only -- some
    servers reject HEAD, which is not itself fatal), then an
    authoritative ranged GET (``Range: bytes=0-1023``).

    ``session``, if given, replaces the ``requests`` module (any object
    exposing ``.head(...)``/``.get(...)`` with a ``requests``-like
    response) -- tests pass a fake session so no real network I/O
    happens. When ``session`` is omitted, ``requests`` is soft-imported;
    if it isn't installed, returns a ``{"skipped": ...}`` dict instead
    of raising.
    """
    if session is None:
        try:
            import requests as _requests
        except ImportError:
            return {
                "scheme": "https", "url": url,
                "skipped": "requests not installed (pip install requests)",
            }
        sess = _requests
    else:
        sess = session

    out = {"scheme": "https", "url": url}

    try:
        r = sess.head(url, timeout=timeout, allow_redirects=False)
        out["head_status"] = r.status_code
        out["accept_ranges_header"] = r.headers.get("Accept-Ranges")
        out["content_length"] = r.headers.get("Content-Length")
        loc = r.headers.get("Location")
        if loc:
            out["redirect_location"] = loc
        if loc and "urs.earthdata.nasa.gov" in loc:
            out["auth_detected"] = "earthdata-login"
            head_classification = "auth-required"
        else:
            head_classification = classify_access_failure("head", r.status_code)
    except Exception as e:
        out["head_error"] = f"{type(e).__name__}: {e}"
        head_classification = classify_access_failure("head", e)
    out["head_classification"] = head_classification

    try:
        t0 = time.monotonic()
        r = sess.get(url, headers={"Range": "bytes=0-1023"}, timeout=timeout,
                     allow_redirects=True, stream=True)
        body = _read_prefix(r)
        out["range_get_status"] = r.status_code
        out["range_ttfb_s"] = round(time.monotonic() - t0, 3)
        out["final_url_host"] = r.url.split("/")[2] if "://" in r.url else None
        history = getattr(r, "history", None) or []
        urs_redirected = any(
            "urs.earthdata.nasa.gov" in (getattr(h, "headers", {}) or {}).get("Location", "")
            for h in history
        )
        if urs_redirected:
            out["auth_detected"] = "earthdata-login"
            range_classification = "auth-required"
        else:
            range_classification = classify_access_failure("range-get", r.status_code)

        if range_classification == "ok":
            out["range_requests"] = "supported"
            out["content_range"] = r.headers.get("Content-Range")
        elif range_classification == "no-range-support":
            out["range_requests"] = (
                "ignored (200 with full body)" if r.status_code == 200
                else f"unexpected status {r.status_code}"
            )
            out["note"] = ("Server ignores Range headers - subsetting via "
                            "range requests will not work on this endpoint.")
        else:
            out["range_requests"] = "unknown (auth required)"
        out["first_bytes_hex"] = body[:16].hex() if body else None

        close = getattr(r, "close", None)
        if callable(close):
            close()
    except Exception as e:
        out["range_get_error"] = f"{type(e).__name__}: {e}"
        range_classification = classify_access_failure("range-get", e)
    out["range_classification"] = range_classification

    out["classification"] = _combine_classifications(head_classification, range_classification)
    return out


def _read_prefix(response, n=2048):
    raw = getattr(response, "raw", None)
    if raw is not None and hasattr(raw, "read"):
        return raw.read(n)
    content = getattr(response, "content", b"") or b""
    return content[:n]


_S3_AUTH_HINT = (
    "403 from S3 can mean: credentials required, requester-pays, or a "
    "bucket policy restricting access to in-region compute (common for "
    "NASA Earthdata direct-S3 buckets, usually us-west-2). If this is an "
    "Earthdata bucket, mint S3 credentials via the DAAC's s3credentials "
    "endpoint or run in-region with earthaccess.get_s3_credentials() -- "
    "never fall back to HTTPS for this data."
)


def probe_s3(url, timeout=10, signed=False):
    """Probe an ``s3://`` URL anonymously (and, if ``signed`` or the
    anonymous attempt fails, with ambient/signed credentials):
    HeadBucket for region discovery, then HeadObject + a ranged
    GetObject.

    Soft-imports ``boto3``/``botocore``; if unavailable, returns a
    ``{"skipped": ...}`` dict instead of raising.
    """
    try:
        import boto3
        import botocore
        from botocore.config import Config
        from botocore import UNSIGNED
    except ImportError:
        return {
            "scheme": "s3", "url": url,
            "skipped": "boto3 not installed (pip install boto3)",
        }

    assert url.startswith("s3://")
    out = {"scheme": "s3", "url": url}
    bucket, _, key = url[5:].partition("/")
    out["bucket"], out["key"] = bucket, key

    signed_cfg = Config(connect_timeout=timeout, read_timeout=timeout,
                        retries={"max_attempts": 1})
    anon_cfg = Config(signature_version=UNSIGNED, connect_timeout=timeout,
                      read_timeout=timeout, retries={"max_attempts": 1})

    bucket_region = None
    probe_client = boto3.client("s3", config=anon_cfg)
    try:
        probe_client.head_bucket(Bucket=bucket)
        bucket_region = probe_client.meta.region_name
    except botocore.exceptions.ClientError as e:
        bucket_region = e.response.get("ResponseMetadata", {}).get(
            "HTTPHeaders", {}).get("x-amz-bucket-region")
        out["anon_head_bucket_error"] = e.response.get("Error", {}).get("Code")
    except Exception as e:
        out["anon_head_bucket_error"] = f"{type(e).__name__}: {e}"
    out["bucket_region"] = bucket_region

    def try_client(client, label):
        res = {}
        try:
            t0 = time.monotonic()
            h = client.head_object(Bucket=bucket, Key=key)
            res["head_object"] = "ok"
            res["head_classification"] = "ok"
            res["content_length"] = h.get("ContentLength")
        except botocore.exceptions.ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            res["head_object_error"] = code
            res["head_classification"] = classify_access_failure("head", code)
        except Exception as e:
            res["head_object_error"] = f"{type(e).__name__}: {e}"
            res["head_classification"] = classify_access_failure("head", e)

        try:
            t0 = time.monotonic()
            g = client.get_object(Bucket=bucket, Key=key, Range="bytes=0-1023")
            body = g["Body"].read()
            res["range_get"] = "ok (S3 always supports ranges)"
            res["range_classification"] = "ok"
            res["range_ttfb_s"] = round(time.monotonic() - t0, 3)
            res["first_bytes_hex"] = body[:16].hex()
        except botocore.exceptions.ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            res["range_get_error"] = code
            classification = classify_access_failure("range-get", code)
            res["range_classification"] = classification
            if classification == "auth-required":
                res["hint"] = _S3_AUTH_HINT
            elif code in ("301", "PermanentRedirect"):
                res["hint"] = f"Wrong region endpoint; bucket is in {bucket_region}."
        except Exception as e:
            res["range_get_error"] = f"{type(e).__name__}: {e}"
            res["range_classification"] = classify_access_failure("range-get", e)

        res["classification"] = _combine_classifications(
            res.get("head_classification", "ok"), res.get("range_classification", "ok"))
        out[label] = res

    kwargs = {"region_name": bucket_region} if bucket_region else {}
    anon_client = boto3.client("s3", config=anon_cfg, **kwargs)
    try_client(anon_client, "anonymous")

    if signed or out["anonymous"].get("classification") != "ok":
        try:
            signed_client = boto3.client("s3", config=signed_cfg, **kwargs)
            try_client(signed_client, "signed")
        except Exception as e:
            out["signed"] = {"error": f"{type(e).__name__}: {e}"}

    out["classification"] = out["anonymous"].get("classification", "ok")
    return out


def probe_local(path):
    """Local paths are not a hosting/access probe target: hosting
    criteria can only be assessed from a URL, not a copy already on
    disk.
    """
    import os
    return {
        "scheme": "local", "url": path, "exists": os.path.exists(path),
        "classification": "not-assessable",
        "note": ("Local path - hosting/access criteria not assessable; "
                 "ask the user whether a cloud-hosted copy exists."),
    }
