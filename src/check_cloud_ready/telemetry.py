"""Transfer budget and a counting fsspec wrapper.

Ported from the earth-science-cloud-readiness skill's smoke_test.py
(``Budget``/``BudgetExceeded``, ``_CountingFile``/``CountingFS``/
``_counting_fs_for``), with two fixes:

S5 (undercounting) has two distinct causes, both fixed here:

1. The ported ``_CountingFile`` wrapped only ``read()``. h5py's fileobj
   driver calls ``readinto()`` exclusively, so a 3.3 GB HDF5 file
   scored "1 request / 0 bytes to open" — a measurement bug, not a
   clean bill of health. ``_CountingFile`` now gives its own
   ``readinto``/``readinto1`` implementations that route through its
   own ``read()`` (mirroring ``fsspec.spec.AbstractBufferedFile.
   readinto``) instead of delegating to the wrapped object's readinto
   via ``__getattr__``, which would bypass counting entirely.
2. Even with (1) fixed, counting bytes at the read()/readinto() layer
   alone still undercounts *wire* bytes for fsspec's cache-mediated
   ``AbstractBufferedFile`` subclasses (S3File/HTTPFile/...): their
   ``read()`` returns only the bytes the caller requested, sized to the
   request, while the default ``cache_type="readahead"`` block cache
   fetches a whole ``blocksize``-sized (or larger) range over the wire
   on a cache miss (measured: a single 16 KB ``readinto`` against a
   1 MB block size pulled ~1 MB over the wire but was being counted as
   16 KB — a ~65x undercount). ``_CountingFile`` now hooks the wrapped
   file's ``cache.fetcher`` (fsspec's duck-typed fetch hook,
   bound to ``_fetch_range``) when present, counting the true wire
   bytes and one request per actual fetch there instead; the
   read()/readinto() layer then skips its own byte counting for that
   file, so bytes are never double-counted at both layers. Plain file
   objects with no such cache (local files, raw test doubles) have no
   fetch layer to hook, so they fall back to counting bytes consumed at
   the read()/readinto() layer, which is exactly correct for them.

``assert_open_measured`` is the exported guard callers use to catch any
*remaining* undercount down to zero: a "successful" open with zero
measured bytes is treated as a bug, never as a perfect score.

S8 (wall-clock cap only checked mid-read): the ported ``Budget.spend``
checked the time cap on every call, but a run with no further reads
after the cap ticked over could sail past it (observed: 65.1s against
a 60s cap) because nothing was left to trigger the check. Time is now
enforced only at explicit stage boundaries via ``Budget.check_stage``,
called by pipeline code between phases (HEAD probe, ranged reads, lazy
open, subset read, ...). A breach is always recorded as a structured
finding in ``Budget.breaches`` (so it can be reported as its own
finding: "dataset forces oversized reads") and, by default, also
raised as ``BudgetExceeded`` so a caller that doesn't explicitly
handle it still stops promptly; callers that want to keep going after
recording the finding pass ``raise_on_breach=False`` or catch the
exception.

Dropped from the port, by design: the ``_NISAR_AUTH_FS`` global, the
hardcoded NISAR earthaccess endpoint, and the URL-scheme/auth
heuristics inside ``_counting_fs_for``. In this architecture the
caller (access/workflow.py) constructs and authenticates the fsspec
filesystem; this module only wraps it. Use ``counting_fs(fs)`` where
the skill used to call ``_counting_fs_for(url, budget)``.
"""
from __future__ import annotations

import time as _time

BYTE_CAP = 25 * 1024 * 1024   # ~25 MB per asset
TIME_CAP = 60.0                # seconds per asset
HEADER_READ = 16 * 1024        # 16 KB


class BudgetExceeded(Exception):
    """Raised when a transfer budget (bytes or wall time) is exceeded."""


class MeasurementError(Exception):
    """Raised when counting instrumentation appears to have missed
    reads (e.g. bytes_read == 0 after a "successful" open). This is a
    bug in the instrumentation layer, not a fact about the dataset —
    never let it read as a perfect (zero-cost) score.
    """


