#!/usr/bin/env python3
"""SessionStart(compact): inject only a current checkpoint, within six bounded parts."""
import json
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import NESTED, utf8_stdio
from injection_layout import PARTS, injection_chunks
from summary_state import load_state, summary_path


def main():
    if os.environ.get(NESTED):
        return
    utf8_stdio()
    part = int(sys.argv[1])
    if not 0 <= part < PARTS:
        return
    hook = json.load(sys.stdin)
    sid = hook["session_id"]
    state = load_state(sid)
    path = summary_path(sid)
    chunks = []
    if state is None:
        if os.path.exists(path):
            chunks = ["WARNING: a legacy or unverified summary exists, but it is not being "
                      "injected as current. Keep the native compaction summary and use "
                      "session_recall for exact historical facts."]
    elif state.get("status") != "ready":
        chunks = ["WARNING: the external summary was not updated successfully for this "
                  "compaction. No stale external summary is being injected. Keep the native "
                  "compaction summary and use session_recall for exact historical facts. "
                  "The next summarization will retry from the last successful checkpoint."]
    elif (not hook.get("transcript_path") or
          os.path.normcase(os.path.realpath(hook["transcript_path"])) != state.get("transcript_path")):
        chunks = ["WARNING: the external summary belongs to a different or unverifiable "
                  "transcript and was not injected. Use the native summary and session_recall."]
    else:
        chunks = injection_chunks(state["summary"], path)
    if part < len(chunks):
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                "additionalContext": chunks[part]}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
