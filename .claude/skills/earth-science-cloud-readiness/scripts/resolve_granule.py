#!/usr/bin/env python3
"""Resolve a NASA CMR granule concept ID to its S3 data URLs and
per-DAAC S3-credentials endpoint.

NOTE: this file is duplicated verbatim at
.claude/skills/check-cloud-ready/scripts/resolve_granule.py and
.claude/skills/earth-science-cloud-readiness/scripts/resolve_granule.py.
Keep both copies byte-identical when editing either one.

Usage: python3 resolve_granule.py <granule-concept-id>

Prints a JSON result to stdout; human-readable notes to stderr.
Exit code 1 on invalid ID, network error, or 404 from CMR.
"""
import argparse
import json
import re
import sys
import urllib.request
import urllib.error

CMR_GRANULE_URL = "https://cmr.earthdata.nasa.gov/search/concepts/{granule_id}.umm_json"
GRANULE_ID_RE = re.compile(r"^G\d+-[A-Z0-9_]+$")
USER_AGENT = "check-cloud-ready-resolve-granule/1.0"


def note(msg):
    print(msg, file=sys.stderr)


def parse_related_urls(umm):
    """Extract S3 data URLs, HTTPS data URLs, and the S3-credentials
    endpoint from a UMM-G granule's RelatedUrls. Pure function, no I/O.
    """
    related = umm.get("RelatedUrls") or []

    credentials_url = None
    credentials_url_type = None
    credentials_url_description = None
    for entry in related:
        url = entry.get("URL") or ""
        if url.rstrip("/").lower().endswith("/s3credentials"):
            credentials_url = url
            credentials_url_type = entry.get("Type")
            credentials_url_description = entry.get("Description")
            break

    s3_urls = [
        entry["URL"] for entry in related
        if entry.get("Type") == "GET DATA VIA DIRECT ACCESS"
        and (entry.get("URL") or "").startswith("s3://")
    ]
    https_urls = [
        entry["URL"] for entry in related
        if entry.get("Type") == "GET DATA"
    ]

    return {
        "credentials_url": credentials_url,
        "credentials_url_type": credentials_url_type,
        "credentials_url_description": credentials_url_description,
        "s3_urls": s3_urls,
        "https_urls": https_urls,
        "note": ("https_urls are for reference only; never assess NASA "
                 "data via HTTPS when an S3 URL exists"),
    }


def resolve(granule_id):
    """Resolve a CMR granule concept ID to its data URLs and
    S3-credentials endpoint. Raises SystemExit with a helpful message
    on invalid input, network failure, or a 404 from CMR.
    """
    if not GRANULE_ID_RE.match(granule_id):
        sys.exit(
            f"Invalid granule concept ID: {granule_id!r} "
            "(expected format like G1234567890-PROVIDER)"
        )

    url = CMR_GRANULE_URL.format(granule_id=granule_id)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            umm = json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            sys.exit(
                f"Granule concept ID not found in CMR: {granule_id} "
                f"(tried {url})"
            )
        sys.exit(f"CMR request failed: HTTP {e.code} for {url}")
    except urllib.error.URLError as e:
        sys.exit(f"CMR request failed: {e.reason} for {url}")

    result = parse_related_urls(umm)
    result["granule_id"] = granule_id
    result["provider"] = granule_id.rsplit("-", 1)[-1]

    if result["credentials_url"] is None:
        note("no S3 credentials endpoint in CMR metadata — "
             "is this DAAC in Earthdata Cloud?")

    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("granule_id", help="CMR granule concept ID, e.g. G1234567890-ASF")
    a = ap.parse_args()
    out = resolve(a.granule_id)
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
