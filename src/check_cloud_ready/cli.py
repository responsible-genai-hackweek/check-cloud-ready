"""check-cloud-ready CLI: the interactive orchestrator.

Wires together every module built in Tasks 1-11 into one run: resolve
access -> detect format -> open the dataset -> pick variable(s) ->
assess chunking/compression/conventions -> score -> render a report.
Deliberately orchestration-thin (per the task brief): this module calls
each library module's already-tested public API in sequence and
assembles their outputs into the Ruling I-4 ``findings`` shape;
non-trivial logic (format sniffing glue aside -- see ``_sniff_format``,
which is literally "call formats.py's primitives in the order the
brief's step 3 documents") lives in the sibling modules, not here.

Two interface gaps flagged by Task 11's review are bridged here, not
upstream (see each site below for detail):

1. ``openers.OpenResult`` returns ``metadata_walk`` as a sibling of
   ``telemetry``, but ``rendering.py`` expects it nested inside
   ``telemetry`` -- re-nested when the asset dict is assembled.
2. ``scoring.score()`` returns ``verdict``/``verdict_reason``/
   ``confidence``/``score``/``tier`` per-asset, but ``rendering.py``
   reads them from the top level of ``findings`` -- lifted explicitly
   when ``findings`` is assembled.

Exit codes: 0 ok (including a NOT READY verdict -- the assessment
itself still succeeded), 2 fatal access failure, 3 unresolved format,
4 variable match failure, 5 unexpected/other.
"""
from __future__ import annotations

import argparse
import json
import platform
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import __version__
from . import chunking, compression, conventions, formats, inventory, llm_suggest
from . import openers, rendering, scoring, telemetry
from .access import workflow
from .prompts import MissingInputError, NonInteractivePrompter, Prompter

__all__ = ["main"]


# ------------------------------------------------------------------ helpers

def _supported_formats() -> list[str]:
    return sorted(formats.CLOUD_NATIVE | formats.CLOUD_OPTIMIZABLE | formats.CLOUD_HOSTILE)


def _out_dirname(raw_input: str) -> str:
    base = raw_input.rstrip("/").rsplit("/", 1)[-1] or "asset"
    return re.sub(r"[^A-Za-z0-9._-]", "-", base)


def _local_fs():
    import fsspec
    return fsspec.filesystem("file")


def _sniff_format(access_result) -> str:
    """Step 3's sniffing glue: ranged head-byte read through the
    resolved fs for a single-file input, or ``formats.sniff_store`` for
    a directory-ish/store input. Ties together ``formats.py``'s already
    -tested primitives (``ext_hint``/``sniff_store``/``sniff_bytes``/
    ``refine_hdf5``) in the order the task brief's step 3 documents;
    there is no library-module home for "in what order do I call
    these" -- that ordering is exactly what an orchestrator does.
    """
    fs, path, url = access_result.fs, access_result.path, access_result.url
    eff_fs = fs if fs is not None else _local_fs()

    ext = formats.ext_hint(url or path)
    is_storeish = ext == "zarr" or str(path).rstrip("/").endswith(".zarr")
    if not is_storeish:
        try:
            is_storeish = eff_fs.isdir(path)
        except Exception:
            is_storeish = False

    if is_storeish:
        info = formats.sniff_store(eff_fs, path)
        return info.get("format") or "unknown"

    try:
        with eff_fs.open(path, "rb") as f:
            head = f.read(telemetry.HEADER_READ)

        def offset_reads(offset, size):
            with eff_fs.open(path, "rb") as f2:
                f2.seek(offset)
                return f2.read(size)

        info = formats.sniff_bytes(head, offset_reads=offset_reads)
        fmt = info.get("format") or "unknown"
    except Exception:
        return ext or "unknown"

    if fmt == "hdf5":
        fmt = formats.refine_hdf5(eff_fs, path).get("format", fmt)
    return fmt or ext or "unknown"


