#!/usr/bin/env python3
"""Probe a dataset entrypoint for reachability, auth requirements, and
HTTP range-request support.

Usage: python3 probe_access.py <url> [--timeout 10] [--signed]
                                [--credentials-url URL] [--granule-id G...-PROVIDER]

Prints a JSON result to stdout; human-readable notes to stderr.
Exit code 0 even on probe failures (the failure IS the result); nonzero only
for usage/dependency errors.
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nasa_s3
import resolve_granule


def note(msg):
    print(msg, file=sys.stderr)


def probe_https(url, timeout):
    try:
        import requests
    except ImportError:
        sys.exit("Missing dependency: pip install requests")
    out = {"scheme": "https", "url": url}

    # HEAD first (some servers reject HEAD; that's informative, not fatal)
    try:
        r = requests.head(url, timeout=timeout, allow_redirects=False)
        out["head_status"] = r.status_code
        out["accept_ranges_header"] = r.headers.get("Accept-Ranges")
        out["content_length"] = r.headers.get("Content-Length")
        loc = r.headers.get("Location")
        if loc:
            out["redirect_location"] = loc
            if "urs.earthdata.nasa.gov" in loc:
                out["auth_detected"] = "earthdata-login"
    except Exception as e:
        out["head_error"] = f"{type(e).__name__}: {e}"

    # Range GET is the authoritative test
    try:
        t0 = time.monotonic()
        r = requests.get(url, headers={"Range": "bytes=0-1023"},
                         timeout=timeout, allow_redirects=True, stream=True)
        body = r.raw.read(2048)
        out["range_get_status"] = r.status_code
        out["range_ttfb_s"] = round(time.monotonic() - t0, 3)
        out["final_url_host"] = r.url.split("/")[2] if "://" in r.url else None
        if any("urs.earthdata.nasa.gov" in h.headers.get("Location", "")
               for h in r.history):
            out["auth_detected"] = "earthdata-login"
        if r.status_code == 206:
            out["range_requests"] = "supported"
            out["content_range"] = r.headers.get("Content-Range")
        elif r.status_code == 200:
            out["range_requests"] = "ignored (200 with full body)"
            out["note"] = ("Server ignores Range headers - subsetting via "
                           "range requests will not work on this endpoint.")
        elif r.status_code in (401, 403):
            out["range_requests"] = "unknown (auth required)"
        else:
            out["range_requests"] = f"unexpected status {r.status_code}"
        out["first_bytes_hex"] = body[:16].hex() if body else None
        r.close()
    except Exception as e:
        out["range_get_error"] = f"{type(e).__name__}: {e}"

    # EDL-protected HTTPS: retry once with a bearer token if EDL was detected
    # (redirect to urs.earthdata.nasa.gov), or the host looks like NASA
    # Earthdata and the unauthenticated probe came back 401/403.
    looks_nasa = nasa_s3.looks_like_nasa_earthdata(url)
    unauth_denied = out.get("head_status") in (401, 403) or \
        out.get("range_get_status") in (401, 403)
    if out.get("auth_detected") == "earthdata-login" or (looks_nasa and unauth_denied):
        token = nasa_s3.edl_bearer_token()
        if token is None:
            out["https_authenticated"] = {
                "note": "no EDL bearer token available - set EARTHDATA_TOKEN, "
                        "EARTHDATA_USERNAME/EARTHDATA_PASSWORD, or ~/.netrc"}
        else:
            out["https_authenticated"] = probe_https_bearer(url, timeout, token)
    return out


def probe_https_bearer(url, timeout, token):
    """Retry HEAD + range GET once with an EDL Authorization: Bearer header."""
    import requests
    headers = {"Authorization": f"Bearer {token}"}
    out = {"scheme": "https", "url": url}
    try:
        r = requests.head(url, timeout=timeout, allow_redirects=True, headers=headers)
        out["head_status"] = r.status_code
    except Exception as e:
        out["head_error"] = f"{type(e).__name__}: {e}"
    try:
        t0 = time.monotonic()
        h = dict(headers, **{"Range": "bytes=0-1023"})
        r = requests.get(url, headers=h, timeout=timeout, allow_redirects=True,
                         stream=True)
        body = r.raw.read(2048)
        out["range_get_status"] = r.status_code
        out["range_ttfb_s"] = round(time.monotonic() - t0, 3)
        if r.status_code == 206:
            out["range_requests"] = "supported"
        elif r.status_code == 200:
            out["range_requests"] = "ignored (200 with full body)"
        elif r.status_code in (401, 403):
            out["range_requests"] = "unknown (bearer token rejected)"
        else:
            out["range_requests"] = f"unexpected status {r.status_code}"
        out["first_bytes_hex"] = body[:16].hex() if body else None
        r.close()
    except Exception as e:
        out["range_get_error"] = f"{type(e).__name__}: {e}"
    return out


_CLASSIFICATION_HINTS = {
    "credentials-endpoint-auth": (
        "EDL credentials missing/invalid — set EARTHDATA_TOKEN, "
        "EARTHDATA_USERNAME/EARTHDATA_PASSWORD, or ~/.netrc"),
    "in-region-only": (
        "credentials minted successfully; S3 denied from this network — "
        "expected outside us-west-2; re-run in-region"),
}


def probe_authenticated_s3(url, timeout, credentials_url):
    """Authenticated S3 probe via obstore + NasaEarthdataCredentialProvider."""
    try:
        import obstore
        from obstore.store import S3Store
        from obstore.auth.earthdata import NasaEarthdataCredentialProvider
    except ImportError:
        return {"error": "obstore not installed - pip install 'obstore>=0.9'"}

    bucket, _, key = url[5:].partition("/")
    out = {"credentials_url": credentials_url}
    cp = NasaEarthdataCredentialProvider(credentials_url)
    try:
        store = S3Store.from_url(f"s3://{bucket}", credential_provider=cp)
        t0 = time.monotonic()
        obstore.head(store, key)
        obstore.get_range(store, key, start=0, end=1024)
        out["ok"] = True
        out["latency_s"] = round(time.monotonic() - t0, 3)
    except Exception as e:
        out["ok"] = False
        out["error"] = f"{type(e).__name__}: {e}"
        classification = nasa_s3.classify_s3_error(e)
        out["classification"] = classification
        hint = _CLASSIFICATION_HINTS.get(classification)
        if hint:
            out["hint"] = hint
    finally:
        cp.close()
    return out


def probe_s3(url, timeout, signed):
    try:
        import boto3
        import botocore
        from botocore.config import Config
        from botocore import UNSIGNED
    except ImportError:
        sys.exit("Missing dependency: pip install boto3")
    out = {"scheme": "s3", "url": url}
    assert url.startswith("s3://")
    bucket, _, key = url[5:].partition("/")
    out["bucket"], out["key"] = bucket, key

    # Region discovery: HeadBucket returns x-amz-bucket-region even on 403/301
    cfg = Config(connect_timeout=timeout, read_timeout=timeout,
                 retries={"max_attempts": 1})
    anon = boto3.client("s3", config=Config(signature_version=UNSIGNED,
                                            connect_timeout=timeout,
                                            read_timeout=timeout,
                                            retries={"max_attempts": 1}))
    region = None
    try:
        anon.head_bucket(Bucket=bucket)
        region = anon.meta.region_name
    except botocore.exceptions.ClientError as e:
        region = e.response.get("ResponseMetadata", {}).get(
            "HTTPHeaders", {}).get("x-amz-bucket-region")
        out["anon_head_bucket_error"] = e.response.get("Error", {}).get("Code")
    except Exception as e:
        out["anon_head_bucket_error"] = f"{type(e).__name__}: {e}"
    out["bucket_region"] = region

    def try_client(client, label):
        res = {}
        try:
            t0 = time.monotonic()
            h = client.head_object(Bucket=bucket, Key=key)
            res["head_object"] = "ok"
            res["content_length"] = h.get("ContentLength")
            g = client.get_object(Bucket=bucket, Key=key, Range="bytes=0-1023")
            body = g["Body"].read()
            res["range_get"] = "ok (S3 always supports ranges)"
            res["range_ttfb_s"] = round(time.monotonic() - t0, 3)
            res["first_bytes_hex"] = body[:16].hex()
        except botocore.exceptions.ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            res["error"] = code
            if code in ("AccessDenied", "403"):
                res["hint"] = (
                    "403 from S3 can mean: credentials required, requester-pays, "
                    "or a bucket policy restricting to in-region compute (NASA "
                    "Earthdata direct-S3, usually us-west-2). If this is NASA "
                    "Earthdata data, the recommended path is a CMR granule "
                    "concept ID: run resolve_granule.py <granule-id> and "
                    "re-run this probe with --credentials-url. An out-of-region "
                    "403 with valid credentials is expected — assessment then "
                    "needs in-region compute.")
            elif code in ("301", "PermanentRedirect"):
                res["hint"] = f"Wrong region endpoint; bucket is in {region}."
        except Exception as e:
            res["error"] = f"{type(e).__name__}: {e}"
        out[label] = res

    kwargs = {"region_name": region} if region else {}
    anon_r = boto3.client("s3", config=Config(signature_version=UNSIGNED,
                                              connect_timeout=timeout,
                                              read_timeout=timeout,
                                              retries={"max_attempts": 1}),
                          **kwargs)
    try_client(anon_r, "anonymous")
    if signed or out["anonymous"].get("error"):
        try:
            signed_c = boto3.client("s3", config=cfg, **kwargs)
            try_client(signed_c, "signed")
        except Exception as e:
            out["signed"] = {"error": f"{type(e).__name__}: {e}"}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--timeout", type=float, default=10)
    ap.add_argument("--signed", action="store_true",
                    help="also try with ambient AWS credentials")
    ap.add_argument("--credentials-url", help="DAAC s3credentials endpoint URL")
    ap.add_argument("--granule-id",
                    help="CMR granule concept ID, resolved via resolve_granule.py "
                         "(ignored if --credentials-url is also given)")
    a = ap.parse_args()
    if a.url.startswith("s3://"):
        out = probe_s3(a.url, a.timeout, a.signed)
        credentials_url = a.credentials_url
        if not credentials_url and a.granule_id:
            try:
                resolved = resolve_granule.resolve(a.granule_id)
                credentials_url = resolved.get("credentials_url")
                if not credentials_url:
                    out["authenticated"] = {
                        "error": "granule has no S3 credentials endpoint in "
                                 "CMR - is this DAAC in Earthdata Cloud?"}
            except SystemExit as e:
                out["authenticated"] = {"error": f"granule resolution failed: {e}"}
                credentials_url = None
        if credentials_url:
            out["authenticated"] = probe_authenticated_s3(
                a.url, a.timeout, credentials_url)
    elif a.url.startswith(("http://", "https://")):
        out = probe_https(a.url, a.timeout)
    else:
        out = {"scheme": "local", "url": a.url,
               "exists": os.path.exists(a.url),
               "note": "Local path - hosting/access criteria not assessable; "
                       "ask the user whether a cloud-hosted copy exists."}
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
