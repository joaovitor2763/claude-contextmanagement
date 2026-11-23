"""Platform checks for the plugin scripts, without a Claude login: run with python -m unittest.

A fake `claude` on PATH stands in for the summarizer, so the PreCompact hook is exercised end to end.
"""
import json, os, shutil, subprocess, sys, tempfile, textwrap, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "plugins", "context-manager", "scripts")
sys.path.insert(0, SCRIPTS)
import common  # noqa: E402

ACCENTS = "Orçamento aprovado: R$ 83.250 — reunião às 14h com a Marina; código ZETA-4471"


def run(script, stdin, env, *args):
    r = subprocess.run([sys.executable, os.path.join(SCRIPTS, script), *args], input=stdin.encode("utf-8"),
                       capture_output=True, env=env, timeout=120)
    return r.returncode, r.stdout.decode("utf-8"), r.stderr.decode("utf-8", "replace")


class Scripts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env = dict(os.environ, CLAUDE_PLUGIN_DATA=os.path.join(self.tmp, "data"))
        self.env.pop("CCM_NESTED", None)
        self.transcript = os.path.join(self.tmp, "session.jsonl")
        rows = [
            {"type": "user", "timestamp": "t1", "message": {"role": "user", "content": ACCENTS}},
            {"type": "assistant", "timestamp": "t2", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "a", "name": "Read", "input": {"file_path": "C:\\proj\\notas.txt"}}]}},
            {"type": "user", "timestamp": "t3", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "a", "content": "x" * 30000 + " fornecedor Beta " + "y" * 30000}]}},
            {"type": "assistant", "timestamp": "t4", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "b", "name": "Edit", "input": {"file_path": "C:\\proj\\decisão.md"}},
                {"type": "text", "text": "Feito."}]}},
        ]
        with open(self.transcript, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_process_lookup(self):
        parent, name = common.proc(os.getpid())
        self.assertEqual(parent, os.getppid())
        self.assertIn("python", name.lower())
        child = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); import common, os; "
                                "print(common.claude_pid(), os.getppid())", SCRIPTS], capture_output=True, text=True)
        found, direct = child.stdout.split()
        # the child's direct parent is this python process, so the lookup must skip it
        self.assertNotEqual(found, direct)

    def test_recall_search_and_paging_utf8(self):
        reqs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "session_recall", "arguments": {"action": "search", "query": "reuniao as 14h"}}},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "session_recall", "arguments": {"action": "search", "query": "fornecedor beta"}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "session_recall", "arguments": {"action": "expand", "from": 2, "count": 1, "offset": 29990}}}]
        env = dict(self.env, RECALL_TRANSCRIPT=self.transcript)
        code, out, err = run("recall_mcp.py", "".join(json.dumps(r) + "\n" for r in reqs), env)
        self.assertEqual(code, 0, err)
        self.assertNotIn("\r", out)
        res = [json.loads(json.loads(l)["result"]["content"][0]["text"]) for l in out.splitlines()[1:]]
        self.assertEqual(res[0]["hits"][0]["text"], ACCENTS)
        self.assertIn("fornecedor Beta", res[1]["hits"][0]["text"])
        self.assertEqual(res[1]["hits"][0]["offset"], 30001)
        page = res[2]["entries"][0]
        self.assertTrue(page["text"].startswith("xxxxxxxxxx fornecedor Beta"))
        self.assertEqual(page["next_offset"], 39990)

    def test_register_and_inject_utf8(self):
        hook = json.dumps({"session_id": "s1", "transcript_path": self.transcript, "source": "compact"})
        code, _, err = run("register_session.py", hook, self.env)
        self.assertEqual(code, 0, err)
        store = os.path.join(self.env["CLAUDE_PLUGIN_DATA"], "summaries")
        os.makedirs(store, exist_ok=True)
        summary = ("## Goal\n" + ACCENTS + "\n") * 300
        with open(os.path.join(store, "s1.md"), "w", encoding="utf-8") as f:
            f.write(summary)
        # The readable .md alone is not a freshness marker: injection requires a checkpoint.
        from hashlib import sha256
        state = {"version": 1, "session_id": "s1", "status": "ready", "summary": summary,
                 "transcript_path": os.path.normcase(os.path.realpath(self.transcript)),
                 "cursor": {"chars": 0, "sha256": sha256(b"").hexdigest()}}
        with open(os.path.join(store, "s1.json"), "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        parts = []
        for i in range(6):
            code, out, err = run("inject_summary.py", hook, self.env, str(i))
            self.assertEqual(code, 0, err)
            if out.strip():
                parts.append(json.loads(out)["hookSpecificOutput"]["additionalContext"])
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= 10000 for p in parts))
        self.assertIn(ACCENTS, "".join(parts))

    def test_precompact_with_fake_claude(self):
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        fake = os.path.join(bindir, "fake_claude.py")
        # The summary arrives in two assistant messages, as when the output cap is hit.
        with open(fake, "w", encoding="utf-8") as f:
            f.write(textwrap.dedent('''
                import json, sys
                sys.stdin.reconfigure(encoding="utf-8"); sys.stdout.reconfigure(encoding="utf-8")
                data = sys.stdin.read()
                assert "<conversation>" in data and "Rules, by value" in data and "--system-prompt-file" in sys.argv
                assert "Orçamento" in data
                for t in ("## Goal\\nResumo com acentuação: ", "continuação. ZETA-4471"):
                    print(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": t}]}}, ensure_ascii=False))
                print(json.dumps({"type": "result", "is_error": False, "total_cost_usd": 0.001}))
            '''))
        if sys.platform == "win32":  # a .cmd shim, like an npm install of Claude Code
            with open(os.path.join(bindir, "claude.cmd"), "w") as f:
                f.write(f'@"{sys.executable}" "{fake}" %*\r\n')
        else:
            path = os.path.join(bindir, "claude")
            with open(path, "w") as f:
                f.write(f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "$@"\n')
            os.chmod(path, 0o755)
        env = dict(self.env, PATH=bindir + os.pathsep + self.env["PATH"])
        hook = json.dumps({"session_id": "s2", "transcript_path": self.transcript, "trigger": "manual"})
        code, out, err = run("precompact_summary.py", hook, env)
        self.assertEqual(code, 0, err)
        self.assertIn("SHORT summary", out)
        with open(os.path.join(self.env["CLAUDE_PLUGIN_DATA"], "summaries", "s2.md"), encoding="utf-8") as f:
            summary = f.read()
        self.assertIn("Resumo com acentuação: continuação. ZETA-4471", summary)
        self.assertIn("<read-files>\nC:\\proj\\notas.txt\n</read-files>", summary)
        self.assertIn("<modified-files>\nC:\\proj\\decisão.md\n</modified-files>", summary)


if __name__ == "__main__":
    unittest.main()