def _confirm_format(fmt: str, prompter) -> str:
    supported = _supported_formats()
    if fmt in (None, "unknown"):
        return prompter.ask(
            f"Could not detect a format automatically. Enter one of: "
            f"{', '.join(supported)}", default=None, flag="--format")
    raw = prompter.ask(
        f"Detected: {fmt}. Accept? [Y / type one of: {', '.join(supported)}]",
        default="y", flag="--format")
    raw = raw.strip()
    return fmt if raw.lower() in ("y", "yes", "") else raw


def _decode_attr_value(v):
    if isinstance(v, bytes):
        try:
            return v.decode()
        except Exception:
            return repr(v)
    if isinstance(v, np.ndarray):
        return v.tolist()
    if isinstance(v, np.generic):
        return v.item()
    return v


def _global_attrs(handle, fmt: str) -> dict:
    """Dataset-level attrs for ``conventions.check_cf``'s
    ``global_attrs`` -- openers.py's OpenResult only carries per-variable
    attrs (Ruling I-1), so the root/global attrs are read directly off
    the already-open handle here."""
    if handle is None or fmt not in ("zarr", "icechunk", "hdf5", "netcdf4"):
        return {}
    try:
        return {k: _decode_attr_value(v) for k, v in dict(handle.attrs).items()}
    except Exception:
        return {}


def _pick_sample(handle, fmt: str, rec: dict):
    """One decoded interior, data-bearing chunk for the primary
    selected variable, per compression.py's Ruling I-3 contract
    ("sample via chunking's S4 sampler" is the caller's job). Returns
    None (never raises) when no sample can be obtained -- benchmarking
    is then simply skipped."""
    if fmt not in ("zarr", "icechunk", "hdf5", "netcdf4"):
        return None
    shape, chunks = rec.get("shape"), rec.get("chunks")
    if not shape or not chunks:
        return None
    try:
        key = rec["name"].lstrip("/")
        obj = handle[key] if key else handle
        idx = chunking.pick_interior_chunks(shape, chunks, 1)[0]
        slices = tuple(slice(i * c, min(s, (i + 1) * c)) for i, c, s in zip(idx, chunks, shape))
        decoded = np.asarray(obj[slices])
    except Exception:
        return None
    return decoded if chunking.is_data_bearing(decoded) else None


def _cmr_meta_from(access_result):
    """UMM-JSON -> scoring.py's cmr_meta shape (this translation is
    documented in scoring.py's module docstring as the CLI
    orchestrator's job). None when this input never resolved through
    CMR (access.workflow.AccessResult.cmr_umm is None)."""
    umm = getattr(access_result, "cmr_umm", None)
    if not umm:
        return None
    doi_field = umm.get("DOI")
    doi = doi_field.get("DOI") if isinstance(doi_field, dict) else doi_field
    doc_links = [{"href": u.get("URL")} for u in (umm.get("RelatedUrls") or [])
                 if "DOCUMENTATION" in str(u.get("Type") or "").upper()]
    keywords = umm.get("ScienceKeywords") or []
    description = umm.get("Abstract") or ""
    return {
        "license": umm.get("License"),
        "doi": doi,
        "contact": umm.get("ContactPersons") or umm.get("ContactGroups"),
        "doc_links": doc_links,
        "n_keywords": len(keywords),
        "description_len": len(description),
    }


def _suggest_names(inv_list, args, prompter):
    mode = args.suggest or prompter.choose(
        "Suggestion method:", ["heuristic", "llm"], default="heuristic", flag="--suggest")
    if mode == "llm":
        try:
            return llm_suggest.suggest_variables(inv_list, model=args.llm_model)
        except llm_suggest.LLMSuggestUnavailable as e:
            print(f"note: llm suggestion unavailable ({e}); falling back to heuristic "
                  "ranking", file=sys.stderr)
    ranked = inventory.rank_variables(inv_list)
    return [v["name"] for v in ranked[:1]]