class Budget:
    """Tracks bytes transferred, request count, and wall time for one
    asset, and enforces the byte and time caps.

    ``clock`` defaults to ``time.monotonic`` but is injectable so tests
    can simulate elapsed time without sleeping.
    """

    def __init__(self, byte_cap=BYTE_CAP, time_cap=TIME_CAP,
                 clock=_time.monotonic):
        self.byte_cap = byte_cap
        self.time_cap = time_cap
        self.clock = clock
        self.bytes = 0
        self.requests = 0
        self.t0 = self.clock()
        self.breaches = []

    def spend(self, nbytes, nreq=1, raise_on_breach=True):
        """Record a transfer. Enforces the byte cap immediately (this
        is the one check that must fire mid-read, since a single
        oversized read can blow through 25 MB in one call). Does NOT
        check the time cap — see ``check_stage`` (S8).
        """
        self.bytes += int(nbytes)
        self.requests += int(nreq)
        if self.bytes > self.byte_cap:
            message = (f"byte cap {self.byte_cap} exceeded ({self.bytes}) "
                       "- dataset forces oversized reads")
            self.breaches.append({
                "stage": None,
                "kind": "bytes",
                "bytes": self.bytes,
                "cap_bytes": self.byte_cap,
                "breached": True,
                "message": message,
            })
            if raise_on_breach:
                raise BudgetExceeded(message)

    def check_stage(self, stage_name: str, raise_on_breach: bool = True):
        """Enforce the wall-clock cap at a stage boundary (S8). Always
        returns a structured finding dict. On breach, the finding is
        appended to ``self.breaches`` regardless of ``raise_on_breach``;
        by default this also raises ``BudgetExceeded`` so a caller that
        doesn't explicitly catch it still stops, but a caller that
        wants to keep going and just record the finding can pass
        ``raise_on_breach=False`` or catch the exception (the record is
        already in ``self.breaches`` either way).
        """
        elapsed = self.clock() - self.t0
        breached = elapsed > self.time_cap
        finding = {
            "stage": stage_name,
            "elapsed_s": round(elapsed, 3),
            "cap_s": self.time_cap,
            "breached": breached,
        }
        if breached:
            message = (f"time cap {self.time_cap}s exceeded at stage "
                       f"{stage_name!r} ({elapsed:.1f}s) - dataset forces "
                       "oversized reads")
            finding["message"] = message
            self.breaches.append(finding)
            if raise_on_breach:
                raise BudgetExceeded(message)
        return finding

    def snapshot(self):
        return {"bytes": self.bytes, "requests": self.requests,
                "elapsed_s": round(self.clock() - self.t0, 3)}


def assert_open_measured(stats: dict) -> None:
    """Guard against S5-style undercounting. Callers (openers) invoke
    this after a lazy open reports success: if ``bytes_read`` is 0,
    that means the counting wrapper missed every read the opener made
    (e.g. a library that calls readinto() exclusively against an
    instrumentation layer that only wrapped read()) — not that the
    open was free. Raises ``MeasurementError`` rather than letting the
    zero flow through into a report as a perfect score.
    """
    if stats.get("bytes_read", 0) == 0:
        raise MeasurementError(
            "instrumentation bug: open reported success but bytes_read == 0 "
            "(counting wrapper likely missed a readinto()/fetch path)"
        )


