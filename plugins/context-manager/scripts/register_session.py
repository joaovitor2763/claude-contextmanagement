#!/usr/bin/env python3
"""SessionStart hook: tell session_recall which transcript belongs to this Claude Code process."""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import NESTED, SESSIONS, claude_pid

if os.environ.get(NESTED):
    sys.exit(0)
hook = json.load(sys.stdin)
pid = claude_pid(os.getppid())
if pid and hook.get("transcript_path"):
    os.makedirs(SESSIONS, exist_ok=True)
    with open(os.path.join(SESSIONS, f"{pid}.json"), "w") as f:
        json.dump({"session_id": hook.get("session_id"), "transcript_path": hook["transcript_path"]}, f)
