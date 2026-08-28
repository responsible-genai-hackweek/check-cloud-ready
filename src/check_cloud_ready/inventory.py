"""Variable inventory: matching (S10 fix) and heuristic suggestion ranking.

Pure functions over "inventory records" (Ruling I-1 — a fixed contract
produced by openers.py, Task 6):

    {"name": str,        # full path for HDF5 ("/science/LSAR/GCOV/grids/frequencyA/HHHH"),
                          # key for zarr
     "dims": [str],      # may be [] / positional placeholders like "dim_0" when unnamed
     "shape": [int],
     "dtype": str,       # numpy dtype string
     "chunks": [int] | None,
     "attrs": dict,       # sampled attrs (may be {})
     "size_bytes": int | None,
     "codec": str | None}

No I/O, no network — everything here operates on already-collected
records.

``match_variables`` is the S10 fix: the earth-science-cloud-readiness
skill's ``--variables`` matching only did substring matching on the
full name. This applies the same substring semantics but folds in
exact full-path, leading-slash-insensitive full-path, and basename
matching too, uniformly (every token is checked against every
candidate the same way; there is no "prefer exact over substring"
special-casing) — and a token matching nothing raises with the full
list of available names rather than silently returning no results.
"""
from __future__ import annotations

import math

# --------------------------------------------------------------------- errors


class VariableMatchError(Exception):
    """Raised by match_variables when a requested token matches
    nothing in the inventory."""


# -------------------------------------------------------------------- matcher

def _basename(name: str) -> str:
    return name.rsplit("/", 1)[-1]


def _token_matches(name: str, token: str) -> bool:
    """Does `token` match inventory var `name`?

    Uniform OR of four criteria — every token is checked against every
    name the same way:
      - exact full name
      - full path, insensitive to a leading "/" on either side
      - basename (last "/" segment)
      - case-insensitive substring anywhere in the full name
    """
    if name == token:
        return True
    if name.lstrip("/") == token.lstrip("/"):
        return True
    if _basename(name) == token:
        return True
    if token.lower() in name.lower():
        return True
    return False


def match_variables(inventory: list[dict], requested: list[str]) -> list[dict]:
    """Resolve requested name/path/substring tokens against an
    inventory. Returns matched records in inventory order, deduplicated
    across tokens. Raises VariableMatchError, listing all available
    names, for any token that matches nothing.
    """
    available_names = [v["name"] for v in inventory]

    matched_names: set[str] = set()
    for token in requested:
        token_matches = [v for v in inventory if _token_matches(v["name"], token)]
        if not token_matches:
            if available_names:
                raise VariableMatchError(
                    f"no variables matched '{token}'; available: "
                    + ", ".join(available_names)
                )
            raise VariableMatchError(
                f"no variables matched '{token}'; no variables available"
            )
        matched_names.update(v["name"] for v in token_matches)

    return [v for v in inventory if v["name"] in matched_names]


# ------------------------------------------------------------------- ranking

_QA_PATTERNS = ("qa", "flag", "quality", "mask")
_BOUNDS_PATTERNS = ("bnds", "bounds")
_CRS_PATTERNS = ("crs", "projection", "spatial_ref")
_COMMON_COORD_NAMES = {
    "time", "lat", "latitude", "lon", "longitude", "x", "y", "z",
    "level", "lev", "plev", "depth", "height",
}


def _matches_any_pattern(text: str, patterns) -> bool:
    low = text.lower()
    return any(p in low for p in patterns)


def _is_string_or_bool_dtype(dtype: str | None) -> bool:
    if not dtype:
        return False
    return "|S" in dtype or "<U" in dtype or "bool" in dtype.lower()


def _is_excluded(v: dict) -> bool:
    """QA/flag/quality/mask, bounds, and CRS/grid-mapping patterns are
    matched as plain case-insensitive substrings of the FULL name/path
    — a keyword in a parent group segment (e.g.
    "/science/LSAR/GCOV/quality/pixelValidityMap") excludes the
    variable just as surely as a keyword in the leaf itself would,
    since the path segment is where the provider's intent ("this
    subtree is QA data") actually lives. Only the coordinate-variable
    rule (name equals one of its own dims, or is a common coordinate
    name) is scoped to the basename — a coordinate is identified by
    its own leaf name, not by which group happens to contain it.

    This is plain substring matching, not word-boundary matching (no
    regex \\b anchoring): "bounds" only matches names that contain the
    literal run "b-o-u-n-d-s", so e.g. "boundaryLayerHeight" ("bound"
    then "ary", never "bounds") is untouched by the bounds rule. That
    happens to hold for all patterns here (none is a short prefix of a
    common unrelated English word in the way "bounds" almost is), so
    plain substring matching does not need word-boundary logic to
    avoid false positives for these particular tokens.
    """
    name = v.get("name", "")
    base = _basename(name)
    dims = v.get("dims") or []
    shape = v.get("shape") or []

    if _matches_any_pattern(name, _QA_PATTERNS):
        return True
    if _matches_any_pattern(name, _BOUNDS_PATTERNS):
        return True
    if _matches_any_pattern(name, _CRS_PATTERNS):
        return True
    if base in dims:
        return True
    if base.lower() in _COMMON_COORD_NAMES:
        return True
    if _is_string_or_bool_dtype(v.get("dtype")):
        return True
    if len(shape) == 0:
        return True
    return False


def _element_count(v: dict) -> int:
    shape = v.get("shape") or []
    if not shape:
        return 0
    return math.prod(shape)


def rank_variables(inventory: list[dict]) -> list[dict]:
    """Deterministic heuristic ranking of inventory records for
    "suggest a variable" prompts. Excludes QA/flag/quality/mask
    variables, bounds variables, CRS/grid-mapping containers,
    coordinate variables, string/bool dtypes, and 0-d scalars.
    Remaining records are ranked by number of dims (more first), then
    total element count (larger first), then name (lexicographic, for
    determinism).
    """
    candidates = [v for v in inventory if not _is_excluded(v)]
    return sorted(
        candidates,
        key=lambda v: (-len(v.get("shape") or []), -_element_count(v), v["name"]),
    )


# -------------------------------------------------------------------- display

def parse_selection(inventory: list[dict], raw: str) -> list[str]:
    """Task 12 CLI helper: turn a raw ``--variables``-prompt answer
    (comma-separated mix of 0-based ``format_inventory_table`` indices
    and ``match_variables``-style name/path/substring tokens, or the
    literal ``"all"``) into a list of tokens suitable for
    ``match_variables``. Pure/no I/O, kept here since it is inventory
    selection semantics, not CLI orchestration.
    """
    raw = raw.strip()
    if raw.lower() == "all":
        return [v["name"] for v in inventory]
    tokens = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if part.isdigit() and int(part) < len(inventory):
            tokens.append(inventory[int(part)]["name"])
        else:
            tokens.append(part)
    return tokens


def format_inventory_table(inventory: list[dict]) -> str:
    """Numbered plain-text table (index, name, dims x shape, dtype) for
    the CLI's --variables prompt. Deliberately dumb: no column
    alignment logic beyond simple padding.
    """
    lines = []
    for i, v in enumerate(inventory):
        dims = ",".join(v.get("dims") or [])
        shape = ",".join(str(n) for n in (v.get("shape") or []))
        lines.append(
            f"{i}  {v.get('name', '')}  ({dims})×({shape})  {v.get('dtype', '')}"
        )
    return "\n".join(lines)
