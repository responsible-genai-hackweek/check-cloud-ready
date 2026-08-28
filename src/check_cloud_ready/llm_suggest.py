"""LLM-backed variable suggestion (opt-in only).

The only module in this package that touches ``ANTHROPIC_API_KEY`` or
the ``anthropic`` SDK. ``anthropic`` is soft-imported (inside the
function, not at module scope) so the package still imports fine
without the ``llm`` extra installed, and this module is never invoked
unless the user explicitly opted into ``--suggest llm`` (a CLI flag) or
picked "llm" at the interactive suggestion prompt (``cli.py``, Task
12) -- it is never called as part of the default assessment flow.

Failure handling is fail-closed and uniform: ANY problem -- no API key,
the ``anthropic`` package missing, a network/API error, a response that
doesn't parse as a JSON array of strings, or a response naming a
variable that isn't actually in the inventory -- raises
``LLMSuggestUnavailable(reason)``. The caller (``cli.py``) catches this
and falls back to the heuristic ranking (``inventory.rank_variables``),
printing a note. This module never returns a partial/best-effort
result: a suggestion is either fully trustworthy or not returned at
all.

Judgment call (documented per the task brief): a returned name that
isn't in the inventory raises ``LLMSuggestUnavailable`` for the WHOLE
response rather than silently filtering just that name out. A
hallucinated variable name means the model's response can't be trusted
to have read the inventory correctly at all, so filtering and using the
remainder risks silently keeping other, subtler mistakes (e.g. a
QA/bounds variable the model missed excluding) -- failing closed and
falling back to the deterministic heuristic ranking is the safer
default.
"""
from __future__ import annotations

import json
import os

__all__ = ["suggest_variables", "LLMSuggestUnavailable"]

_PROMPT = (
    "You are helping choose which variable(s) in an earth-science dataset "
    "to assess for cloud-readiness. Given this JSON array of candidate "
    "variables (name, dims, shape, attrs), return a ranked JSON array of "
    "variable name strings (most important primary geophysical variable "
    "first). Never include QA/quality/flag/mask variables, bounds "
    "variables, or coordinate/CRS variables. Respond with ONLY the JSON "
    "array of strings -- no prose, no markdown fences.\n\nVariables:\n{payload}"
)


class LLMSuggestUnavailable(Exception):
    """Raised for any reason an LLM suggestion could not be produced;
    the caller should fall back to the heuristic ranking."""


def _summarize(inventory: list[dict]) -> list[dict]:
    """Only names/dims/shapes/attrs are sent -- no chunk layout, size,
    or codec information the model doesn't need to pick a variable."""
    return [
        {"name": v.get("name"), "dims": v.get("dims"), "shape": v.get("shape"),
         "attrs": v.get("attrs")}
        for v in inventory
    ]


def _extract_text(response) -> str:
    return "".join(
        block.text for block in response.content
        if getattr(block, "type", None) == "text"
    )


def suggest_variables(inventory: list[dict], *, model: str = "claude-sonnet-5") -> list[str]:
    """Ask an LLM to rank inventory variable names by assessment
    priority. Raises ``LLMSuggestUnavailable`` on any failure (see
    module docstring) instead of ever returning a best-effort result.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise LLMSuggestUnavailable("no ANTHROPIC_API_KEY set in the environment")

    try:
        import anthropic
    except Exception as e:
        raise LLMSuggestUnavailable(f"anthropic package not available: {e}") from e

    payload = json.dumps(_summarize(inventory))
    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=model, max_tokens=1024,
            messages=[{"role": "user", "content": _PROMPT.format(payload=payload)}],
        )
        text = _extract_text(response)
    except LLMSuggestUnavailable:
        raise
    except Exception as e:
        raise LLMSuggestUnavailable(f"anthropic request failed: {type(e).__name__}: {e}") from e

    try:
        names = json.loads(text)
    except Exception as e:
        raise LLMSuggestUnavailable(f"unparseable LLM response: {e}") from e

    if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
        raise LLMSuggestUnavailable(
            f"LLM response was not a non-empty JSON array of strings: {text!r}")

    available = {v.get("name") for v in inventory}
    unknown = [n for n in names if n not in available]
    if unknown:
        raise LLMSuggestUnavailable(
            f"LLM suggested variable name(s) not in the inventory: {unknown}")

    return names
