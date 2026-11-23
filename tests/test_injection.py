"""Regression tests for bounded injection and explicit failure/overflow recovery."""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import runpy
import sys
import tempfile
import types
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "context-manager" / "scripts"
sys.path.insert(0, str(SCRIPTS))
from injection_layout import CAP, HEAD, PARTS, SUMMARY_BUDGET, injection_chunks, summary_fits


class Layout(unittest.TestCase):
    def test_exact_payload_reassembled_at_budget(self):
        text = "BEGIN\n" + "á" * (SUMMARY_BUDGET - 10) + "\nEND"
        self.assertEqual(len(text), SUMMARY_BUDGET)
        self.assertTrue(summary_fits(text))
        parts = injection_chunks(text, "/tmp/summary.md")
        self.assertLessEqual(len(parts), PARTS)
        self.assertTrue(all(len(p) <= CAP for p in parts))
        joined = "".join(re.sub(r"^\(summary continued, part \d+\)\n", "", p) for p in parts)
        self.assertEqual(joined, HEAD + text)

    def test_oversize_keeps_tail_and_recovery_path(self):
        text = "GOAL_MARKER\n" + "x" * 60000 + "\n<modified-files>\n/project/critical.py\n</modified-files>"
        parts = injection_chunks(text, "/tmp/full summary.md")
        joined = "".join(parts)
        self.assertLessEqual(len(parts), PARTS)
        self.assertTrue(all(len(p) <= CAP for p in parts))
        for marker in ("SUMMARY EXCERPT", "GOAL_MARKER", "/project/critical.py", "/tmp/full summary.md", "characters omitted", "NOT asked"):
            self.assertIn(marker, joined)
        self.assertFalse(summary_fits(text))

    def test_first_character_over_budget_has_positive_omission(self):
        text = "x" * SUMMARY_BUDGET + "Z"
        joined = "".join(injection_chunks(text, "/tmp/full.md"))
        found = re.search(r"(\d+) characters omitted", joined)
        self.assertIsNotNone(found)
        self.assertGreater(int(found.group(1)), 0)
        self.assertTrue(joined.endswith("Z"))

    def test_empty_summary_and_long_path_are_bounded(self):
        self.assertEqual(injection_chunks("", "/tmp/full.md"), [HEAD])
        parts = injection_chunks("x" * 100000, "/tmp/" + "p" * 4000)
        self.assertTrue(all(len(p) <= CAP for p in parts))
        with self.assertRaises(ValueError):
            injection_chunks("x" * 100000, "p" * 60000)


class Injection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "s1.md")
        self.transcript = os.path.normcase(os.path.realpath(str(Path(self.tmp.name) / "s1.jsonl")))

    def emit(self, state, part=0, transcript=None):
        common = types.ModuleType("common")
        common.NESTED = "CCM_NESTED"
        common.utf8_stdio = lambda: None
        storage = types.ModuleType("summary_state")
        storage.load_state = lambda sid: state
        storage.summary_path = lambda sid: self.path
        hook = {"session_id": "s1", "transcript_path": transcript or self.transcript}
        output = io.StringIO()
        with mock.patch.dict(sys.modules, {"common": common, "summary_state": storage}), \
             mock.patch.object(sys, "argv", ["inject_summary.py", str(part)]), \
             mock.patch.object(sys, "stdin", io.StringIO(json.dumps(hook))), \
             mock.patch.dict(os.environ, {"CCM_NESTED": ""}), \
             contextlib.redirect_stdout(output):
            runpy.run_path(str(SCRIPTS / "inject_summary.py"), run_name="__main__")
        text = output.getvalue()
        return json.loads(text)["hookSpecificOutput"]["additionalContext"] if text else ""

    def test_only_ready_checkpoint_is_injected(self):
        state = {"status": "ready", "summary": "CURRENT FACT", "transcript_path": self.transcript}
        self.assertIn("CURRENT FACT", self.emit(state))
        self.assertEqual(self.emit(state, PARTS), "")

    def test_pending_failed_and_legacy_do_not_inject_old_fact(self):
        for status in ("pending", "failed"):
            state = {"status": status, "summary": "OBSOLETE FACT", "transcript_path": self.transcript}
            with self.subTest(status=status):
                output = self.emit(state)
                self.assertIn("WARNING", output)
                self.assertNotIn("OBSOLETE FACT", output)
                self.assertEqual(self.emit(state, 1), "")
        Path(self.path).write_text("OBSOLETE LEGACY FACT", encoding="utf-8")
        output = self.emit(None)
        self.assertIn("unverified", output)
        self.assertNotIn("OBSOLETE LEGACY FACT", output)

    def test_no_checkpoint_is_silent_and_mismatch_is_not_injected(self):
        self.assertEqual(self.emit(None), "")
        state = {"status": "ready", "summary": "OTHER SESSION FACT", "transcript_path": self.transcript + ".other"}
        output = self.emit(state)
        self.assertIn("unverifiable", output)
        self.assertNotIn("OTHER SESSION FACT", output)


if __name__ == "__main__":
    unittest.main()
