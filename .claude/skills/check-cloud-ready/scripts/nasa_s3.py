#!/usr/bin/env python3
"""Auth helpers for reading NASA Earthdata Cloud data directly from S3.

NOTE: this file is duplicated verbatim at
.claude/skills/check-cloud-ready/scripts/nasa_s3.py and
.claude/skills/earth-science-cloud-readiness/scripts/nasa_s3.py.
Keep both copies byte-identical when editing either one.

There is NO HTTPS code path in this module. NASA Earthdata Cloud data
must be read via S3 (in us-west-2) with credentials minted from a
DAAC's `/s3credentials` endpoint; falling back to HTTPS silently
defeats the point of an in-region cloud read and this module refuses
to do it.

Usage: python3 nasa_s3.py <url> [--granule-id G123...-PROVIDER]
                                  [--credentials-url URL]
                                  [--anon] [--earthaccess-fallback]

Soft-imports obstore/earthaccess/s3fs so this module always imports
cleanly, even in environments with none of them installed.
"""
import argparse
import base64
import json
import netrc
import os
import sys
import urllib.request

USER_AGENT = "check-cloud-ready-nasa-s3/1.0"
EDL_HOST = "urs.earthdata.nasa.gov"
EDL_TOKENS_LIST_URL = f"https://{EDL_HOST}/api/users/tokens"
EDL_TOKEN_CREATE_URL = f"https://{EDL_HOST}/api/users/token"


def _try(name):
    try:
        return __import__(name)
    except Exception:
        return None


obstore = _try("obstore")
earthaccess = _try("earthaccess")
s3fs = _try("s3fs")


_NASA_EARTHDATA_MARKERS = (
    "earthdatacloud", "daac", "cumulus", "podaac", "lpdaac", "gesdisc",
    "nsidc", "asf", "ornl", "ghrc", "laads", "obdaac", "nisar", "opera",
)


def looks_like_nasa_earthdata(url: str) -> bool:
    """Case-insensitive heuristic: does this URL look like NASA
    Earthdata Cloud data? False negatives are expected and handled by
    skill prose ("any anon-403 bucket -> ask the user").
    """
    low = url.lower()
    return any(marker in low for marker in _NASA_EARTHDATA_MARKERS)


# Well-known Earthdata Cloud s3credentials endpoints, verified 2026-08-27
# via GET (302/307 redirect to URS observed for every entry below).
# These are guesses for the agent to offer the user when no granule ID
# or endpoint is known (per auth-workflow.md). A granule-ID CMR lookup
# via resolve_granule.py is always more reliable than this table.
KNOWN_CREDENTIALS_ENDPOINTS = {
    "PO.DAAC": "https://archive.podaac.earthdata.nasa.gov/s3credentials",
    "LP DAAC": "https://data.lpdaac.earthdatacloud.nasa.gov/s3credentials",
    "ORNL DAAC": "https://data.ornldaac.earthdata.nasa.gov/s3credentials",
    "NSIDC DAAC": "https://data.nsidc.earthdatacloud.nasa.gov/s3credentials",
    "ASF DAAC": "https://sentinel1.asf.alaska.edu/s3credentials",
    "NISAR (ASF)": "https://nisar.asf.earthdatacloud.nasa.gov/s3credentials",
    "GES DISC": "https://data.gesdisc.earthdata.nasa.gov/s3credentials",
    "GHRC DAAC": "https://data.ghrc.earthdata.nasa.gov/s3credentials",
    "LAADS DAAC": "https://data.laadsdaac.earthdatacloud.nasa.gov/s3credentials",
    "OB.DAAC": "https://obdaac-tea.earthdatacloud.nasa.gov/s3credentials",
}

# URL substring -> earthaccess/CMR DAAC short name, for inferring a DAAC
# when only a bare URL is known (used by the earthaccess_fallback path).
_URL_DAAC_HINTS = (
    ("podaac", "PODAAC"),
    ("lpdaac", "LPDAAC"),
    ("ornl", "ORNLDAAC"),
    ("nsidc", "NSIDC"),
    ("nisar", "ASF"),
    ("asf", "ASF"),
    ("gesdisc", "GES_DISC"),
    ("ghrc", "GHRCDAAC"),
    ("laads", "LAADS"),
    ("obdaac", "OBDAAC"),
)


