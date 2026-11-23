"""External-summary lifecycle regressions. Fake Claude only: no login or paid calls."""
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tempfile
import textwrap
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "context-manager" / "scripts"


class SummaryStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.scripts = self.root / "scripts"
        self.scripts.mkdir()
        for name in ("common.py", "summary_state.py", "precompact_summary.py"):
            shutil.copyfile(SCRIPTS / name, self.scripts / name)
        shutil.copyfile(SCRIPTS / "injection_layout.py", self.scripts / "injection_layout.py")
        self.data = self.root / "data"
        self.transcript = self.root / "transcript.jsonl"
        self.transcript.write_text("", encoding="utf-8")
        self.capture = self.root / "payloads.jsonl"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        # Python resolves the fake executable on PATH; it records every actual payload.
        fake = self.bin / "fake_claude.py"
        fake.write_text(textwrap.dedent('''\
            import json, os, sys
            sys.stdin.reconfigure(encoding="utf-8")
            sys.stdout.reconfigure(encoding="utf-8", newline="\\n")
            sys.stderr.reconfigure(encoding="utf-8", newline="\\n")
            payload = sys.stdin.read()
            with open(os.environ["TEST_CAPTURE"], "a", encoding="utf-8") as f:
                f.write(json.dumps(payload) + "\\n")
            mode = os.environ.get("TEST_MODE", "success")
            if mode == "failure":
                print("SECRET_TOKEN_do_not_log", file=sys.stderr)
                sys.exit(1)
            if mode == "empty":
                print(json.dumps({"type": "result", "is_error": False}))
                sys.exit(0)
            if mode in ("error", "missing_result"):
                print(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "partial"}]}}))
                if mode == "error":
                    print(json.dumps({"type": "result", "is_error": True}))
                sys.exit(0)
            texts = ["x" * 120000] if mode == "large" else ["## Goal\\nCumulative ", "summary."]
            for text in texts:
                print(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}))
            print(json.dumps({"type": "result", "is_error": False, "result": texts[-1]}))
            '''), encoding="utf-8")
        if os.name == "nt":
            launcher = self.bin / "claude.cmd"
            launcher.write_text('@"' + sys.executable + '" "' + str(fake) + '" %*\n', encoding="utf-8")
        else:
            launcher = self.bin / "claude"
            launcher.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " "
                                + shlex.quote(str(fake)) + ' "$@"\n', encoding="utf-8")
            launcher.chmod(0o700)
        self.env = {**os.environ, "CLAUDE_PLUGIN_DATA": str(self.data),
                    "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
                    "TEST_CAPTURE": str(self.capture), "PYTHONPATH": str(self.scripts)}
        self.env.pop("CCM_NESTED", None)
        self.env.pop("CCM_ENV_FILE", None)
        self.hook = {"session_id": "test-session", "transcript_path": str(self.transcript)}

    def record(self, content, role="user", **extra):
        with self.transcript.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"type": role, "message": {"content": content}, **extra}, ensure_ascii=False) + "\n")

    def boundary(self):
        with self.transcript.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "system", "subtype": "compact_boundary"}) + "\n")

    def python(self, code, **env):
        r = subprocess.run([sys.executable, "-c", textwrap.dedent(code)], env={**self.env, **env},
                           capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def run_hook(self, mode="success", bootstrap=None):
        env = {**self.env, "TEST_MODE": mode}
        command = ([sys.executable, "-c", textwrap.dedent(bootstrap)] if bootstrap
                   else [sys.executable, str(self.scripts / "precompact_summary.py")])
        r = subprocess.run(command, input=json.dumps(self.hook), env=env, capture_output=True,
                           text=True, encoding="utf-8", timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def state(self):
        return json.loads((self.data / "summaries" / "test-session.json").read_text(encoding="utf-8"))

    def payloads(self):
        return [json.loads(line) for line in self.capture.read_text(encoding="utf-8").splitlines()]

    def test_success_correction_failure_retry_replays_since_success_not_boundary(self):
        self.record("Original preference: blue.")
        self.record([{"type": "tool_use", "name": "Read", "input": {"file_path": "/old.py"}}], "assistant")
        self.assertIn("SHORT", self.run_hook().stdout)
        baseline = self.state()
        self.assertEqual(baseline["status"], "ready")
        self.assertIn("Cumulative summary.", baseline["summary"])
        self.boundary()
        self.record("Correction: use verde, not blue.")
        self.record([{"type": "tool_use", "name": "Edit", "input": {"file_path": "/old.py"}}], "assistant")
        self.assertEqual(self.run_hook("failure").stdout, "")
        failed = self.state()
        self.assertEqual(failed["status"], "failed")
        for key in ("summary", "cursor"):
            self.assertEqual(failed[key], baseline[key])
        self.boundary()
        self.record("Native compact summary must not be recursively summarized.", isCompactSummary=True)
        self.record("Continue with the corrected preference.")
        self.assertIn("SHORT", self.run_hook().stdout)
        payload = self.payloads()[-1]
        conversation = payload.split("</conversation>")[0]
        self.assertIn("Correction: use verde, not blue.", conversation)
        self.assertIn("Continue with the corrected preference.", conversation)
        self.assertNotIn("Original preference", conversation)
        self.assertNotIn("Native compact", payload)
        self.assertIn("<previous-summary>", payload)
        ready = self.state()
        self.assertEqual(ready["status"], "ready")
        self.assertNotEqual(ready["cursor"], baseline["cursor"])
        self.assertIn("<modified-files>\n/old.py\n</modified-files>", ready["summary"])
        self.assertNotIn("<read-files>", ready["summary"])
        self.assertEqual((self.data / "summaries" / "test-session.md").read_text(encoding="utf-8"), ready["summary"])
        self.assertNotIn("SECRET_TOKEN", (self.data / "log.jsonl").read_text(encoding="utf-8"))

    def test_timeout_preserves_baseline_and_logs_without_secrets(self):
        self.record("Initial")
        self.run_hook()
        baseline = self.state()
        self.record("Must recover this correction")
        r = self.run_hook(bootstrap='''\
            import runpy, subprocess
            from unittest.mock import patch
            def timeout(*args, **kwargs):
                raise subprocess.TimeoutExpired("SECRET_COMMAND", 900, output="SECRET_OUTPUT", stderr="SECRET_STDERR")
            with patch("subprocess.run", side_effect=timeout):
                runpy.run_module("precompact_summary", run_name="__main__")
            ''')
        self.assertEqual(r.stdout, "")
        failed = self.state()
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["summary"], baseline["summary"])
        self.assertEqual(failed["cursor"], baseline["cursor"])
        logs = (self.data / "log.jsonl").read_text(encoding="utf-8")
        self.assertIn('"reason": "timeout"', logs)
        self.assertNotIn("SECRET", logs)
        self.boundary()
        self.run_hook()
        self.assertIn("Must recover this correction", self.payloads()[-1])

    def test_killed_attempt_leaves_pending_not_ready(self):
        self.record("Baseline")
        self.run_hook()
        baseline = self.state()
        self.record("New request")
        self.run_hook(bootstrap='''\
            import runpy
            from unittest.mock import patch
            with patch("subprocess.run", side_effect=SystemExit(0)):
                runpy.run_module("precompact_summary", run_name="__main__")
            ''')
        state = self.state()
        self.assertEqual(state["status"], "pending")
        self.assertEqual(state["cursor"], baseline["cursor"])
        self.assertEqual(state["summary"], baseline["summary"])
        self.boundary()
        self.run_hook()
        self.assertIn("New request", self.payloads()[-1])

    def test_rewritten_prefix_replays_from_start_and_ignores_native_summary(self):
        self.record("Old history")
        self.run_hook()
        self.transcript.write_text("", encoding="utf-8")
        self.record("Correction before the new boundary")
        self.boundary()
        self.record("Do not include native summary", isCompactSummary=True)
        self.record("Window after boundary")
        self.run_hook()
        payload = self.payloads()[-1]
        self.assertIn("Correction before the new boundary", payload)
        self.assertIn("Window after boundary", payload)
        self.assertNotIn("Do not include native summary", payload)
        self.assertIn("Cumulative summary.", payload)

    def test_legacy_markdown_is_not_automatically_ready_or_baseline(self):
        self.python('''\
            from pathlib import Path
            from summary_state import summary_path, load_state
            path = Path(summary_path("test-session"))
            path.parent.mkdir(parents=True)
            path.write_text("OBSOLETE LEGACY", encoding="utf-8")
            assert load_state("test-session") is None
            ''')
        self.record("Retained message before boundary")
        self.boundary()
        self.record("Fresh message")
        self.run_hook()
        payload = self.payloads()[-1]
        self.assertIn("Retained message before boundary", payload)
        self.assertNotIn("OBSOLETE LEGACY", payload)
        self.assertNotIn("<previous-summary>", payload)

    def test_corrupt_and_invalid_checkpoints_fail_closed(self):
        self.python('''\
            import json
            from pathlib import Path
            from summary_state import load_state, state_path
            path = Path(state_path("test-session"))
            path.parent.mkdir(parents=True)
            invalid = ["{", "[]", "null", json.dumps({"version": 1, "session_id": "other"})]
            for text in invalid:
                path.write_text(text, encoding="utf-8")
                assert load_state("test-session") is None
            assert load_state("../escape") is None
            ''')
        self.record("Recover after corrupt checkpoint")
        self.assertIn("SHORT", self.run_hook().stdout)
        self.assertEqual(self.state()["status"], "ready")

    def test_state_atomic_replace_keeps_previous_json_if_replace_fails(self):
        self.record("Baseline")
        self.run_hook()
        self.python('''\
            from pathlib import Path
            from unittest.mock import patch
            from summary_state import load_state, save_state, state_path
            path = Path(state_path("test-session"))
            original = path.read_bytes()
            state = load_state("test-session")
            state["status"] = "pending"
            with patch("summary_state.os.replace", side_effect=OSError("simulated failure")):
                try:
                    save_state("test-session", state)
                except OSError:
                    pass
                else:
                    raise AssertionError("write unexpectedly succeeded")
            assert path.read_bytes() == original
            assert list(path.parent.glob(".summary-*.tmp")) == []
            save_state("test-session", state)
            assert load_state("test-session")["status"] == "pending"
            ''')

    def test_oversize_including_file_tags_never_prints_short(self):
        self.record("Read many files")
        self.record([{"type": "tool_use", "name": "Read", "input": {"file_path": "/" + "a" * 120000}}], "assistant")
        self.assertEqual(self.run_hook().stdout, "")
        state = self.state()
        self.assertEqual(state["status"], "ready")
        self.assertIn("Cumulative summary.", state["summary"])
        self.assertGreater(len(state["summary"]), 120000)
        self.assertIsNotNone(state["cursor"])
        log = json.loads((self.data / "log.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(log["reason"], "summary_too_large")
        self.assertTrue(log["ok"])
        self.assertFalse(log["injection_complete"])
        self.python('''\
            from injection_layout import injection_chunks
            from summary_state import load_state, summary_path
            state = load_state("test-session")
            chunks = injection_chunks(state["summary"], summary_path("test-session"))
            assert len(chunks) <= 6
            assert all(len(chunk) <= 9600 for chunk in chunks)
            assert "SUMMARY EXCERPT" in chunks[0]
            assert "</read-files>" in chunks[-1]
            ''')
        self.boundary()
        self.assertEqual(self.run_hook().stdout, "")
        self.assertEqual(self.state()["status"], "ready")
        self.assertEqual(len(self.payloads()), 1)

    def test_nonzero_empty_and_result_error_preserve_baseline(self):
        self.record("Baseline")
        self.run_hook()
        baseline = self.state()
        for mode in ("failure", "empty", "error", "missing_result"):
            with self.subTest(mode=mode):
                self.record("Retry correction " + mode)
                self.assertEqual(self.run_hook(mode).stdout, "")
                failed = self.state()
                self.assertEqual(failed["status"], "failed")
                self.assertEqual(failed["summary"], baseline["summary"])
                self.assertEqual(failed["cursor"], baseline["cursor"])

    def test_cursor_is_verifiable_and_rejects_partial_jsonl_extension(self):
        self.python('''\
            from summary_state import make_cursor, cursor_matches
            text = '{"message":"açúcar"}\\n'
            cursor = make_cursor(text)
            assert cursor["chars"] == len(text)
            assert cursor_matches(cursor, text + "new\\n")
            assert not cursor_matches(cursor, text.replace("açúcar", "coffee"))
            assert not cursor_matches(cursor, "short")
            assert not cursor_matches(make_cursor('{"partial"'), '{"partial":true}\\n')
            ''')

    def test_mirror_failure_keeps_old_checkpoint_cursor_for_retry(self):
        self.record("Baseline")
        self.run_hook()
        baseline = self.state()
        mirror = self.data / "summaries" / "test-session.md"
        original_mirror = mirror.read_text(encoding="utf-8")
        self.record("Correction requiring recovery after mirror failure")
        self.record([{"type": "tool_use", "name": "Edit", "input": {"file_path": "/after.py"}}], "assistant")
        r = self.run_hook(bootstrap='''\
            import runpy
            from unittest.mock import patch
            with patch("summary_state.save_summary", side_effect=OSError("mirror unavailable")):
                runpy.run_module("precompact_summary", run_name="__main__")
            ''')
        self.assertEqual(r.stdout, "")
        self.assertEqual(self.state()["status"], "failed")
        self.assertEqual(self.state()["cursor"], baseline["cursor"])
        self.assertEqual(self.state()["summary"], baseline["summary"])
        self.assertEqual(mirror.read_text(encoding="utf-8"), original_mirror)
        self.boundary()
        self.run_hook()
        self.assertIn("Correction requiring recovery after mirror failure", self.payloads()[-1])
        self.assertIn("/after.py", self.state()["summary"])
        self.assertEqual(mirror.read_text(encoding="utf-8"), self.state()["summary"])

    def test_checkpoint_failure_after_mirror_keeps_old_cursor(self):
        self.record("Baseline")
        self.run_hook()
        baseline = self.state()
        self.record("Correction after canonical write failure")
        self.record([{"type": "tool_use", "name": "Edit", "input": {"file_path": "/new-mirror.py"}}], "assistant")
        r = self.run_hook(bootstrap='''\
            import runpy
            import summary_state
            from unittest.mock import patch
            original = summary_state.save_state
            def fail_ready(sid, state):
                if state["status"] == "ready":
                    raise OSError("canonical write unavailable")
                return original(sid, state)
            with patch("summary_state.save_state", side_effect=fail_ready):
                runpy.run_module("precompact_summary", run_name="__main__")
            ''')
        self.assertEqual(r.stdout, "")
        self.assertEqual(self.state()["status"], "failed")
        self.assertEqual(self.state()["cursor"], baseline["cursor"])
        self.assertEqual(self.state()["summary"], baseline["summary"])
        mirror = self.data / "summaries" / "test-session.md"
        self.assertIn("/new-mirror.py", mirror.read_text(encoding="utf-8"))
        self.boundary()
        self.run_hook()
        payload = self.payloads()[-1]
        self.assertIn("Correction after canonical write failure", payload)
        self.assertNotIn("/new-mirror.py", payload.split("<previous-summary>")[-1].split("</previous-summary>")[0])
        self.assertEqual(mirror.read_text(encoding="utf-8"), self.state()["summary"])

    def test_initial_failure_has_no_success_cursor(self):
        self.record("Unprocessed request")
        self.assertEqual(self.run_hook("failure").stdout, "")
        state = self.state()
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["summary"], "")
        self.assertIsNone(state["cursor"])
        self.boundary()
        self.run_hook()
        self.assertIn("Unprocessed request", self.payloads()[-1])

    def test_different_transcript_path_does_not_reuse_baseline(self):
        self.record("Old transcript")
        self.run_hook()
        self.transcript = self.root / "different.jsonl"
        self.hook["transcript_path"] = str(self.transcript)
        self.record("New transcript")
        self.run_hook()
        self.assertNotIn("<previous-summary>", self.payloads()[-1])
        self.assertEqual(self.state()["transcript_path"], os.path.normcase(os.path.realpath(self.transcript)))

    def test_missing_transcript_marks_previous_state_failed(self):
        self.record("Baseline")
        self.run_hook()
        baseline = self.state()
        self.transcript.unlink()
        self.assertEqual(self.run_hook().stdout, "")
        self.assertEqual(self.state()["status"], "failed")
        self.assertEqual(self.state()["cursor"], baseline["cursor"])

    def test_no_new_messages_keeps_verified_ready_without_summarizer(self):
        self.record("Baseline")
        self.run_hook()
        baseline = self.state()
        self.boundary()
        self.record("Native compact summary", isCompactSummary=True)
        self.assertEqual(self.run_hook().stdout, "")
        self.assertEqual(self.state()["status"], "ready")
        self.assertEqual(self.state()["cursor"], baseline["cursor"])
        self.assertEqual(len(self.payloads()), 1)


if __name__ == "__main__":
    unittest.main()
