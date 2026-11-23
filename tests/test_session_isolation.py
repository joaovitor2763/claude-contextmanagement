"""Recall must never infer a session from another transcript's freshness."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, mock_open, patch

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "context-manager" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import common
import register_session
import recall_mcp


class SessionIsolation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sessions = self.root / "data" / "sessions"
        self.sessions.mkdir(parents=True)
        self.first = self.root / "sessão-um.jsonl"
        self.second = self.root / "sessão-dois.jsonl"
        for path, text in ((self.first, "Orçamento reunião às 14h"), (self.second, "PRIVATE OTHER SESSION")):
            path.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": text}},
                                       ensure_ascii=False) + "\n", encoding="utf-8")
        for module in (register_session, recall_mcp):
            for key, value in (("SESSIONS", str(self.sessions)), ("claude_pid", lambda: 4321),
                               ("process_identity", lambda pid: "test:start-A")):
                p = patch.object(module, key, value)
                p.start()
                self.addCleanup(p.stop)
        p = patch.dict(os.environ)
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("RECALL_TRANSCRIPT", None)
        self.hook = {"session_id": "sessão-um", "transcript_path": str(self.first)}

    def save(self, record):
        (self.sessions / "4321.json").write_text(json.dumps(record), encoding="utf-8")

    def registered(self):
        return register_session.register(self.hook)

    def call(self, action="search", **args):
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "session_recall", "arguments": dict(action=action, **args)}}
        output = io.StringIO()
        with patch.object(sys, "stdin", io.StringIO(json.dumps(request) + "\n")), patch.object(sys, "stdout", output):
            recall_mcp.main()
        return json.loads(output.getvalue())["result"]

    def assertToolError(self):
        for action in ("search", "expand"):
            result = self.call(action, query="PRIVATE", **{"from": 0})
            self.assertIs(result.get("isError"), True)
            self.assertIn("error:", result["content"][0]["text"])
            self.assertNotIn("PRIVATE OTHER SESSION", result["content"][0]["text"])

    def test_two_sessions_same_project_no_registry_never_uses_latest(self):
        project = self.root / "same-project"
        project.mkdir()
        # Reproduce the old project-path heuristic, with a newer unrelated file.
        import re
        encoded = re.sub(r"[^a-zA-Z0-9]", "-", str(project))
        old_root = self.root / ".claude" / "projects" / encoded
        old_root.mkdir(parents=True)
        for path in (self.first, self.second):
            (old_root / path.name).write_bytes(path.read_bytes())
        os.utime(old_root / self.second.name, (2000000000, 2000000000))
        with patch.dict(os.environ, HOME=str(self.root), USERPROFILE=str(self.root), CLAUDE_PROJECT_DIR=str(project)):
            self.assertToolError()

    def test_correct_registration_is_atomic_and_session_specific(self):
        with patch.object(register_session.os, "replace", wraps=os.replace) as replace:
            record = self.registered()
        self.assertEqual(replace.call_count, 1)
        self.assertEqual(record["pid"], 4321)
        self.assertEqual(record["session_id"], "sessão-um")
        self.assertEqual(record["process_identity"], "test:start-A")
        self.assertEqual(list(self.sessions.glob("*.tmp")), [])
        self.assertEqual(recall_mcp.transcript(), str(self.first))
        hits = recall_mcp.search("reuniao as 14h")["hits"]
        self.assertEqual(hits[0]["text"], "Orçamento reunião às 14h")
        self.assertEqual(recall_mcp.search("PRIVATE")["hits"], [])

    def test_stale_process_generation_returns_tool_error(self):
        record = self.registered()
        record["process_identity"] = "test:old-generation"
        self.save(record)
        self.assertToolError()

    def test_wrong_pid_and_invalid_schema_return_tool_error(self):
        record = self.registered()
        for changes in ({"pid": 99}, {"pid": True}, {"schema_version": True}, {"schema_version": 2},
                        {"session_id": ""}, {"session_id": []}, {"process_identity": None}):
            with self.subTest(changes=changes):
                self.save(dict(record, **changes))
                self.assertToolError()
        self.save({"session_id": "legacy", "transcript_path": str(self.second)})
        self.assertToolError()

    def test_corrupt_or_non_object_registry_returns_tool_error(self):
        for raw in ("{broken", "[]", "null", '"not a record"'):
            with self.subTest(raw=raw):
                (self.sessions / "4321.json").write_text(raw, encoding="utf-8")
                self.assertToolError()

    def test_missing_unreadable_or_non_regular_transcript_returns_tool_error(self):
        record = self.registered()
        for path in (str(self.root / "absent"), str(self.root), "relative.jsonl"):
            self.save(dict(record, transcript_path=path))
            self.assertToolError()
        self.save(record)
        with patch("common.open", side_effect=PermissionError("denied")):
            self.assertToolError()

    def test_explicit_override_ignores_registry_but_is_validated(self):
        with patch.dict(os.environ, RECALL_TRANSCRIPT=str(self.second)):
            self.assertEqual(recall_mcp.transcript(), str(self.second))
            self.assertEqual(recall_mcp.search("PRIVATE")["total_matches"], 1)
        for path in ("", str(self.root / "absent"), str(self.root)):
            with patch.dict(os.environ, RECALL_TRANSCRIPT=path):
                self.assertToolError()

    def test_unverifiable_process_never_reads_registry(self):
        self.registered()
        with patch.object(recall_mcp, "process_identity", return_value=None):
            self.assertToolError()
        with patch.object(recall_mcp, "claude_pid", return_value=0):
            self.assertToolError()

    def test_generation_changes_during_resolution(self):
        self.registered()
        with patch.object(recall_mcp, "process_identity", side_effect=["test:start-A", "test:start-B"]):
            with self.assertRaisesRegex(ValueError, "changed"):
                recall_mcp.transcript()

    def test_failed_registration_never_replaces_record_and_cleans_temp(self):
        original = self.registered()
        with patch.object(register_session, "process_identity", side_effect=["test:start-A", "test:start-B"]):
            with self.assertRaisesRegex(ValueError, "changed"):
                register_session.register(dict(self.hook, transcript_path=str(self.second)))
        self.assertEqual(json.loads((self.sessions / "4321.json").read_text(encoding="utf-8")), original)
        self.assertEqual(list(self.sessions.glob("*.tmp")), [])
        for hook in ({}, dict(self.hook, session_id=""), dict(self.hook, transcript_path=str(self.root)), []):
            with self.assertRaises(ValueError):
                register_session.register(hook)

    def test_mcp_subprocess_missing_association_is_error_not_empty_results(self):
        env = dict(os.environ, CLAUDE_PLUGIN_DATA=str(self.root / "isolated"))
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"arguments": {"action": "search", "query": "reuniao"}}}
        result = subprocess.run([sys.executable, str(SCRIPTS / "recall_mcp.py")],
                                input=json.dumps(request) + "\n", capture_output=True, text=True, env=env, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIs(json.loads(result.stdout)["result"]["isError"], True)


class ProcessLookup(unittest.TestCase):
    def test_native_process_and_generation(self):
        parent, name = common.proc(os.getpid())
        self.assertEqual(parent, os.getppid())
        self.assertTrue(name)
        identity = common.process_identity(os.getpid())
        self.assertTrue(identity)
        self.assertEqual(identity, common.process_identity(os.getpid()))
        self.assertIsNone(common.process_identity(-1))
        self.assertIsNone(common.process_identity(2147483647))

    def test_only_identified_claude_is_accepted(self):
        for name in ("", "Code Helper", "g4os", "unrelated", "node"):
            with self.subTest(name=name), patch.object(common, "proc", return_value=(55, name)), \
                    patch.object(common, "_node_is_claude", return_value=False):
                self.assertEqual(common.claude_pid(100), 0)
        for name in ("claude", "/usr/bin/claude", r"C:\Program Files\Claude\claude.exe"):
            with patch.object(common, "proc", return_value=(55, name)):
                self.assertEqual(common.claude_pid(100), 100)
        with patch.object(common, "proc", side_effect=[(99, "python3.9"), (98, "sh"), (5, "claude")]):
            self.assertEqual(common.claude_pid(100), 98)
        with patch.object(common, "proc", return_value=(100, "sh")):
            self.assertEqual(common.claude_pid(100), 0)
        with patch.object(common, "proc", return_value=(5, "node")), patch.object(common, "_node_is_claude", return_value=True):
            self.assertEqual(common.claude_pid(100), 100)

    def test_linux_generation_uses_boot_id_and_starttime(self):
        rest = ["S", "7"] + ["0"] * 17 + ["123456"]
        with patch.object(common.sys, "platform", "linux"), patch.object(common, "_linux_stat", return_value=("claude", rest)), \
                patch("builtins.open", mock_open(read_data="boot-uuid\n")):
            self.assertEqual(common.process_identity(7), "linux:boot-uuid:123456")
            self.assertEqual(common.proc(7), (7, "claude"))

    def test_linux_stat_handles_spaces_and_parentheses(self):
        with patch("builtins.open", mock_open(read_data="42 (a tricky (name)) S 21 " + "0 " * 17 + "87654")):
            name, rest = common._linux_stat(42)
        self.assertEqual(name, "a tricky (name)")
        self.assertEqual(rest[1], "21")
        self.assertEqual(rest[19], "87654")

    def test_macos_generation_forces_c_locale_and_normalizes_spaces(self):
        with patch.object(common.sys, "platform", "darwin"), patch.object(common.subprocess, "run", return_value=Mock(stdout="Mon Nov  2 12:34:56 2026\n")) as run:
            self.assertEqual(common.process_identity(123), "darwin:Mon Nov 2 12:34:56 2026")
            self.assertEqual(run.call_args.kwargs["env"]["LC_ALL"], "C")
        with patch.object(common.sys, "platform", "darwin"), patch.object(common.subprocess, "run", return_value=Mock(stdout="")):
            self.assertIsNone(common.process_identity(123))

    def test_windows_generation_types_and_closes_64_bit_handle(self):
        import ctypes
        from ctypes import wintypes
        kernel = Mock()
        kernel.OpenProcess.return_value = 0x123456789ABC
        def times(handle, created, *unused):
            self.assertEqual(handle, 0x123456789ABC)
            stamp = ctypes.cast(created, ctypes.POINTER(wintypes.FILETIME)).contents
            stamp.dwHighDateTime, stamp.dwLowDateTime = 2, 17
            return True
        kernel.GetProcessTimes.side_effect = times
        with patch.object(common.sys, "platform", "win32"), patch.object(common, "_kernel32", return_value=kernel):
            self.assertEqual(common.process_identity(123), "win32:8589934609")
        self.assertIs(kernel.OpenProcess.restype, wintypes.HANDLE)
        self.assertEqual(kernel.GetProcessTimes.argtypes[0], wintypes.HANDLE)
        kernel.CloseHandle.assert_called_once_with(0x123456789ABC)
        kernel.reset_mock()
        kernel.GetProcessTimes.side_effect = None
        kernel.GetProcessTimes.return_value = False
        with patch.object(common.sys, "platform", "win32"), patch.object(common, "_kernel32", return_value=kernel):
            self.assertIsNone(common.process_identity(123))
        kernel.CloseHandle.assert_called_once_with(0x123456789ABC)

    @unittest.skipIf(sys.platform == "win32", "POSIX npm symlink recognition")
    def test_node_absolute_symlink_resolves_only_to_official_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            official = root / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
            official.parent.mkdir(parents=True)
            official.write_text("// official package layout", encoding="utf-8")
            other = root / "unrelated" / "cli.js"
            other.parent.mkdir()
            other.write_text("// unrelated CLI", encoding="utf-8")
            launcher = root / "bin" / "claude"
            launcher.parent.mkdir()
            launcher.symlink_to(official)
            unrelated = root / "bin" / "other-claude"
            unrelated.symlink_to(other)
            with patch.object(common.sys, "platform", "linux"):
                for path, expected in ((launcher, True), (unrelated, False), (other, False)):
                    argv = ("node\0" + str(path) + "\0").encode("utf-8")
                    with patch("builtins.open", mock_open(read_data=argv)):
                        self.assertEqual(common._node_is_claude(123), expected)
                # Relative arguments must not be resolved against the MCP's cwd.
                relative = os.path.relpath(launcher)
                with patch("builtins.open", mock_open(read_data=("node\0" + relative + "\0").encode("utf-8"))):
                    self.assertFalse(common._node_is_claude(123))

    def test_node_path_detection_not_generic_node(self):
        with patch.object(common.sys, "platform", "linux"):
            for cmdline, expected in ((b"node\0/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js\0", True),
                                      (b"node\0/other/cli.js\0", False)):
                with patch("builtins.open", mock_open(read_data=cmdline)):
                    self.assertEqual(common._node_is_claude(123), expected)


if __name__ == "__main__":
    unittest.main()
