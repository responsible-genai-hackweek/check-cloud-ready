"""Interactive/non-interactive prompt abstraction.

``Prompter`` wraps stdlib ``input()`` with numbered-option rendering and
default handling. ``NonInteractivePrompter`` never touches stdin: it
returns a default when one exists, or raises ``MissingInputError``
naming the CLI flag that would have supplied the missing value.

Both classes share the same call signature (``ask``/``choose``/
``confirm``, each accepting an optional keyword-only ``flag``) so
``cli.py`` can call either polymorphically without knowing which one is
active. ``access/workflow.py`` (Task 4) calls ``ask``/``choose``/
``confirm`` without ``flag`` at all -- every one of its call sites
either supplies a default or is gated behind a ``confirm`` that
defaults to ``False``, so a ``NonInteractivePrompter`` never actually
needs ``flag`` on those calls (see auth-workflow.md's flow: the only
default-less ``ask`` there is reached only when a preceding ``confirm``
returned ``True``, which never happens non-interactively).
"""
from __future__ import annotations

__all__ = ["Prompter", "NonInteractivePrompter", "MissingInputError"]


class MissingInputError(Exception):
    """Raised by ``NonInteractivePrompter`` when a prompt has no
    default and ``--non-interactive`` means no ``input()`` call can be
    made. The message names the CLI flag the caller should pass
    instead."""


class Prompter:
    """Interactive prompter over stdlib ``input()``."""

    def ask(self, text: str, default: str | None = None, *, flag: str | None = None) -> str:
        suffix = f" [{default}]" if default not in (None, "") else ""
        prompt = f"{text}{suffix}: "
        while True:
            raw = input(prompt).strip()
            if raw:
                return raw
            if default is not None:
                return default

    def choose(self, text: str, options: list[str], default, *, flag: str | None = None) -> str:
        default_label = default if isinstance(default, str) else options[default]
        lines = [text]
        for i, opt in enumerate(options):
            marker = " [default]" if opt == default_label else ""
            lines.append(f"  {i}) {opt}{marker}")
        prompt = "\n".join(lines) + f"\nChoice [{default_label}]: "
        while True:
            raw = input(prompt).strip()
            if not raw:
                return default_label
            if raw in options:
                return raw
            if raw.isdigit() and int(raw) < len(options):
                return options[int(raw)]
            print(f"Invalid choice {raw!r}; enter a number 0-{len(options) - 1} or one "
                  f"of: {', '.join(options)}")

    def confirm(self, text: str, default: bool = False, *, flag: str | None = None) -> bool:
        hint = "Y/n" if default else "y/N"
        prompt = f"{text} [{hint}]: "
        while True:
            raw = input(prompt).strip().lower()
            if not raw:
                return default
            if raw in ("y", "yes"):
                return True
            if raw in ("n", "no"):
                return False
            print("Please answer y or n.")


class NonInteractivePrompter:
    """Never touches stdin. Returns a default when one exists;
    otherwise raises ``MissingInputError`` naming ``flag``."""

    def __init__(self, defaults_ok: bool = True):
        self.defaults_ok = defaults_ok

    def _require(self, default, flag):
        raise MissingInputError(
            f"interactive prompt required but --non-interactive given; pass {flag}"
        )

    def ask(self, text: str, default: str | None = None, *, flag: str | None = None) -> str:
        if default is not None:
            return default
        return self._require(default, flag)

    def choose(self, text: str, options: list[str], default, *, flag: str | None = None) -> str:
        if default is None:
            return self._require(default, flag)
        return default if isinstance(default, str) else options[default]

    def confirm(self, text: str, default: bool = False, *, flag: str | None = None) -> bool:
        return bool(default)