def _select_variables(inv_list, args, prompter):
    if args.all_variables:
        return list(inv_list)
    if args.variables:
        tokens = [t.strip() for t in args.variables.split(",") if t.strip()]
        return inventory.match_variables(inv_list, tokens)
    if not inv_list:
        return []

    if isinstance(prompter, NonInteractivePrompter):
        # `--suggest llm` is honored even non-interactively (it needs no
        # further input): _suggest_names short-circuits on args.suggest
        # and already falls back to the heuristic (with a printed note)
        # on any LLMSuggestUnavailable, so this never raises just
        # because the LLM path was unavailable.
        if args.suggest:
            names = _suggest_names(inv_list, args, prompter)
        else:
            ranked = inventory.rank_variables(inv_list)
            if not ranked:
                prompter.ask(
                    "No --variables given and no rankable variable found in "
                    "this dataset; select one explicitly", default=None, flag="--variables")
            names = [ranked[0]["name"]]
        print(f"note: no --variables given; defaulting to {names!r} (documented "
              "non-interactive default)", file=sys.stderr)
        return inventory.match_variables(inv_list, names)

    ranked = inventory.rank_variables(inv_list)
    default_name = ranked[0]["name"] if ranked else None
    print(inventory.format_inventory_table(inv_list))
    raw = prompter.ask(
        "Select variable(s) to assess (indices/names, comma-separated, 'all', "
        "or '?' to suggest)", default=default_name, flag="--variables")
    if raw.strip() == "?":
        names = _suggest_names(inv_list, args, prompter)
        prompter.confirm(f"Assess {names}?", default=True)
        return inventory.match_variables(inv_list, names)
    tokens = inventory.parse_selection(inv_list, raw)
    return inventory.match_variables(inv_list, tokens)


# --------------------------------------------------------------------- run

