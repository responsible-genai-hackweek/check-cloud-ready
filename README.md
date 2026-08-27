# check-cloud-ready

An [Agent Skill](https://code.claude.com/docs/en/skills) that assesses the **cloud
readiness** of an earth science dataset — is it on object storage, randomly
accessible via HTTP range requests, sensibly chunked (~8–16 MB compressed),
well-compressed, and standards-conformant (CF / GeoZarr)? It produces a
verdict-first scorecard report with concrete recommendations for data providers
who can change file structure and embedded metadata.

Supported formats: HDF4 (short-circuits — no random access), HDF5, NetCDF-3/4,
Zarr v2/v3, Icechunk, VirtualiZarr/Kerchunk references. COG, GeoParquet, COPC,
and GRIB2 are recognized but not yet assessed.

## Layout

```
check-cloud-ready/
├── SKILL.md              # the workflow the agent follows
├── references/           # chunking targets, compression grid, format notes,
│                         # CF/GeoZarr checks, auth & access-error guidance
└── scripts/              # deterministic probes the agent runs
    ├── probe_access.py       # reachability, EDL detection, range support, S3 region
    ├── detect_format.py      # magic bytes + store-layout format ID
    ├── chunk_report.py       # per-variable chunk geometry + sampled compressed sizes
    └── compression_bench.py  # codec grid benchmark on sampled chunks
```

The `SKILL.md` format (YAML frontmatter with `name` + `description`, markdown
body, optional bundled files) is portable across agents that have adopted Agent
Skills — installation is just placing this folder where your agent looks for
skills.

## Install

If you received this as a `check-cloud-ready.skill` file: it's a zip archive —
`unzip check-cloud-ready.skill` yields the `check-cloud-ready/` folder used below.

### Claude Code

Personal (all your projects):

```bash
mkdir -p ~/.claude/skills
cp -r check-cloud-ready ~/.claude/skills/
```

Or per-project (checked into the repo, shared with your team):

```bash
mkdir -p .claude/skills
cp -r check-cloud-ready .claude/skills/
```

Claude Code picks up new skills in-session (no restart if `~/.claude/skills/`
already existed). Verify with `/skills`, then invoke:

```
/check-cloud-ready s3://bucket/path/to/dataset.zarr
```

or just ask in natural language ("is this dataset cloud optimized? <url>") —
the skill triggers on the description. Claude.ai / Cowork sessions don't read
`~/.claude/skills/`; for those, upload the `.skill` file in a conversation and
save it, or enable it under **Settings → Skills** on claude.ai.

### OpenAI Codex CLI

```bash
mkdir -p ~/.codex/skills
cp -r check-cloud-ready ~/.codex/skills/
codex --enable skills
```

(Early Codex releases gate skills behind `--enable skills`; newer builds may
have them on by default — see `codex --help`.) Then ask Codex to
"check cloud readiness of <url>" or reference the skill by name.

### Google Antigravity

Per-workspace:

```bash
mkdir -p .agents/skills        # older builds: .agent/skills
cp -r check-cloud-ready .agents/skills/
```

Global (all workspaces):

```bash
mkdir -p ~/.gemini/config/skills
cp -r check-cloud-ready ~/.gemini/config/skills/
```

The agent discovers the skill from its frontmatter description at conversation
start; mention it by name to force activation.

### Any-agent install via the skills CLI

Once this skill lives in a public git repo, the
[skills CLI](https://skills.sh) installs it into whichever agents it detects
(Claude Code, Codex, Cursor, Antigravity, and others):

```bash
npx skills add <github-repo-url> --skill check-cloud-ready
```

## Runtime dependencies

The agent installs these lazily as the workflow needs them (each script names
its missing imports):

```bash
pip install requests boto3 s3fs fsspec xarray zarr h5py netCDF4 numcodecs zstandard obstore
```

Optional (fallback only): `earthaccess` — only needed if the user explicitly
opts into the earthaccess fallback path instead of obstore.

Optional: `compliance-checker` (formal CF check), `cf_xarray`,
`xbitinfo` (lossy bit-rounding analysis),
`vzviz` (chunk-layout visualization for VirtualiZarr manifests).

## Credentials

The skill never asks for credentials in the conversation. For NASA Earthdata
Login (EDL), set `EARTHDATA_TOKEN` (a bearer token), or
`EARTHDATA_USERNAME`/`EARTHDATA_PASSWORD` env vars, or `~/.netrc`
(`machine urs.earthdata.nasa.gov login ... password ...`, `chmod 600`) — never
typed into the conversation.

The recommended entrypoint for NASA Earthdata data is a CMR granule concept ID
(e.g. `G4289749526-ASF`): one CMR lookup (`scripts/resolve_granule.py`) yields
both the granule's s3 URL(s) and the DAAC's `/s3credentials` endpoint, which
obstore's `NasaEarthdataCredentialProvider` consumes directly to mint S3
credentials. `earthaccess` is an optional, user-chosen fallback, not the
default path. Minted S3 keys work only from in-region (us-west-2) compute —
anywhere else, expect access denied even with valid credentials.

AWS credentials follow the standard chain (env vars, `~/.aws/credentials`,
instance profile).

## What you get

A `cloud-readiness-<dataset>.md` scorecard: a one-line verdict
(READY / READY WITH CAVEATS / NOT READY), a per-criterion table with measured
evidence, findings for every non-PASS row, and provider recommendations ordered
by impact.
