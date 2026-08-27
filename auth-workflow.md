# check-cloud-ready — access resolution & assessment dispatch workflow
Describes how the skill
gets from a user-supplied input to an open, authenticated handle on the data,
and then dispatches to format-specific assessment.

## Inputs

The workflow accepts either of two entry points:

1. **Item URL/path** — an `https://` or `s3://` URL (or local path) pointing at
   the dataset/asset directly.
2. **Granule ID** — a CMR granule identifier with no URL.

## Step 1 - local file URL vs HTTPS or S3

- if the input is a granule ID move on to step 2
- if the file URL is a local path, move on to assessment
- if the file is s3 or https, move on to step 3

## Step 2 — Resolve a granule ID to URLs (when no URL is given)

If the input is a granule ID rather than a URL:

- **Query CMR** to resolve the granule to its credentials entrypoint. Implemented
  by `scripts/resolve_granule.py <granule-id>` (present in both skills), which
  fetches `https://cmr.earthdata.nasa.gov/search/concepts/<id>.umm_json` and
  returns the granule's s3 data URL(s) plus the DAAC's `/s3credentials` endpoint.
  Example: `G4289749526-ASF` resolves to
  `https://nisar.asf.earthdatacloud.nasa.gov/s3credentials` for the link with
  description "S3 credentials endpoint for direct in-region bucket access".
- Access the resolved assets via **obstore**, using the resolved endpoint as
  `--credentials-url` on every subsequent script.

```python
from obstore.store import S3Store
from obstore.auth.earthdata import NasaEarthdataCredentialProvider

# Obtain an S3 credentials URL and an S3 data/download URL, typically
# via metadata returned from a NASA CMR collection or granule query.
credentials_url = "https://data.ornldaac.earthdata.nasa.gov/s3credentials"
data_url = (
    "s3://ornl-cumulus-prod-protected/gedi/GEDI_L4A_AGB_Density_V2_1/data/"
    "GEDI04_A_2024332225741_O33764_03_T01289_02_004_01_V002.h5"
)
data_prefix_url, filename = data_url.rsplit("/", 1)

# Since no NASA Earthdata credentials are specified in this example,
# environment variables or netrc will be used to locate them in order to
# obtain S3 credentials from the URL.
cp = NasaEarthdataCredentialProvider(credentials_url)

store = S3Store.from_url(data_prefix_url, credential_provider=cp)

# Download the file by streaming chunks
try:
    result = store.get(filename)
    with open(filename, "wb") as f:
        for chunk in iter(result):
            f.write(chunk)
finally:
    cp.close()
```

- For the object-store endpoint, absent a granule ID: **guess from the
  whitelist, or let the user provide the endpoint explicitly** — see Step 3.

The resolved URL then joins the main flow below.

## Step 3 — Public or protected?

Decision: **is the item URL publicly accessible?**

- **Public → go straight to Assess.** No credential setup; open the store
  (obstore) and proceed.
- **Not public → ask for Earthdata credentials** (`EARTHDATA_TOKEN`, or
  `EARTHDATA_USERNAME`/`EARTHDATA_PASSWORD`, or `~/.netrc` — never typed into
  the conversation), then branch on protocol.

## Step 3b — Protected access, by protocol (HTTP vs S3)

- **HTTP**: authenticate with an **`Authorization: Bearer <token>`** header
  (EDL bearer token minted from the Earthdata credentials via
  `nasa_s3.edl_bearer_token()`). This retry is **host-gated**: the token is
  only ever sent when the response redirected to `urs.earthdata.nasa.gov`, or
  the URL itself looks like a NASA Earthdata host (per
  `nasa_s3.looks_like_nasa_earthdata`) — a 401/403 from an unrelated host is
  never answered with the user's EDL credential.
