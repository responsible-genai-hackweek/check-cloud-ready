# Access probing and authentication

## Credentials: the rules

Never ask the user to paste usernames, passwords, or long-lived tokens into the
conversation — the transcript persists. Instead:

**Earthdata Login (EDL)** — in order of precedence:

1. `EARTHDATA_TOKEN` environment variable (a bearer token).
2. `EARTHDATA_USERNAME` / `EARTHDATA_PASSWORD` environment variables.
3. `~/.netrc`:

```
# ~/.netrc  (chmod 600)
machine urs.earthdata.nasa.gov
    login YOUR_USERNAME
    password YOUR_PASSWORD
```

This is exactly the order obstore's `NasaEarthdataCredentialProvider` resolves
credentials in — the scripts in this skill use it directly, so setting up any one
of the three is sufficient. If none is set, pause, show the user the snippet
above, and resume when they say it's in place. Verify with a probe, not by asking
them to echo secrets.

`earthaccess` is a **fallback only, at the user's explicit choice** (Step 1,
option (b)) — not the default path. See "earthaccess fallback" below.

**AWS** — standard chain: env vars, `~/.aws/credentials`, instance profile.
Never ask for key material in chat.

## NASA Earthdata S3 — the recommended path

The most reliable way to get NASA Earthdata Cloud S3 access is via a CMR granule
concept ID, not a hardcoded DAAC table:

1. Granule concept ID (e.g. `G4289749526-ASF`) →
   `python3 scripts/resolve_granule.py <id>` → fetches
   `https://cmr.earthdata.nasa.gov/search/concepts/<id>.umm_json`.
2. In the granule's `RelatedUrls`, exactly one entry ends in `/s3credentials` — the
   DAAC's S3-credentials endpoint. This is carried in CMR metadata per granule, so
   it's more reliable than earthaccess's built-in DAAC table (which is missing
   endpoints for some DAACs/missions — NISAR's
   `https://nisar.asf.earthdatacloud.nasa.gov/s3credentials`, for example, is not
   in earthaccess's table).
3. Use it directly with obstore:

```python
from obstore.auth.earthdata import NasaEarthdataCredentialProvider
from obstore.store import S3Store

cp = NasaEarthdataCredentialProvider(credentials_url)
store = S3Store.from_url(f"s3://{bucket}", credential_provider=cp)
# or, for fsspec-shaped consumers (zarr, h5py, ...):
# from obstore.fsspec import FsspecStore
# fs = FsspecStore("s3", credential_provider=cp)
```

Credentials auto-refresh per call — no manual renewal needed. Minted keys are
usable only from AWS **us-west-2** compute; see below.

## What "requires auth" looks like on the wire

- **EDL-protected HTTPS**: GET redirects (302/307) to `urs.earthdata.nasa.gov`,
  or returns 401. This is a *positive detection* of EDL — report "EDL required",
  don't call it broken.
- **S3 anonymous denied**: 403 AccessDenied on unsigned request. Could mean:
  credentials required, requester-pays (`x-amz-request-payer` needed), or a
  bucket policy restricting to in-region/VPC access. Try signed if credentials
  exist; report which combination succeeded.

## S3 region and the in-region-only case

Expect this and explain it well — it's the most common confusing failure:

- **Wrong-region endpoint**: 301 PermanentRedirect with the correct region in the
  `x-amz-bucket-region` header (also on a HEAD). Retry against the right region
  before concluding anything.
- **NASA Earthdata "direct S3"** buckets are only accessible from compute
  *in the same AWS region* (typically us-west-2), using the DAAC's temporary
  credentials. From anywhere else, expect 403 even with valid credentials — this
  is expected, not a failure. Tell the user: "the S3 URL is valid and credentials
  minted successfully; access was denied because this request isn't running in
  us-west-2." The scorecard's hosting row still PASSes — restricted-egress hosting
  is a hosting *model*, not a failure — but the byte-range/chunking criteria become
  "not assessed — requires in-region (us-west-2) compute; re-run there with
  `--credentials-url`". **Never reroute NASA S3 assessment to HTTPS** — bearer-token
  HTTPS access is for HTTPS *inputs* only, never a substitute for an s3:// input.
- Region mismatch is also a *performance* finding: if the provider's stated
  audience computes in region X but the bucket lives in region Y, flag it.

## Range-request support

The load-bearing probe. Send `GET` with `Range: bytes=0-1023`:

- **206 Partial Content** with `Content-Range` → ranges work.
- **200 with full body** → server ignores ranges: criterion 2 FAILs for
  file-based formats regardless of format quality. (For Zarr stores, per-object
  GETs are the subsetting mechanism, so whole-object 200s are fine; range support
  *within* objects matters only for sharded stores.)
- Also record `Accept-Ranges` from HEAD, but trust the actual 206 test over the
  header — proxies lie in both directions.
- Some portals (e.g. OPeNDAP endpoints, on-the-fly-zipping downloads) serve
  ranges but with server-side cost, chunked transfer encoding, or no
  Content-Length; note anomalies.

## Timeouts and flakiness

Probe with short timeouts (10 s connect) and one retry. Distinguish in the
report: DNS failure (URL wrong/internal), connect timeout (network path/region
blocked), 5xx (server side). Don't burn minutes retrying — one clean diagnosis
beats ten timeouts, and "the endpoint was unreachable from the assessment
environment" is itself a valid finding with the environment named.

## earthaccess fallback

Used only when the user explicitly opts in (Step 1, option (b)) — never the
default path. Given a DAAC short name or a known/guessed credentials endpoint:

```python
import earthaccess
earthaccess.login()
fs = earthaccess.get_s3_filesystem(endpoint=credentials_url)  # or daac=daac_short_name
```

On failure, have the user verify, in order:

1. The DAAC for this dataset.
2. The `/s3credentials` endpoint (a CMR granule ID resolves this exactly, more
   reliably than earthaccess's built-in DAAC table — see `resolve_granule.py`).
3. EDL credentials are actually present (`EARTHDATA_TOKEN`,
   `EARTHDATA_USERNAME`/`EARTHDATA_PASSWORD`, or `~/.netrc`).

Never fall back to HTTPS for NASA S3 data if earthaccess fails — the fix is one
of the three checks above, not a different protocol.
