#!/usr/bin/env python3
"""SessionStart(compact) hook: re-inject part N (0-5) of the cumulative summary (Claude Code caps each hook at 10,000 chars)."""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import NESTED, SUMMARIES, utf8_stdio

if os.environ.get(NESTED):
    sys.exit(0)
PART, CAP = int(sys.argv[1]), 9600
utf8_stdio()
hook = json.load(sys.stdin)
path = os.path.join(SUMMARIES, f"{hook['session_id']}.md")
if not os.path.exists(path):
    sys.exit(0)
s = open(path, encoding="utf8").read()
head = ("DETAILED SUMMARY OF THE EARLIER HISTORY (cumulative, written by a separate summarizer). The short summary above "
        "covers only the recent work. For any exact old fact (number, name, date, file, the reason for a decision), use "
        "the session_recall tool of the recall server (search, then expand) and read the original text before stating it.\n\n")
chunks, first = [], CAP - len(head)
chunks.append(head + s[:first]); rest = s[first:]
while rest:
    chunks.append(f"(summary continued, part {len(chunks) + 1})\n" + rest[:CAP - 60]); rest = rest[CAP - 60:]
if PART < len(chunks):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": chunks[PART]}},
                     ensure_ascii=False))