- **S3**: never try HTTPS as a substitute — implemented ask, in order:
  1. **Granule ID?** If the user has one (or it was already resolved in Step
     2), use it — go back to Step 2 / pass `--credentials-url` from that
     resolution. This is the recommended path.
  2. **Explicit endpoint?** If they have the DAAC's `/s3credentials` URL
     directly, use it as `--credentials-url`.
  3. **Whitelist guess.** Neither of the above: offer a guess from
     `KNOWN_CREDENTIALS_ENDPOINTS` in `scripts/nasa_s3.py` (both skills), and
     have the user confirm which one looks right. This whitelist is
     implemented — it is no longer a "perhaps we should" idea.
  4. **earthaccess fallback (opt-in only).** If none of the above resolves it
     and the user explicitly chooses to, fall back to earthaccess: ask which
     **DAAC** issues the credentials (known or guessed via CLI flag
     `--earthaccess-fallback`), then obtain temporary S3 credentials via
     **earthaccess**.

  All of 1–3 consume obstore's `NasaEarthdataCredentialProvider` directly
  (`scripts/nasa_s3.py`); CLI flags across the scripts are `--granule-id`,
  `--credentials-url`, and `--earthaccess-fallback`.

  **Failure classification** (never FAILED — always SKIPPED, since these are
  access-environment facts, not dataset defects):
  - `nasa-credentials-required` — the URL is NASA Earthdata S3 and no
    credentials were supplied at all. Fix: pass `--granule-id` or
    `--credentials-url` (recommended), or `--earthaccess-fallback`.
  - `credentials-endpoint-auth` — EDL/URS rejected the credentials-endpoint
    request (bad/missing/expired EDL credentials). Fix: check
    `EARTHDATA_TOKEN` / `EARTHDATA_USERNAME`+`EARTHDATA_PASSWORD` / `~/.netrc`.
  - `in-region-only` — S3 credentials minted successfully but the bucket/object
    read was denied. Expected outside AWS us-west-2; re-run from in-region
    compute. The scorecard's hosting row still PASSes; byte-range/chunking
    criteria become "not assessed — requires in-region compute."

## Step 4 — Assess: dispatch by format

With an open handle on the data, dispatch to a format-specific assessment
path:

| Format | Opened / validated with | Check |
|---|---|---|
| HDF5 / NetCDF | h5py / xarray | chunking, compression, structure |
| Zarr | (xarray/zarr) | **GeoZarr** spec conformance; chunking, compression, structure |
| COG | rasterio | **valid COG** check |
| COPC | pdal | **valid COPC** check |

Across the raster/array formats (NetCDF/HDF5, Zarr, and where applicable COG),
run **CF conventions checks via xarray** — the "CF (xarray)" bracket on the
board spans these branches.

## Flow summary

```
granule id ──▶ resolve_granule.py (CMR) ──▶ credentials_url + s3 URL(s) ──▶ obstore ──┐
                                                                                       │
item URL/path ──▶ public? ── yes ─────────────────────────────────────────────▶   ASSESS
                     │                                                                ▲
                     no                                                               │
                     ▼                                                                │
        EDL credentials present? (EARTHDATA_TOKEN / USERNAME+PASSWORD / ~/.netrc)     │
                     │                                                                │
              ┌── protocol? ──┐                                                       │
            HTTP              S3 (never HTTPS fallback)                               │
              │                │                                                      │
      Bearer token       granule id → Step 2, or endpoint (known/whitelist-guessed)   │
      (host-gated:       → obstore NasaEarthdataCredentialProvider,                   │
      URS/NASA host      or opt-in earthaccess fallback (daac known/guessed)          │
      only)                    │                                                      │
              │           credentials-endpoint-auth / in-region-only → SKIPPED        │
              │                │                                                      │
              └──────▶ file handle ◀──────────────────────────────────────────────────┘

ASSESS ──▶ HDF5/NetCDF ─▶ h5py/xarray ─┐
      ├──▶ Zarr ────────▶ GeoZarr      ├─ CF checks (xarray)
      ├──▶ COG ─────────▶ rasterio (valid COG)
      └──▶ COPC ────────▶ pdal (valid COPC)
```
