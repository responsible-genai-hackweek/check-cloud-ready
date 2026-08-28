"""Tests for check_cloud_ready.prompts: Prompter (interactive, over a
monkeypatched input()) and NonInteractivePrompter (defaults or
MissingInputError)."""
import unittest
from unittest import mock

from check_cloud_ready.prompts import MissingInputError, NonInteractivePrompter, Prompter


def _feed(*answers):
    """Return a function suitable as input()'s side_effect: yields each
    answer in turn."""
    it = iter(answers)
    return lambda *a, **k: next(it)


class TestPrompterAsk(unittest.TestCase):
    def test_returns_typed_answer(self):
        p = Prompter()
        with mock.patch("builtins.input", side_effect=_feed("hello")):
            self.assertEqual(p.ask("Say something"), "hello")

    def test_empty_input_returns_default(self):
        p = Prompter()
        with mock.patch("builtins.input", side_effect=_feed("")):
            self.assertEqual(p.ask("Say something", default="world"), "world")

    def test_empty_input_no_default_reprompts_until_answered(self):
        p = Prompter()
        with mock.patch("builtins.input", side_effect=_feed("", "", "final")):
            self.assertEqual(p.ask("Say something"), "final")


class TestPrompterChoose(unittest.TestCase):
    def test_default_on_empty(self):
        p = Prompter()
        with mock.patch("builtins.input", side_effect=_feed("")):
            self.assertEqual(p.choose("Pick", ["a", "b", "c"], "b"), "b")

    def test_default_by_index(self):
        p = Prompter()
        with mock.patch("builtins.input", side_effect=_feed("")):
            self.assertEqual(p.choose("Pick", ["a", "b", "c"], 2), "c")

    def test_choose_by_number(self):
        p = Prompter()
        with mock.patch("builtins.input", side_effect=_feed("1")):
            self.assertEqual(p.choose("Pick", ["a", "b", "c"], "a"), "b")

    def test_choose_by_name(self):
        p = Prompter()
        with mock.patch("builtins.input", side_effect=_feed("c")):
            self.assertEqual(p.choose("Pick", ["a", "b", "c"], "a"), "c")

    def test_invalid_then_valid(self):
        p = Prompter()
        with mock.patch("builtins.input", side_effect=_feed("banana", "9", "1")):
            self.assertEqual(p.choose("Pick", ["a", "b", "c"], "a"), "b")


class TestPrompterConfirm(unittest.TestCase):
    def test_default_false_on_empty(self):
        p = Prompter()
        with mock.patch("builtins.input", side_effect=_feed("")):
            self.assertFalse(p.confirm("Proceed?"))

    def test_default_true_on_empty(self):
        p = Prompter()
        with mock.patch("builtins.input", side_effect=_feed("")):
            self.assertTrue(p.confirm("Proceed?", default=True))

    def test_yes_no_variants(self):
        p = Prompter()
        for word, expected in (("y", True), ("yes", True), ("Y", True),
                               ("n", False), ("no", False), ("N", False)):
            with mock.patch("builtins.input", side_effect=_feed(word)):
                self.assertEqual(p.confirm("Proceed?"), expected, word)

    def test_invalid_then_valid(self):
        p = Prompter()
        with mock.patch("builtins.input", side_effect=_feed("maybe", "y")):
            self.assertTrue(p.confirm("Proceed?"))


class TestNonInteractivePrompter(unittest.TestCase):
    def test_ask_returns_default(self):
        p = NonInteractivePrompter()
        self.assertEqual(p.ask("Anything?", default="x", flag="--x"), "x")

    def test_ask_raises_naming_flag(self):
        p = NonInteractivePrompter()
        with self.assertRaises(MissingInputError) as ctx:
            p.ask("Anything?", flag="--variables")
        self.assertIn("--variables", str(ctx.exception))
        self.assertIn("non-interactive", str(ctx.exception))

    def test_choose_returns_default_string(self):
        p = NonInteractivePrompter()
        self.assertEqual(p.choose("Pick", ["a", "b"], "b", flag="--x"), "b")

    def test_choose_returns_default_index(self):
        p = NonInteractivePrompter()
        self.assertEqual(p.choose("Pick", ["a", "b"], 1, flag="--x"), "b")

    def test_choose_raises_naming_flag_when_no_default(self):
        p = NonInteractivePrompter()
        with self.assertRaises(MissingInputError) as ctx:
            p.choose("Pick", ["a", "b"], None, flag="--format")
        self.assertIn("--format", str(ctx.exception))

    def test_confirm_never_raises_returns_default(self):
        p = NonInteractivePrompter()
        self.assertFalse(p.confirm("Proceed?"))
        self.assertTrue(p.confirm("Proceed?", default=True))


if __name__ == "__main__":
    unittest.main()