class _CountingFile:
    """Wraps a file-like object returned by ``fs.open()``, counting
    bytes through every path a consumer might pull data through.

    ``read()`` and ``readinto()``/``readinto1()`` are all implemented
    here explicitly rather than left to fall through ``__getattr__``:
    if ``readinto`` were left to delegate straight to the wrapped
    object's own ``readinto``, a consumer that calls only ``readinto``
    (h5py's fileobj driver) would never touch this class's counted
    ``read`` at all, and bytes would be silently undercounted (S5).

    That alone is not sufficient for fsspec's ``AbstractBufferedFile``
    subclasses (the S3File/HTTPFile family), because their ``read()``
    returns bytes sized to the *request*, not to what was actually
    fetched over the wire: on a cache miss, the default
    ``cache_type="readahead"`` block cache fetches a whole
    ``blocksize``-sized (or larger) range via ``self.cache.fetcher``
    (bound to ``_fetch_range``), and only serves the requested slice
    back to the caller. Counting at the read()/readinto() layer alone
    can undercount true wire traffic by up to ~blocksize per miss (a
    16 KB ``readinto`` against a 1 MB block size measured as a ~65x
    undercount in practice). So: when the wrapped file exposes a
    duck-typed ``cache.fetcher`` (the fsspec block-cache pattern), this
    class hooks that fetcher instead and counts wire bytes + one
    request per actual fetch; ``read()`` then does *not* also count
    consumed bytes for that file, to avoid double-counting the same
    bytes at both layers. For plain file-like objects with no such
    cache (local files, raw fakes/test doubles), there is no fetch
    layer to hook, so ``read()`` falls back to counting bytes consumed
    by the caller, as before — that is exactly correct there, since
    "consumed" and "fetched" are the same thing for an unbuffered file.
    """

    def __init__(self, f, budget: Budget):
        self._f = f
        self._budget = budget
        self._wire_counted = self._hook_fetch_layer()

    def _hook_fetch_layer(self) -> bool:
        """Wrap ``f.cache.fetcher`` in place, if present, so every wire
        fetch adds its true byte count and increments the request
        counter. Returns True if a fetch layer was found and hooked
        (meaning ``read()`` must not also count consumed bytes for this
        file), False if there's no such layer (meaning ``read()``
        should count consumed bytes as the fallback).
        """
        cache = getattr(self._f, "cache", None)
        fetcher = getattr(cache, "fetcher", None)
        if fetcher is None or not callable(fetcher):
            return False
        budget = self._budget

        def counted_fetcher(start, end, _orig=fetcher):
            data = _orig(start, end)
            budget.spend(len(data), 1)
            return data

        cache.fetcher = counted_fetcher
        return True

    def read(self, *a, **k):
        data = self._f.read(*a, **k)
        if not self._wire_counted:
            # No fetch layer to hook (plain file object / local file /
            # test fake): count bytes consumed by the caller, as before.
            # Each read may or may not correspond to a new network
            # request; request counting for this fallback path happens
            # in the FS proxy (CountingFS.open()/cat_file/...), not here.
            self._budget.spend(len(data), 0)
        return data

    def readinto(self, b):
        # Mirrors fsspec.spec.AbstractBufferedFile.readinto: pull bytes
        # through *this* class's read() instead of delegating to the
        # wrapped object's own readinto, so the same counting rules
        # (fetch-layer or consumed-bytes fallback) always apply.
        out = memoryview(b).cast("B")
        data = self.read(len(out))
        n = len(data)
        out[:n] = data
        return n

    def readinto1(self, b):
        return self.readinto(b)

    def __getattr__(self, name):
        return getattr(self._f, name)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self._f.close()


class CountingFS:
    """Delegating proxy over an fsspec-like filesystem that counts
    likely-HTTP operations (cat_file/info/ls/open/...) and feeds a
    shared ``Budget``.
    """

    _COUNTED = {"cat_file", "cat", "cat_ranges", "info", "ls", "exists",
                "isdir", "isfile", "open", "get_mapper", "size", "find"}

    def __init__(self, fs, budget: "Budget | None" = None):
        self._fs = fs
        self.budget = budget if budget is not None else Budget()

    @property
    def stats(self):
        """Current counters, keyed for callers computing
        requests_to_open/bytes_to_open (see ``snapshot``)."""
        return {"requests": self.budget.requests, "bytes_read": self.budget.bytes}

    def snapshot(self):
        """Point-in-time counters. Callers take one snapshot before an
        operation (e.g. a lazy open) and another after, and diff the
        two to derive that operation's requests_to_open/bytes_to_open
        without needing a dedicated "phase" API.
        """
        snap = self.budget.snapshot()
        return {"requests": snap["requests"], "bytes_read": snap["bytes"],
                "elapsed_s": snap["elapsed_s"]}

    def _count_call(self, name, result=None):
        nbytes = 0
        if isinstance(result, (bytes, bytearray)):
            nbytes = len(result)
        elif isinstance(result, dict):
            nbytes = sum(len(v) for v in result.values()
                         if isinstance(v, (bytes, bytearray)))
        elif isinstance(result, list) and result and isinstance(result[0], (bytes, bytearray)):
            nbytes = sum(len(v) for v in result)
        self.budget.spend(nbytes, 1)

    def __getattr__(self, name):
        attr = getattr(self._fs, name)
        if name not in self._COUNTED or not callable(attr):
            return attr
        budget = self.budget
        count = self._count_call

        def wrapper(*a, **k):
            res = attr(*a, **k)
            if name == "open":
                budget.spend(0, 1)
                return _CountingFile(res, budget)
            count(name, res)
            return res
        return wrapper


def counting_fs(fs, budget: "Budget | None" = None) -> CountingFS:
    """Wrap an already-constructed fsspec-like filesystem (the caller
    is responsible for authentication/construction — see
    access/workflow.py) with request/byte counting. Creates a fresh
    ``Budget`` if one isn't supplied.
    """
    return CountingFS(fs, budget)
