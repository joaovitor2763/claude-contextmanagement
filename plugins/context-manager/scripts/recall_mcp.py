#!/usr/bin/env python3
"""session_recall: MCP stdio server that searches the current Claude Code transcript.

Finds the transcript through the SessionStart hook's record for this Claude Code process,
else the newest .jsonl of the project (or $RECALL_TRANSCRIPT). No model: literal search, exact phrase first, then most of the words.
"""
import glob, json, os, re, sys, unicodedata
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import SESSIONS, claude_pid, utf8_stdio

SHOWN, HITS_TOTAL, HITS_PER_MSG, EXPAND_CAP, MESSAGE_CHARS, EXPAND_MAX = 500, 12, 3, 12000, 10000, 20

DESC = ("Recover what an earlier part of this conversation said, including messages and tool results already "
        "compacted out of your context. No model is involved. action=search: finds text in the messages, the full "
        "tool results and the tool call arguments, best matches first (exact phrase first, then messages with most "
        "of the words, ignoring case and accents), and returns ids with the matching line and its neighbours. "
        "action=expand: reads entries by id (from, optional count, default 3) to see the exact content; a long entry "
        "comes in pages: pass its next_offset, or the offset of a hit, as offset to read on. A hit shows only a few "
        "lines: expand it before you answer from it. Use it before stating any old fact (number, name, "
        "date, decision) that is not in your current context.")

SCHEMA = {"type": "object", "properties": {
    "action": {"type": "string", "enum": ["search", "expand"]},
    "query": {"type": "string", "description": "search: words or phrase to find"},
    "from": {"type": "integer", "description": "expand: entry id"},
    "count": {"type": "integer", "description": "expand: how many entries from `from` (max 20)"},
    "offset": {"type": "integer", "description": "expand: character offset inside the first entry, to read a long "
               "entry from a hit's offset or continue from its next_offset"}},
    "required": ["action"]}


