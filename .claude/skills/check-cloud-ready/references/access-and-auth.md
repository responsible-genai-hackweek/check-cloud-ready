# Access probing and authentication

## Credentials: the rules

Never ask the user to paste usernames, passwords, or long-lived tokens into the
conversation — the transcript persists. Instead:

**Earthdata Login (EDL)** — either of:

```
# ~/.netrc  (chmod 600)
machine urs.earthdata.nasa.gov
    login YOUR_USERNAME
    password YOUR_PASSWORD
```

or environment variables `EARTHDATA_USERNAME` / `EARTHDATA_PASSWORD`. Then
`earthaccess.login()` picks them up automatically and can mint HTTPS bearer
tokens and temporary in-region S3 credentials (`earthaccess.get_s3_credentials()`
per DAAC). If neither is set, pause, show the user the snippet above, and resume
when they say it's in place. Verify with a probe, not by asking them to echo
secrets.

**AWS** — standard chain: env vars, `~/.aws/credentials`, instance profile.
Never ask for key material in chat.

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
  credentials. From anywhere else, expect 403 — tell the user: "this is expected;
  the S3 URL is valid but designed for in-region use. From here I can assess via
  the HTTPS (EDL) endpoint; the S3 result would be identical structurally." The
  scorecard's hosting row still PASSes — restricted-egress hosting is a hosting
  *model*, not a failure — but record the constraint.
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
