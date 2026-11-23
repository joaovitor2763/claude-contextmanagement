"""Shared paths for the context-manager hooks and the session_recall server."""
import os, subprocess

DATA = os.environ.get("CLAUDE_PLUGIN_DATA") or os.path.expanduser("~/.claude/context-manager")
SUMMARIES = os.path.join(DATA, "summaries")
SESSIONS = os.path.join(DATA, "sessions")
LOG = os.path.join(DATA, "log.jsonl")
NESTED = "CCM_NESTED"  # set on the summarizer call so its own hooks do nothing


def ppid(pid):
    try:
        out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout
        return int(out.strip() or 0)
    except Exception:
        return 0


def comm(pid):
    try:
        return subprocess.run(["ps", "-o", "comm=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return ""


def claude_pid(start):
    """The Claude Code process above `start` (hooks run under a shell, the MCP server directly)."""
    pid = start
    for _ in range(6):
        if not pid or pid == 1:
            return 0
        if "claude" in os.path.basename(comm(pid)).lower():
            return pid
        pid = ppid(pid)
    return 0
