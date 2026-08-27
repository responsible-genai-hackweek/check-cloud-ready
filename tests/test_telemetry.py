"""Offline tests for check_cloud_ready.telemetry.

No network access. All filesystem/file objects are fakes built in-test.
Budget is given an injectable clock so time-cap behavior is testable
without sleeping.
"""
import unittest

from fsspec.spec import AbstractBufferedFile, AbstractFileSystem

from check_cloud_ready import telemetry as telemetry_mod

Budget = telemetry_mod.Budget
BudgetExceeded = telemetry_mod.BudgetExceeded
MeasurementError = telemetry_mod.MeasurementError
CountingFS = telemetry_mod.CountingFS
counting_fs = telemetry_mod.counting_fs
assert_open_measured = telemetry_mod.assert_open_measured
BYTE_CAP = telemetry_mod.BYTE_CAP
TIME_CAP = telemetry_mod.TIME_CAP
HEADER_READ = telemetry_mod.HEADER_READ


class FakeClock:
    """Manually-advanced clock standing in for time.monotonic."""

    def __init__(self, start=0.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class ReadOnlyFile:
    """A fake underlying file exposing only read() (like a plain
    file-like object with no readinto of its own)."""

    def __init__(self, data):
        self.data = data
        self.pos = 0
        self.closed = False

    def read(self, n=-1):
        if n is None or n < 0:
            n = len(self.data) - self.pos
        chunk = self.data[self.pos:self.pos + n]
        self.pos += len(chunk)
        return chunk

    def close(self):
        self.closed = True


class ReadIntoOnlyConsumerFile(ReadOnlyFile):
    """Same fake file; the *consumer* in these tests calls only
    readinto(), the way h5py's fileobj driver does. read() being
    present on the underlying object is incidental (real file objects
    have both); what matters is which method the caller invokes.
    """


class FakeFS:
    """Minimal fsspec-like fake: only .open() is exercised (mirrors
    ported CountingFS._COUNTED semantics for the "open" branch), plus
    cat_file for the requests-per-call assertions.
    """

    def __init__(self, files, file_cls=ReadOnlyFile):
        self.files = files
        self.file_cls = file_cls
        self.open_calls = 0
        self.cat_calls = 0

    def open(self, path, mode="rb", **kwargs):
        self.open_calls += 1
        return self.file_cls(self.files[path])

    def cat_file(self, path):
        self.cat_calls += 1
        return self.files[path]


class InstrumentedFetchFile(AbstractBufferedFile):
    """A real fsspec ``AbstractBufferedFile`` subclass whose
    ``_fetch_range`` is instrumented to record exactly how many bytes
    crossed the "wire" in place of a real network call. This exercises
    fsspec's actual cache-mediated code path — ``cache_type="readahead"``
    is fsspec's own default — rather than a fake with no cache at all,
    so it reproduces the S5 wire-byte undercount for real.
    """

    def __init__(self, fs, path, wire_log, data=b"", mode="rb",
                 block_size=None, cache_type="readahead", **kwargs):
        self._data = data
        self._wire_log = wire_log
        super().__init__(fs, path, mode=mode, block_size=block_size,
                          cache_type=cache_type, size=len(data), **kwargs)

    def _fetch_range(self, start, end):
        chunk = self._data[start:end]
        self._wire_log.append(len(chunk))
        return chunk


class InstrumentedFetchFS(AbstractFileSystem):
    """Minimal real ``AbstractFileSystem`` whose ``open()`` returns an
    ``InstrumentedFetchFile``, so its cache/fetcher machinery is the
    genuine fsspec implementation, not a hand-rolled stand-in."""

    protocol = "instrumented-test"

    def __init__(self, data, wire_log):
        super().__init__()
        self._data = data
        self._wire_log = wire_log

    def _open(self, path, mode="rb", block_size=None,
              cache_type="readahead", **kwargs):
        return InstrumentedFetchFile(
            self, path, wire_log=self._wire_log, data=self._data,
            mode=mode, block_size=block_size, cache_type=cache_type,
            **kwargs)

    def info(self, path, **kwargs):
        return {"name": path, "size": len(self._data), "type": "file"}


class BudgetSpendTests(unittest.TestCase):
    def test_spend_accumulates_bytes_and_requests(self):
        b = Budget()
        b.spend(100, 1)
        b.spend(50, 1)
        self.assertEqual(b.bytes, 150)
        self.assertEqual(b.requests, 2)

    def test_byte_cap_raises_and_records_breach(self):
        b = Budget(byte_cap=100)
        with self.assertRaises(BudgetExceeded):
            b.spend(150, 1)
        self.assertEqual(len(b.breaches), 1)
        breach = b.breaches[0]
        self.assertTrue(breach["breached"])
        self.assertIn("oversized reads", breach.get("message", ""))

    def test_byte_cap_can_be_opted_out_of_raising_but_still_records(self):
        b = Budget(byte_cap=100)
        b.spend(150, 1, raise_on_breach=False)
        self.assertEqual(len(b.breaches), 1)
        self.assertEqual(b.bytes, 150)

    def test_snapshot_reports_bytes_requests_elapsed(self):
        clock = FakeClock()
        b = Budget(clock=clock)
        b.spend(10, 1)
        clock.advance(5)
        snap = b.snapshot()
        self.assertEqual(snap["bytes"], 10)
        self.assertEqual(snap["requests"], 1)
        self.assertEqual(snap["elapsed_s"], 5.0)


class BudgetStageBoundaryTests(unittest.TestCase):
    """S8: time cap enforced at stage boundaries, not mid-spend."""

    def test_spend_never_raises_on_time_alone(self):
        clock = FakeClock()
        b = Budget(clock=clock, time_cap=1.0)
        clock.advance(65.1)  # observed failure was 65.1s under the old code
        # spend() must not itself explode from stale wall-clock time; only
        # check_stage() enforces the time cap now.
        b.spend(1, 1)
        self.assertEqual(b.breaches, [])

    def test_check_stage_raises_and_records_structured_finding(self):
        clock = FakeClock()
        b = Budget(clock=clock, time_cap=60.0)
        clock.advance(65.1)
        with self.assertRaises(BudgetExceeded):
            b.check_stage("lazy_open")
        self.assertEqual(len(b.breaches), 1)
        finding = b.breaches[0]
        self.assertEqual(finding["stage"], "lazy_open")
        self.assertAlmostEqual(finding["elapsed_s"], 65.1, places=2)
        self.assertEqual(finding["cap_s"], 60.0)
        self.assertTrue(finding["breached"])
        self.assertIn("oversized reads", finding.get("message", ""))

    def test_check_stage_breach_is_catchable_not_fatal(self):
        """A breach must not crash the whole program: callers can catch
        BudgetExceeded and continue, with the finding already recorded."""
        clock = FakeClock()
        b = Budget(clock=clock, time_cap=10.0)
        clock.advance(20)
        try:
            b.check_stage("subset_read")
        except BudgetExceeded as e:
            recovered = True
            message = str(e)
        else:
            recovered = False
            message = ""
        self.assertTrue(recovered)
        self.assertTrue("oversized reads" in message or "cap" in message)
        self.assertEqual(len(b.breaches), 1)

    def test_check_stage_under_cap_does_not_raise_or_record(self):
        clock = FakeClock()
        b = Budget(clock=clock, time_cap=60.0)
        clock.advance(1.0)
        finding = b.check_stage("head")
        self.assertFalse(finding["breached"])
        self.assertEqual(b.breaches, [])


class CountingFileReadTests(unittest.TestCase):
    """S5 regression: bytes must be counted regardless of whether the
    consumer calls read() or readinto()."""

    def test_plain_read_is_counted(self):
        b = Budget()
        fs = FakeFS({"/a": b"x" * 1000})
        cfs = counting_fs(fs, budget=b)
        f = cfs.open("/a", "rb")
        data = f.read(200)
        self.assertEqual(len(data), 200)
        self.assertEqual(b.bytes, 200)

    def test_readinto_only_consumer_is_counted(self):
        """A consumer (like h5py's fileobj driver) that calls ONLY
        readinto() must still have its bytes counted. Before the S5
        fix, _CountingFile only wrapped read(), so a readinto()-only
        consumer scored 0 bytes even after real reads happened.
        """
        b = Budget()
        fs = FakeFS({"/a": b"y" * 1000}, file_cls=ReadIntoOnlyConsumerFile)
        cfs = counting_fs(fs, budget=b)
        f = cfs.open("/a", "rb")

        buf = bytearray(256)
        n = f.readinto(buf)

        self.assertEqual(n, 256)
        self.assertEqual(bytes(buf), b"y" * 256)
        self.assertEqual(b.bytes, 256)

    def test_readinto1_if_present_is_also_counted(self):
        b = Budget()
        fs = FakeFS({"/a": b"z" * 1000})
        cfs = counting_fs(fs, budget=b)
        f = cfs.open("/a", "rb")
        buf = bytearray(64)
        n = f.readinto1(buf)
        self.assertEqual(n, 64)
        self.assertEqual(b.bytes, 64)

    def test_multiple_readinto_calls_accumulate(self):
        b = Budget()
        fs = FakeFS({"/a": b"w" * 1000})
        cfs = counting_fs(fs, budget=b)
        f = cfs.open("/a", "rb")
        buf = bytearray(100)
        for _ in range(3):
            f.readinto(buf)
        self.assertEqual(b.bytes, 300)


class WireByteFetchLayerTests(unittest.TestCase):
    """S5 (part 2): counting at the read()/readinto() layer alone still
    undercounts *wire* bytes for fsspec's cache-mediated
    AbstractBufferedFile subclasses, because a small request can
    trigger a much larger block fetch on a cache miss (the default
    cache_type is "readahead"). _CountingFile must hook the cache's
    fetcher and count what actually crossed the wire, not just what
    the caller consumed — and must not double-count when it does.
    """

    def test_readinto_counts_wire_bytes_not_just_consumed_bytes(self):
        data = b"Q" * (5 * 1024 * 1024)
        wire_log = []
        fs = InstrumentedFetchFS(data, wire_log)
        b = Budget()
        cfs = counting_fs(fs, budget=b)
        f = cfs.open("/x", "rb", block_size=1024 * 1024, cache_type="readahead")

        buf = bytearray(16384)
        n = f.readinto(buf)

        self.assertEqual(n, 16384)  # consumer still gets what it asked for
        wire_bytes = sum(wire_log)
        self.assertGreater(wire_bytes, 16384)  # block cache over-fetched
        self.assertEqual(b.bytes, wire_bytes)  # Budget tracks the wire cost,
                                                # not the smaller consumed count
        # 1 request for the open() call itself, plus 1 per real wire fetch.
        self.assertEqual(b.requests, 1 + len(wire_log))

    def test_second_read_within_cached_block_adds_no_new_request(self):
        """A second small read served entirely from the already-fetched
        block must not trigger another wire fetch/request."""
        data = b"R" * (5 * 1024 * 1024)
        wire_log = []
        fs = InstrumentedFetchFS(data, wire_log)
        b = Budget()
        cfs = counting_fs(fs, budget=b)
        f = cfs.open("/x", "rb", block_size=1024 * 1024, cache_type="readahead")

        f.readinto(bytearray(1024))
        bytes_after_first, requests_after_first = b.bytes, b.requests
        f.readinto(bytearray(1024))  # still within the same cached block

        self.assertEqual(b.requests, requests_after_first)  # no new wire fetch
        self.assertEqual(b.bytes, bytes_after_first)  # no new bytes counted
        self.assertEqual(len(wire_log), 1)  # confirm: fsspec really did serve
                                             # this from cache, not a new fetch

    def test_plain_file_without_fetch_layer_is_not_double_counted(self):
        """Files with no cache/fetcher (plain file-like objects, e.g.
        local files or the fakes used elsewhere in this module) fall
        back to counting consumed bytes exactly once — not missed
        (zero) and not doubled (counted at both layers).
        """
        b = Budget()
        fs = FakeFS({"/a": b"p" * 1000})
        cfs = counting_fs(fs, budget=b)
        f = cfs.open("/a", "rb")
        f.read(100)
        f.readinto(bytearray(50))
        self.assertEqual(b.bytes, 150)


class RequestCountingTests(unittest.TestCase):
    def test_open_counts_one_request(self):
        b = Budget()
        fs = FakeFS({"/a": b"data"})
        cfs = counting_fs(fs, budget=b)
        cfs.open("/a", "rb")
        self.assertEqual(b.requests, 1)

    def test_cat_file_counts_one_request_and_its_bytes(self):
        b = Budget()
        fs = FakeFS({"/a": b"0123456789"})
        cfs = counting_fs(fs, budget=b)
        raw = cfs.cat_file("/a")
        self.assertEqual(raw, b"0123456789")
        self.assertEqual(b.requests, 1)
        self.assertEqual(b.bytes, 10)

    def test_open_then_reads_do_not_add_extra_requests(self):
        """Ported semantics: reads on an opened file count bytes but not
        additional requests (request accounting happens at the FS-proxy
        open() call, not per read)."""
        b = Budget()
        fs = FakeFS({"/a": b"v" * 1000})
        cfs = counting_fs(fs, budget=b)
        f = cfs.open("/a", "rb")
        f.read(10)
        f.readinto(bytearray(10))
        self.assertEqual(b.requests, 1)


class MeasurementGuardTests(unittest.TestCase):
    """S5: a successful open with zero measured bytes is a measurement
    bug, not a clean bill of health."""

    def test_zero_bytes_after_successful_open_trips_guard(self):
        stats = {"requests": 1, "bytes_read": 0}
        with self.assertRaises(MeasurementError):
            assert_open_measured(stats)

    def test_nonzero_bytes_passes_guard(self):
        stats = {"requests": 1, "bytes_read": 128}
        assert_open_measured(stats)  # must not raise


class SnapshotDerivationTests(unittest.TestCase):
    """Snapshot semantics let callers derive requests_to_open /
    bytes_to_open by diffing two snapshots taken around an open."""

    def test_requests_and_bytes_to_open_are_derivable_from_snapshots(self):
        b = Budget()
        fs = FakeFS({"/a": b"q" * 10000})
        cfs = counting_fs(fs, budget=b)

        before = cfs.snapshot()
        f = cfs.open("/a", "rb")
        f.read(1000)
        after_open = cfs.snapshot()

        requests_to_open = after_open["requests"] - before["requests"]
        bytes_to_open = after_open["bytes_read"] - before["bytes_read"]
        self.assertEqual(requests_to_open, 1)  # the open() call
        self.assertEqual(bytes_to_open, 1000)

        f.read(500)
        after_more = cfs.snapshot()
        self.assertEqual(after_more["bytes_read"] - after_open["bytes_read"], 500)
        self.assertEqual(after_more["requests"] - after_open["requests"], 0)

    def test_stats_property_matches_snapshot(self):
        b = Budget()
        fs = FakeFS({"/a": b"m" * 10})
        cfs = counting_fs(fs, budget=b)
        cfs.open("/a", "rb")
        self.assertEqual(cfs.stats["requests"], cfs.snapshot()["requests"])
        self.assertEqual(cfs.stats["bytes_read"], cfs.snapshot()["bytes_read"])


class CountingFsDefaultBudgetTests(unittest.TestCase):
    def test_counting_fs_creates_its_own_budget_if_not_given(self):
        fs = FakeFS({"/a": b"n" * 10})
        cfs = counting_fs(fs)
        self.assertIsInstance(cfs.budget, Budget)
        cfs.open("/a", "rb")
        self.assertEqual(cfs.budget.requests, 1)


if __name__ == "__main__":
    unittest.main()