def _run(args, prompter) -> int:
    raw_input = args.input
    if raw_input is None:
        raw_input = prompter.ask("Dataset input (path, URL, or granule ID)", flag="INPUT")

    kind = formats.detect_input(raw_input)

    access_result = workflow.resolve_access(
        raw_input, prompter=prompter, anon=args.anon, granule_id=args.granule_id,
        credentials_url=args.credentials_url, earthaccess_fallback=args.earthaccess_fallback,
        no_network=args.no_network)

    if access_result.auth == "error":
        for f in access_result.findings:
            print(f"{f.get('id')}: {f.get('evidence')}", file=sys.stderr)
            if f.get("remediation"):
                print(f"  remediation: {f['remediation']}", file=sys.stderr)
        return 2

    if args.format != "auto":
        fmt = args.format
    else:
        sniffed = _sniff_format(access_result)
        try:
            fmt = _confirm_format(sniffed, prompter)
        except MissingInputError:
            print(f"error: could not determine a format for {raw_input!r}; supported "
                  f"formats: {', '.join(_supported_formats())}", file=sys.stderr)
            return 3

    budget = telemetry.Budget(byte_cap=int(args.byte_cap * 1024 * 1024), time_cap=args.time_cap)
    open_result = openers.open_dataset(fmt, access_result.fs, access_result.path, budget)
    budget.check_stage("open", raise_on_breach=False)

    inv_list = open_result.get("inventory") or []
    try:
        selected = _select_variables(inv_list, args, prompter)
    except inventory.VariableMatchError as e:
        print(f"error: {e}", file=sys.stderr)
        return 4

    handle = open_result.get("handle")
    chunking_out = None
    compression_out = None
    conv = {"cf": None}

    if selected:
        # Finding-4 fix: CF conformance is a FILE-level property --
        # coordinate identification, grid-mapping resolution, and bounds
        # checks need to cross-reference the coordinate/grid-mapping/
        # bounds variables against the WHOLE file inventory, not just
        # whichever 1 variable a user happened to select for chunking/
        # compression assessment. Passing `selected` here silently
        # returned "not evaluated" (ok=None) for checks whose supporting
        # variables exist in the file but weren't in the selection --
        # a false-negative risk that could mask real broken CF metadata.
        # chunking/compression correctly keep using `selected` below
        # (those genuinely are per-selected-variable assessments).
        conv = {"cf": conventions.check_cf(inv_list, _global_attrs(handle, fmt))}

        run_benchmark = False
        sample = None
        keepbits = None
        if handle is not None:
            # S2/Finding-2 fix: access_result.fs is None for any local-path
            # input (the local branch of access.workflow never builds a
            # filesystem). Passing that raw None straight into
            # chunking.assess_chunking made its zarr sampler fail
            # internally (fs.find on None) and silently degrade to the
            # 2:1-ratio *estimate* -- with the raw AttributeError string
            # landing in findings.json -- instead of a real measurement.
            # Reuse the same eff_fs/_local_fs() fallback the geozarr check
            # below already relies on so local zarr/HDF5 inputs get a real
            # measured chunk size.
            eff_fs = access_result.fs if access_result.fs is not None else _local_fs()
            chunking_out = chunking.assess_chunking(
                handle, eff_fs, access_result.path, selected,
                budget=budget, use_case=args.use_case, engine=fmt)

            if args.benchmark is None:
                run_benchmark = prompter.confirm(
                    "Run compression codec trials? Downloads ~1 sampled chunk",
                    default=False, flag="--benchmark")
            else:
                run_benchmark = args.benchmark

            sample = _pick_sample(handle, fmt, selected[0]) if run_benchmark else None
            if args.bitround_max_abs_error is not None and sample is not None:
                keepbits = compression.keepbits_for_max_abs_error(
                    sample, args.bitround_max_abs_error)

            if fmt in ("zarr", "icechunk"):
                conv["geozarr"] = conventions.check_geozarr(eff_fs, access_result.path)

        compression_out = compression.assess_compression(
            selected, sample=sample, run_benchmark=run_benchmark, keepbits=keepbits)

    budget.check_stage("assessments", raise_on_breach=False)

    # The opener handed back a live handle (h5py.File / zarr.Group /
    # rasterio dataset / pyarrow.ParquetFile) purely so chunking/
    # compression could read from it above; it has no business in the
    # JSON/report output (unserializable, and h5py in particular leaks
    # a file descriptor if never closed), so close it and drop it from
    # the dict that becomes asset["open"].
    if handle is not None:
        close = getattr(handle, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
    open_for_asset = {k: v for k, v in open_result.items() if k != "handle"}

    status = open_result.get("status")
    if status == "ok":
        smoke_status = "pass"
    elif status == "fail":
        smoke_status = "fail"
    else:
        smoke_status = "skipped"

    asset = {
        "url": access_result.url,
        "format": fmt,
        "format_class": formats.format_class(fmt),
        "access_findings": access_result.findings,
        "open": open_for_asset,
        "inventory": selected,
        "chunking": chunking_out,
        "compression": compression_out,
        "conventions": conv,
        "smoke_status": smoke_status,
        "cmr_meta": _cmr_meta_from(access_result),
        # Finding-3 fix: thread the live-probe ETag (access.probe's HEAD/
        # HeadObject or ranged-GET/GetObject response header, captured on
        # AccessResult by access.workflow) through so scoring.py's
        # E1-version/E2-checksums can credit a real strong ETag instead
        # of permanently reading etag=None for every non-Icechunk asset.
        "etag": access_result.etag,
        "profile": args.use_case,
        # S8: budget.breaches only ever contains stages that actually
        # breached (both check_stage("open"/"assessments", ...) calls
        # above ran with raise_on_breach=False so a breach never crashes
        # the run) -- read once here so the fact surfaces in
        # findings.json and the rendered report (rendering.py's
        # budget_breaches_section) instead of being silently absorbed.
        "budget_breaches": list(budget.breaches),
    }
    scored = scoring.score(asset)
    asset["checks"] = scored["checks"]
    asset["dimensions"] = scored["dimensions"]
    asset["score"] = scored["score"]
    asset["tier"] = scored["tier"]
    # Bridge 1: rendering.py expects metadata_walk nested inside telemetry.
    asset["telemetry"] = {**(open_result.get("telemetry") or {}),
                          "metadata_walk": open_result.get("metadata_walk")}
    tel = open_result.get("telemetry") or {}
    asset["smoke"] = {
        "status": smoke_status,
        "lazy_open": {"requests_to_open": tel.get("requests_to_open"),
                      "bytes_to_open": tel.get("bytes_to_open")},
        "subset_read": {},
        "budget": budget.snapshot(),
        "reason": open_result.get("reason"),
    }

    findings = {
        "input": {"raw": raw_input, "type": kind, "profile_hint": args.use_case},
        "environment": {"python": platform.python_version(),
                        "check_cloud_ready_version": __version__},
        "assets": [asset],
        # Bridge 2: lift scoring's per-asset verdict/score/tier/confidence
        # up to findings' top level (rendering.py reads them from there).
        "score": scored["score"],
        "tier": scored["tier"],
        "verdict": scored["verdict"],
        "verdict_reason": scored["verdict_reason"],
        "confidence": scored["confidence"],
        "sample_frame": {
            "input": raw_input, "format": fmt,
            "variables_assessed": [v["name"] for v in selected],
            "byte_cap_mb": args.byte_cap, "time_cap_s": args.time_cap,
            "budget_used": budget.snapshot(),
        },
        "generated": datetime.now(timezone.utc).isoformat(),
    }

    style = args.report_style or prompter.choose(
        "Report style:", ["verdict", "score", "both"], default="both", flag="--report-style")

    out_dir = Path(args.out) if args.out else Path(f"assessments/cloud-readiness-{_out_dirname(raw_input)}")
    report_path = rendering.write_report(findings, out_dir, style)
    (out_dir / "findings.json").write_text(
        json.dumps(findings, indent=2, sort_keys=True, default=str))

    if args.json:
        print(json.dumps(findings, indent=2, sort_keys=True, default=str))

    print(f"**Verdict: {findings['verdict']} — {findings['verdict_reason']}**")
    if style != "verdict":
        print(f"Tier {findings['tier']} · {findings['score']}/100")
    print(f"Report written to {report_path}")

    return 0


# --------------------------------------------------------------- arg parser

def _build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="check-cloud-ready")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("input", nargs="?", metavar="INPUT",
                    help="Local path, s3:// or https:// URL, or CMR granule ID")
    ap.add_argument("--granule-id")
    ap.add_argument("--credentials-url")
    ap.add_argument("--earthaccess-fallback", action="store_true")
    ap.add_argument("--anon", action="store_true")
    ap.add_argument("--format", default="auto")
    ap.add_argument("--variables")
    ap.add_argument("--all-variables", action="store_true")
    ap.add_argument("--suggest", choices=["heuristic", "llm"], default=None)
    ap.add_argument("--llm-model", default="claude-sonnet-5")
    ap.add_argument("--benchmark", dest="benchmark",
                    action=argparse.BooleanOptionalAction, default=None)
    ap.add_argument("--bitround-max-abs-error", type=float, default=None)
    ap.add_argument("--use-case", choices=["timeseries", "maps", "balanced"], default=None)
    ap.add_argument("--report-style", choices=["verdict", "score", "both"], default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--non-interactive", action="store_true")
    ap.add_argument("--no-network", action="store_true")
    ap.add_argument("--byte-cap", type=float, default=25.0, help="MB")
    ap.add_argument("--time-cap", type=float, default=60.0, help="seconds")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    non_interactive = args.non_interactive or not sys.stdin.isatty()
    prompter = NonInteractivePrompter() if non_interactive else Prompter()

    try:
        return _run(args, prompter)
    except MissingInputError as e:
        print(f"error: {e}", file=sys.stderr)
        return 5
    except Exception as e:  # pragma: no cover - last-resort safety net
        print(f"error: unexpected failure: {type(e).__name__}: {e}", file=sys.stderr)
        return 5


if __name__ == "__main__":
    sys.exit(main())
