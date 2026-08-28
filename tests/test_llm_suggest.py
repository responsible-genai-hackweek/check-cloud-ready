"""Tests for check_cloud_ready.llm_suggest: fail-closed LLM variable
suggestion. No real network/API calls -- the ``anthropic`` module is
faked via sys.modules monkeypatching."""
import json
import sys
import types
import unittest
import unittest.mock

from check_cloud_ready.llm_suggest import LLMSuggestUnavailable, suggest_variables

_INVENTORY = [
    {"name": "/sst", "dims": ["time", "lat", "lon"], "shape": [10, 20, 30], "attrs": {}},
    {"name": "/lat", "dims": ["lat"], "shape": [20], "attrs": {}},
    {"name": "/qa_flag", "dims": ["time", "lat", "lon"], "shape": [10, 20, 30], "attrs": {}},
]


def _fake_anthropic_module(response_text=None, raise_exc=None):
    """Build a fake `anthropic` module exposing just enough surface
    (Anthropic().messages.create(...) -> object with .content) for
    llm_suggest.suggest_variables to call."""
    mod = types.ModuleType("anthropic")

    class _Block:
        def __init__(self, text):
            self.type = "text"
            self.text = text

    class _Response:
        def __init__(self, text):
            self.content = [_Block(text)]

    class _Messages:
        def create(self, **kwargs):
            if raise_exc is not None:
                raise raise_exc
            return _Response(response_text)

    class _Anthropic:
        def __init__(self, api_key=None):
            self.api_key = api_key
            self.messages = _Messages()

    mod.Anthropic = _Anthropic
    return mod


class TestNoApiKey(unittest.TestCase):
    def test_raises_without_key(self):
        import os
        old = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            with self.assertRaises(LLMSuggestUnavailable):
                suggest_variables(_INVENTORY)
        finally:
            if old is not None:
                os.environ["ANTHROPIC_API_KEY"] = old


class _WithApiKeyAndFakeAnthropic(unittest.TestCase):
    """Base class: sets ANTHROPIC_API_KEY and installs a fake
    `anthropic` module for the duration of each test."""

    response_text = None
    raise_exc = None

    def setUp(self):
        import os
        self._old_key = os.environ.get("ANTHROPIC_API_KEY")
        os.environ["ANTHROPIC_API_KEY"] = "test-key"
        self._old_module = sys.modules.get("anthropic")
        sys.modules["anthropic"] = _fake_anthropic_module(
            response_text=self.response_text, raise_exc=self.raise_exc)

    def tearDown(self):
        import os
        if self._old_key is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = self._old_key
        if self._old_module is None:
            sys.modules.pop("anthropic", None)
        else:
            sys.modules["anthropic"] = self._old_module


class TestGoodResponse(_WithApiKeyAndFakeAnthropic):
    response_text = json.dumps(["/sst"])

    def test_parses_ranked_list(self):
        self.assertEqual(suggest_variables(_INVENTORY), ["/sst"])


class TestGarbageResponse(_WithApiKeyAndFakeAnthropic):
    response_text = "not json at all"

    def test_unparseable_raises(self):
        with self.assertRaises(LLMSuggestUnavailable):
            suggest_variables(_INVENTORY)


class TestNonListResponse(_WithApiKeyAndFakeAnthropic):
    response_text = json.dumps({"not": "a list"})

    def test_non_list_raises(self):
        with self.assertRaises(LLMSuggestUnavailable):
            suggest_variables(_INVENTORY)


class TestUnknownVariableName(_WithApiKeyAndFakeAnthropic):
    response_text = json.dumps(["/totally_made_up_variable"])

    def test_unknown_name_raises(self):
        # Documented choice: a hallucinated/unknown name fails the
        # WHOLE response closed rather than silently filtering it out.
        with self.assertRaises(LLMSuggestUnavailable) as ctx:
            suggest_variables(_INVENTORY)
        self.assertIn("totally_made_up_variable", str(ctx.exception))


class TestNetworkError(_WithApiKeyAndFakeAnthropic):
    raise_exc = ConnectionError("boom")

    def test_network_error_raises(self):
        with self.assertRaises(LLMSuggestUnavailable):
            suggest_variables(_INVENTORY)


class TestImportError(unittest.TestCase):
    def test_anthropic_not_installed_raises(self):
        import os
        import builtins

        old_key = os.environ.get("ANTHROPIC_API_KEY")
        os.environ["ANTHROPIC_API_KEY"] = "test-key"
        old_module = sys.modules.pop("anthropic", None)

        real_import = builtins.__import__

        def fake_import(name, *a, **k):
            if name == "anthropic":
                raise ImportError("no anthropic here")
            return real_import(name, *a, **k)

        try:
            with unittest.mock.patch("builtins.__import__", side_effect=fake_import):
                with self.assertRaises(LLMSuggestUnavailable):
                    suggest_variables(_INVENTORY)
        finally:
            if old_key is None:
                os.environ.pop("ANTHROPIC_API_KEY", None)
            else:
                os.environ["ANTHROPIC_API_KEY"] = old_key
            if old_module is not None:
                sys.modules["anthropic"] = old_module


if __name__ == "__main__":
    unittest.main()