def fold(s):
    return "".join(c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn")


def transcript():
    p = os.environ.get("RECALL_TRANSCRIPT")
    if p:
        return p
    pid = claude_pid()  # the SessionStart hook recorded this process's transcript
    reg = os.path.join(SESSIONS, f"{pid}.json")
    if pid and os.path.exists(reg):
        p = json.load(open(reg)).get("transcript_path")
        if p and os.path.exists(p):
            return p
    root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    proj = os.path.expanduser("~/.claude/projects/" + re.sub(r"[^a-zA-Z0-9]", "-", root))
    files = [f for f in glob.glob(proj + "/*.jsonl")]
    return max(files, key=os.path.getmtime) if files else None


def render(content):
    if isinstance(content, str):
        return [("message", None, content)]
    out = []
    for b in content or []:
        t = b.get("type")
        if t == "text":
            out.append(("message", None, b.get("text", "")))
        elif t == "tool_use":
            inp = b.get("input")
            if isinstance(inp, dict):
                txt = "\n".join(f"{k}: {v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)}" for k, v in inp.items())
            else:
                txt = json.dumps(inp, ensure_ascii=False)
            out.append(("tool_call", b.get("name"), txt))
        elif t == "tool_result":
            c = b.get("content")
            if isinstance(c, list):
                c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
            out.append(("tool_result", None, str(c or "")))
    return out


def load():
    path = transcript()
    items = []
    if not path:
        return items
    for line in open(path, encoding="utf8"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("type") not in ("user", "assistant") or d.get("isCompactSummary") or d.get("isMeta"):
            continue
        m = d.get("message") or {}
        for kind, tool, text in render(m.get("content")):
            if text.strip():
                items.append({"id": len(items), "role": m.get("role"), "kind": kind, "tool": tool,
                              "at": d.get("timestamp"), "text": text})
    return items


def hit_window(line, at):
    if len(line) <= SHOWN:
        return line
    start = max(0, min(at - SHOWN // 4, len(line) - SHOWN))
    return line[start:start + SHOWN]


def around_hit(before, line, after, at):
    """The matching line, centred on the match, plus as much of its neighbours as fits in SHOWN."""
    hit = hit_window(line, at)
    room = SHOWN - len(hit)
    before_room = room if after is None else room // 2
    head = before[-(before_room - 1):] if before is not None and before_room > 1 else None
    room -= 0 if head is None else len(head) + 1
    tail = after[:room - 1] if after is not None and room > 1 else None
    return "\n".join(x for x in (head, hit, tail) if x is not None)


def search(q):
    items = load()
    phrase = fold(q.strip())
    words = [w for w in re.findall(r"\w+", phrase) if len(w) > 1]
    ranked = []
    for it in items:
        ft = fold(it["text"])
        lines = it["text"].split("\n")
        if phrase and phrase in ft:
            score = len(words) + 1
            match = [fold(l).find(phrase) for l in lines]
        else:
            present = [w for w in words if w in ft]
            if len(words) > 1 and len(present) >= max(2, -(-len(words) // 2)):
                score = len(present)
                match = [min([fold(l).find(w) for w in present if w in fold(l)] or [-1]) for l in lines]
            elif len(words) == 1 and present:
                score = 1
                match = [fold(l).find(words[0]) for l in lines]
            else:
                continue
        ranked.append((score, it, match, lines))
    ranked.sort(key=lambda x: -x[0])
    hits = []
    for score, it, match, lines in ranked:
        n, line_start = 0, 0
        for i, at in enumerate(match):
            start = line_start
            line_start += len(lines[i]) + 1
            if at == -1:
                continue
            if n == HITS_PER_MSG or len(hits) == HITS_TOTAL:
                break
            n += 1
            h = {"id": it["id"], "kind": it["kind"], "role": it["role"], "at": it["at"], "line": i + 1,
                 "text": around_hit(lines[i - 1] if i else None, lines[i], lines[i + 1] if i + 1 < len(lines) else None, at)}
            if len(lines[i]) > SHOWN or len(it["text"]) > MESSAGE_CHARS:
                h["offset"] = start + at
            if it["tool"]:
                h["tool"] = it["tool"]
            hits.append(h)
        if len(hits) == HITS_TOTAL:
            break
    return {"query": q, "total_matches": len(ranked), "hits": hits,
            "hint": "expand an id to read the exact content before answering"}


def expand(frm, count, offset=0):
    items = load()
    if not 0 <= frm < len(items):
        raise ValueError("unknown entry id; use search to find valid ids")
    count = max(1, min(int(count or 3), EXPAND_MAX))
    out, chars = [], 0
    for pos, it in enumerate(items[frm:frm + count]):
        start = min(max(int(offset or 0), 0), len(it["text"])) if pos == 0 else 0
        t = it["text"][start:start + MESSAGE_CHARS]
        if out and chars + len(t) > EXPAND_CAP:
            break
        chars += len(t)
        e = {k: v for k, v in dict(it, text=t).items() if v is not None}
        if start:
            e["offset"] = start
        if start + len(t) < len(it["text"]):
            e.update(text_truncated=True, next_offset=start + len(t), total_chars=len(it["text"]))
        out.append(e)
    return {"entries": out, "next": frm + len(out) if frm + len(out) < len(items) else None}


def reply(i, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": i}
    if error:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


utf8_stdio()
for raw in sys.stdin:
    try:
        req = json.loads(raw)
    except Exception:
        continue
    m, i = req.get("method"), req.get("id")
    if i is None:
        continue
    if m == "initialize":
        reply(i, {"protocolVersion": req.get("params", {}).get("protocolVersion", "2025-06-18"),
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "recall", "version": "1.0"}})
    elif m == "tools/list":
        reply(i, {"tools": [{"name": "session_recall", "description": DESC, "inputSchema": SCHEMA}]})
    elif m == "tools/call":
        a = req.get("params", {}).get("arguments") or {}
        try:
            r = search(a.get("query", "")) if a.get("action") == "search" else expand(int(a.get("from", 0)), a.get("count"), a.get("offset"))
            reply(i, {"content": [{"type": "text", "text": json.dumps(r, ensure_ascii=False)}]})
        except Exception as e:
            reply(i, {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True})
    else:
        reply(i, {})
