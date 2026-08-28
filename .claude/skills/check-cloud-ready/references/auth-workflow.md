# check-cloud-ready — access resolution & assessment dispatch workflow
Describes how the skill gets from a user-supplied input to an open, authenticated handle on the data,
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

- **Query CMR** to resolve the granule to its credentials entrypoint. Example: https://cmr.earthdata.nasa.gov/search/concepts/G4289749526-ASF.umm_json points to "https://nisar.asf.earthdatacloud.nasa.gov/s3credentials" for the link with description "S3 credentials endpoint for direct in-region bucket access"
- Access the resolved assets via **obstore**.

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

- For the object-store endpoint: **guess the Earthdata endpoint, or let the
  user provide the endpoint explicitly** (endpoint provision is the escape
  hatch when guessing fails).

The resolved URL then joins the main flow below.

## Step 3 — Public or protected?

Decision: **is the item URL publicly accessible?**

- **Public → go straight to Assess.** No credential setup; open the store
  (obstore) and proceed.
- **Not public → ask for Earthdata credentials** (username + password, e.g.
  `EARTHDATA_USERNAME`/`EARTHDATA_PASSWORD` or `~/.netrc`), then branch on
  protocol.

## Step 3 — Protected access, by protocol (HTTP vs S3)

- **HTTP**: authenticate with an **`Authorization: Bearer <token>`** header
  (EDL bearer token minted from the Earthdata credentials).
- **S3**: 
  - Use S3, don't try HTTPS
  - ask if they want to use obstore or earthaccess
  - earthaccess
    - Which **DAAC** issues the credentials — do we know it, or do we **guess**?
    - obtain temporary S3 credentials via **earthaccess (EA)**
  - obstore
    - ask if they have the granule id, if so go back to step 2
    - if not, ask if they have the nasa endpoint for credentials to generate with obstore or want the agent to guess

Note: in cases where the agent is guessing perhaps we can compile a whitelist of earthdata credentials endpoints to use and ask the user which one looks right.

## Step 4 — Assess: dispatch by format

With an open handle on the data, dispatch to a format-specific assessment
path:

| Format | Opened / validated with | Check |
|---|---|---|
| HDF5 / NetCDF | h5py / xarray | chunking, compression, structure |
| Zarr | (xarray/zarr) | **GeoZarr** spec conformance | chunking, compression, structure |
| COG | rasterio | **valid COG** check |
| COPC | pdal | **valid COPC** check |

Across the raster/array formats (NetCDF/HDF5, Zarr, and where applicable COG),
run **CF conventions checks via xarray** — the "CF (xarray)" bracket on the
board spans these branches.

## Flow summary

```
granule id ──▶ query CMR ──▶ obstore ─────────────────────────┐
   (guess ED endpoint, or user provides endpoint)             │
                                                              ▼
item URL/path ──▶ public? ── yes ───────────────────────▶  ASSESS
                     │                                        ▲
                     no                                       │
                     ▼                                        │
        set Earthdata credentials (user/pass)                 │
                     │                                        │
              ┌── protocol? ──┐                               │
            HTTP              S3                              │
              │                │                              │
      Bearer token      earthaccess (daac?) or obstore (granule id or endpoint) │
              │                                               │
              └──────▶ file handle ◀──┘                       │
                          └───────────────────────────────────┘

ASSESS ──▶ HDF5/NetCDF ─▶ h5py/xarray ─┐
      ├──▶ Zarr ────────▶ GeoZarr      ├─ CF checks (xarray)
      ├──▶ COG ─────────▶ rasterio (valid COG)
      └──▶ COPC ────────▶ pdal (valid COPC)
```
