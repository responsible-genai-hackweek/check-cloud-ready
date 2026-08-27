#!/usr/bin/env python3
"""Probe a dataset entrypoint for reachability, auth requirements, and
HTTP range-request support.

Usage: python3 probe_access.py <url> [--timeout 10] [--signed]

Prints a JSON result to stdout; human-readable notes to stderr.
Exit code 0 even on probe failures (the failure IS the result); nonzero only
for usage/dependency errors.
"""
import argparse
import json
import sys
import time


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
                    "or a bucket policy restricting access to in-region compute "
                    "(common for NASA Earthdata direct-S3 buckets, usually "
                    "us-west-2). If this is an Earthdata bucket, assess via the "
                    "HTTPS/EDL endpoint instead, or run in-region with "
                    "earthaccess.get_s3_credentials().")
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
    a = ap.parse_args()
    if a.url.startswith("s3://"):
        out = probe_s3(a.url, a.timeout, a.signed)
    elif a.url.startswith(("http://", "https://")):
        out = probe_https(a.url, a.timeout)
    else:
        import os
        out = {"scheme": "local", "url": a.url,
               "exists": os.path.exists(a.url),
               "note": "Local path - hosting/access criteria not assessable; "
                       "ask the user whether a cloud-hosted copy exists."}
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