def _infer_daac(url):
    low = url.lower()
    for hint, daac in _URL_DAAC_HINTS:
        if hint in low:
            return daac
    return None


def _edl_credentials():
    """Return (username, password) from env vars or ~/.netrc (or
    $NETRC), or None if no EDL credentials are configured anywhere.
    Never prompts, never logs the values.
    """
    user = os.environ.get("EARTHDATA_USERNAME")
    password = os.environ.get("EARTHDATA_PASSWORD")
    if user and password:
        return user, password

    netrc_path = os.environ.get("NETRC") or os.path.expanduser("~/.netrc")
    if not os.path.isfile(netrc_path):
        return None
    try:
        parsed = netrc.netrc(netrc_path)
        auth = parsed.authenticators(EDL_HOST)
    except Exception:
        return None
    if not auth:
        return None
    login, _account, password = auth
    if not login or not password:
        return None
    return login, password


def _basic_auth_header(username, password):
    raw = f"{username}:{password}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def edl_bearer_token():
    """Return an Earthdata Login bearer token, or None if none is
    available. Order: EARTHDATA_TOKEN env var; else mint/reuse one via
    basic auth (env vars or ~/.netrc) against the URS token API. Never
    prints or requests secrets. HTTPS-only (URS), never used for S3.
    """
    token = os.environ.get("EARTHDATA_TOKEN")
    if token:
        return token

    creds = _edl_credentials()
    if creds is None:
        return None
    username, password = creds
    headers = {
        "Authorization": _basic_auth_header(username, password),
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    try:
        req = urllib.request.Request(EDL_TOKENS_LIST_URL, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            tokens = json.load(resp)
        if tokens:
            return tokens[0].get("access_token")

        req2 = urllib.request.Request(
            EDL_TOKEN_CREATE_URL, headers=headers, method="POST", data=b"")
        with urllib.request.urlopen(req2, timeout=15) as resp:
            created = json.load(resp)
        return created.get("access_token")
    except Exception:
        return None


EARTHACCESS_FALLBACK_GUIDANCE = (
    "earthaccess could not mint S3 credentials. Verify: (1) the DAAC "
    "for this dataset, (2) the S3 credentials endpoint (a CMR granule "
    "ID resolves it exactly — see resolve_granule.py), (3) EDL "
    "credentials are present (EARTHDATA_TOKEN, "
    "EARTHDATA_USERNAME/EARTHDATA_PASSWORD, or ~/.netrc). Do NOT fall "
    "back to HTTPS for NASA S3 data."
)


def _import_resolve_granule():
    import importlib.util
    this_dir = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(this_dir, "resolve_granule.py")
    spec = importlib.util.spec_from_file_location("resolve_granule", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def get_fs(url, *, anon=False, credentials_url=None, granule_id=None,
           earthaccess_fallback=False):
    """Return (fs, path): an fsspec-compatible filesystem for `url`,
    and the path string to hand to it (the s3:// URL, unchanged).
    NO HTTPS code path exists anywhere in this function.
    """
    if granule_id:
        rg = _import_resolve_granule()
        result = rg.resolve(granule_id)
        credentials_url = result.get("credentials_url")
        if not credentials_url:
            raise RuntimeError(
                "granule has no S3 credentials endpoint in CMR — "
                "is this DAAC in Earthdata Cloud?"
            )

    if credentials_url:
        try:
            from obstore.auth.earthdata import NasaEarthdataCredentialProvider
            from obstore.fsspec import FsspecStore
        except ImportError as e:
            raise RuntimeError(
                "NASA Earthdata S3 credentials-endpoint auth requires "
                "obstore — pip install 'obstore>=0.9'"
            ) from e
        cp = NasaEarthdataCredentialProvider(credentials_url)
        fs = FsspecStore("s3", credential_provider=cp)
        return fs, url

    if earthaccess_fallback:
        try:
            import earthaccess as ea
        except ImportError as e:
            raise RuntimeError(EARTHACCESS_FALLBACK_GUIDANCE) from e
        try:
            ea.login(strategy="all")
            if credentials_url:
                fs = ea.get_s3_filesystem(endpoint=credentials_url)
            else:
                daac = _infer_daac(url)
                if daac is None:
                    raise RuntimeError("cannot infer a DAAC from this URL")
                fs = ea.get_s3_filesystem(daac=daac)
        except Exception as e:
            raise RuntimeError(EARTHACCESS_FALLBACK_GUIDANCE) from e
        return fs, url

    try:
        import s3fs as _s3fs
    except ImportError as e:
        raise RuntimeError(
            "s3fs is required for anonymous/legacy S3 access — "
            "pip install s3fs"
        ) from e
    fs = _s3fs.S3FileSystem(anon=anon)
    return fs, url


def _exception_chain_text(exc):
    """Lowercased text of an exception's str(), args, and chained
    __cause__/__context__ exceptions, for best-effort classification.
    """
    parts = []
    seen = set()
    e = exc
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        parts.append(str(e))
        for a in getattr(e, "args", ()) or ():
            parts.append(str(a))
        e = getattr(e, "__cause__", None) or getattr(e, "__context__", None)
    return " | ".join(parts).lower()


def classify_s3_error(exc: BaseException) -> str:
    """Best-effort classification of an S3/auth exception:
    - "credentials-endpoint-auth": an EDL/URS problem (bad or missing
      EDL credentials, expired session). Fixable from anywhere.
      Caller should print: "Earthdata Login credentials are missing,
      expired, or lack access to this DAAC's s3credentials endpoint.
      Check EARTHDATA_TOKEN / EARTHDATA_USERNAME+PASSWORD / ~/.netrc."
    - "in-region-only": S3 minted credentials fine but denied the
      object/bucket read. Almost always means the request isn't
      running in AWS us-west-2. Caller should print: "S3 credentials
      were minted successfully but access was denied at the bucket/
      object level — this almost always means the request is not
      running inside AWS us-west-2. Run from a us-west-2 compute
      environment, or use the HTTPS URL for out-of-region access."
    - "unknown": anything else.
    """
    text = _exception_chain_text(exc)

    auth_status_markers = ("401", "403", "unauthorized")
    credentials_endpoint_markers = ("urs.earthdata", "s3credentials", "earthdata login")
    if any(m in text for m in auth_status_markers) and any(
            m in text for m in credentials_endpoint_markers):
        return "credentials-endpoint-auth"

    denial_markers = ("accessdenied", "access denied", "forbidden")
    if any(m in text for m in denial_markers) and not any(
            m in text for m in credentials_endpoint_markers):
        return "in-region-only"

    return "unknown"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("url", help="s3:// or https:// data URL")
    ap.add_argument("--granule-id", help="CMR granule concept ID, resolved via resolve_granule.py")
    ap.add_argument("--credentials-url", help="DAAC s3credentials endpoint URL")
    ap.add_argument("--anon", action="store_true", help="anonymous S3 access (legacy path)")
    ap.add_argument("--earthaccess-fallback", action="store_true")
    a = ap.parse_args()

    print(f"looks_like_nasa_earthdata: {looks_like_nasa_earthdata(a.url)}", file=sys.stderr)
    print(f"backends available: obstore={obstore is not None} "
          f"earthaccess={earthaccess is not None} s3fs={s3fs is not None}",
          file=sys.stderr)

    if not (a.granule_id or a.credentials_url or a.earthaccess_fallback):
        return

    try:
        fs, path = get_fs(
            a.url, anon=a.anon, credentials_url=a.credentials_url,
            granule_id=a.granule_id, earthaccess_fallback=a.earthaccess_fallback)
    except Exception as e:
        print(f"error constructing filesystem: {e}", file=sys.stderr)
        print(f"classification: {classify_s3_error(e)}", file=sys.stderr)
        sys.exit(1)

    print(f"fs class: {type(fs).__module__}.{type(fs).__name__}")
    print(f"path: {path}")


if __name__ == "__main__":
    main()
