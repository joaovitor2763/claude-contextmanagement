#!/usr/bin/env python3
"""SessionStart: atomically bind this Claude process generation to its transcript."""
import json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import NESTED, SESSIONS, claude_pid, process_identity, readable_transcript, utf8_stdio


def register(hook):
    if not isinstance(hook, dict):
        raise ValueError("SessionStart input must be an object")
    sid = hook.get("session_id")
    if not isinstance(sid, str) or not sid.strip():
        raise ValueError("SessionStart requires a nonempty session_id")
    path = readable_transcript(hook.get("transcript_path"))
    pid = claude_pid()
    identity = process_identity(pid)
    if not pid or not identity:
        raise ValueError("cannot identify the Claude process generation; recall requires RECALL_TRANSCRIPT")
    record = {"schema_version": 1, "pid": pid, "process_identity": identity,
              "session_id": sid, "transcript_path": path}
    os.makedirs(SESSIONS, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", prefix=f".{pid}.",
                                         suffix=".tmp", dir=SESSIONS, delete=False) as fh:
            temp = fh.name
            json.dump(record, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        if process_identity(pid) != identity or claude_pid() != pid:
            raise ValueError("Claude process changed during session registration")
        os.replace(temp, os.path.join(SESSIONS, f"{pid}.json"))
        temp = None
    finally:
        if temp is not None:
            os.unlink(temp)
    return record


def main():
    if os.environ.get(NESTED):
        return
    utf8_stdio()
    try:
        register(json.load(sys.stdin))
    except (OSError, ValueError, TypeError) as exc:
        # Do not break SessionStart, but never manufacture an association.
        print(f"context-manager: session not registered: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
