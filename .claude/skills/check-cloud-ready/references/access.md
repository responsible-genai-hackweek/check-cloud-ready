# Access resolution & authentication

This is the CLI's actual decision flow (`access/workflow.py`, `access/nasa_s3.py`,
`access/resolve_granule.py`, `access/probe.py`) — not a script you run yourself. The
CLI resolves access automatically from whatever you pass it; this doc explains what
it did and why, and what to set up when it can't.

## Credentials: the rules

**Never paste usernames, passwords, or tokens into a chat/agent conversation** — the
transcript persists. Configure one of, and the CLI (never the agent) reads it:

- `EARTHDATA_TOKEN` — an Earthdata Login (EDL) bearer token.
- `EARTHDATA_USERNAME` + `EARTHDATA_PASSWORD` — env vars.
- `~/.netrc` (chmod 600):
  ```
  machine urs.earthdata.nasa.gov
      login YOUR_USERNAME
      password YOUR_PASSWORD
  ```

`access/nasa_s3.edl_bearer_token()` checks these in exactly that order and never logs
or echoes a secret. If none is configured, the CLI records a `D2-auth` finding with
setup guidance and stops — it never prompts for a password in-band. AWS credentials
(for non-NASA protected S3) follow the standard chain: env vars, `~/.aws/credentials`,
instance role — also never typed in chat.

## The decision flow

Given an input (`INPUT` positional argument — a local path, an `https://`/`s3://`
URL, or a CMR granule concept ID), `access.workflow.resolve_access` does:

1. **Classify the input** (`formats.detect_input`). A local path skips network
   entirely (hosting/access can't be assessed from a copy on disk — `D0-hosting`
   skipped, with a remediation to point the CLI at the cloud-hosted URL instead).
   `--no-network` short-circuits everything to a `D0-network` skipped finding,
   regardless of input kind.
2. **Granule ID → CMR first.** A granule ID (matching `G<digits>-<PROVIDER>`, or
   passed via `--granule-id`) is resolved through
   `https://cmr.earthdata.nasa.gov/search/concepts/<id>.umm_json` to its S3 data
   URL(s) and its DAAC's `/s3credentials` endpoint (`access/resolve_granule.py`).
   Multiple S3 assets → the CLI asks which to assess (or takes the first
   non-interactively); no S3 URL at all → falls back to the granule's HTTPS URL with
   a note. The raw UMM-JSON is carried through as `cmr_umm` so the scorer can derive
   license/DOI/contact/keywords evidence later (see `references/rubric.md`,
   Dimension E) — this is CMR metadata standing in for a STAC/Croissant catalog,
   which this CLI does not consume.
3. **Probe anonymously first**, always, for both `s3://` and `https://` inputs
   (`access/probe.py`). Public data needs no credential setup at all — a successful
   anonymous probe becomes a `pass` `D2-auth` finding and the run proceeds
   immediately. `--anon` forces anonymous S3 access even when the probe looked
   protected (recorded as a `warn`, not silently upgraded to `pass`).
4. **Protected `s3://`**: resolved via, in priority order —
   - an explicit `--granule-id` (re-enters step 2);
   - an explicit `--credentials-url` (a DAAC `/s3credentials` endpoint), via
     `obstore`'s `NasaEarthdataCredentialProvider`;
   - if the bucket looks like NASA Earthdata Cloud (`nasa_s3.looks_like_nasa_earthdata`
     — a hostname-keyword heuristic covering `earthdatacloud`, `daac`, `cumulus`,
     `podaac`, `lpdaac`, `gesdisc`, `nsidc`, `asf`, `ornl`, `ghrc`, `laads`, `obdaac`,
     `nisar`, `opera`) or the user confirms it is: ask for a granule ID, or offer a
     table of known DAAC credentials endpoints to pick from;
   - `--earthaccess-fallback` (or an interactive yes): `earthaccess.login()` +
     `get_s3_filesystem()`, inferring the DAAC from the URL if no endpoint is known.

   **There is no S3→HTTPS rewrite anywhere in this flow, ever** — a non-NASA
   protected bucket that isn't confirmed as NASA Earthdata data gets a generic
   "AWS credentials or requester-pays" finding, not an HTTPS retry. NASA Earthdata
   Cloud protected buckets are only correctly readable via S3, in-region
   (us-west-2), with minted credentials; rewriting to HTTPS would silently defeat
   the point of an in-region cloud read and would misrepresent the access model for
   any other protected bucket too.
5. **Protected `https://`**: an EDL bearer token (see above) is minted and sent as
   `Authorization: Bearer <token>`. Detected via a 401/403, or a redirect naming
   `urs.earthdata.nasa.gov`, at either the HEAD or the ranged-GET stage.

## Error classification: reading an S3 failure correctly

`access/nasa_s3.classify_s3_error` reads the exception (and its chain) to distinguish
two very different S3 failure modes, both surfaced as a `D2-auth` finding:

- **`credentials-endpoint-auth`** — a 401/403 alongside `urs.earthdata` /
  `s3credentials` / "earthdata login" in the error text: EDL credentials are missing,
  expired, or lack access to this DAAC's `/s3credentials` endpoint. Fixable from
  anywhere. Remediation: check `EARTHDATA_TOKEN` / `EARTHDATA_USERNAME`+
  `EARTHDATA_PASSWORD` / `~/.netrc`.
- **`in-region-only`** — "AccessDenied"/"Forbidden" with **no** EDL/credentials-endpoint
  marker in the text: credentials were minted successfully, but the bucket/object
  read was denied anyway. This almost always means the request isn't running inside
  AWS us-west-2. Remediation: re-run from in-region compute. This is **not** a
  hosting failure — the bucket's restricted-egress model is a hosting choice, not a
  defect — but it is a real constraint worth recording plainly rather than as a raw
  stack trace.

## Range-request support (`D1-range`)

`access/probe.py` sends `Range: bytes=0-1023` against `https://`/`s3://` (S3 always
supports ranges; the HTTPS case is the real test). One important fix baked into the
classifier (`classify_access_failure`): a 401/403, or a redirect/marker pointing at
Earthdata Login, **at any stage — including the ranged GET itself** — is classified
`auth-required`, never `no-range-support`. NASA's TEA HTTPS distribution answers HEAD
anonymously but gates the ranged GET behind EDL; treating that as "no range support"
would misdiagnose an auth gap as a format/hosting defect. Only a non-auth, non-206
response to an actual range request means the server genuinely ignores ranges.

## Auth documentation as a D2 signal

For a CMR-resolved granule, the existence of a DAAC-documented `/s3credentials`
endpoint (found in the granule's UMM-JSON `RelatedUrls`) itself counts as "auth
clearly documented" evidence for Dimension D2 — see `references/rubric.md`.
