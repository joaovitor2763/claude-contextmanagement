#!/usr/bin/env python3
"""PreCompact hook: a cheap model writes the cumulative summary of the old history.

Reads the transcript since the last compact boundary, adds the previous cumulative summary,
asks the summarizer (same login, `claude -p --model haiku` by default) for a new cumulative summary with
the checkpoint format of pi-coding-agent (MIT, Mario Zechner) plus summary rules, appends the cumulative lists of files
read and modified, stores it for the SessionStart(compact) hook, and prints short-summary
instructions so the main model writes only a brief summary of the most recent work.
"""
import json, os, re, shutil, subprocess, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import DATA, LOG, NESTED, SUMMARIES as STORE, utf8_stdio

TOOL_RESULT_MAX_CHARS = 2000  # as pi-coding-agent's serializeConversation
TOOL_ARGS_MAX_CHARS = 20000
MAX_OUTPUT_TOKENS = os.environ.get("CCM_MAX_OUTPUT_TOKENS", "15000")  # summary output cap
MODEL = os.environ.get("CCM_SUMMARY_MODEL", "haiku")

SYSTEM = ("You are a context summarization assistant. Your task is to read a conversation between a user and an AI "
          "assistant, then produce a structured summary following the exact format specified.\n\nDo NOT continue the "
          "conversation. Do NOT respond to any questions in the conversation. ONLY output the structured summary.")

FORMAT = """## Goal
[What is the user trying to accomplish? Can be multiple items if the session covers different tasks.]

## Constraints & Preferences
- [Any constraints, preferences, or requirements mentioned by user]
- [Or "(none)" if none were mentioned]

## Progress
### Done
- [x] [Completed tasks/changes]

### In Progress
- [ ] [Current work]

### Blocked
- [Issues preventing progress, if any]

## Key Decisions
- **[Decision]**: [Brief rationale]

## Next Steps
1. [Ordered list of what should happen next]

## Critical Context
- [Any data, examples, or references needed to continue]
- [Or "(none)" if not applicable]

Keep each section concise. Preserve exact file paths, function names, and error messages."""

INITIAL = ("The messages above are a conversation to summarize. Create a structured context checkpoint summary that "
           "another LLM will use to continue the work.\n\nUse this EXACT format:\n\n" + FORMAT)

UPDATE = """The messages above are NEW conversation messages to incorporate into the existing summary provided in <previous-summary> tags.

Update the existing structured summary with new information. RULES:
- PRESERVE all existing information from the previous summary
- ADD new progress, decisions, and context from the new messages
- UPDATE the Progress section: move items from "In Progress" to "Done" when completed
- UPDATE "Next Steps" based on what was accomplished
- PRESERVE exact file paths, function names, and error messages
- If something is no longer relevant, you may remove it

Use this EXACT format:

""" + FORMAT

RULES = "\n".join([
    "Rules, by value:",
    "1. The person's words matter most: orders, decisions, corrections, preferences and their reasons. Keep them close to verbatim, however short.",
    "2. Then anything with lasting effect, and what failed and why, with the exact error.",
    "3. Then findings, open questions and the assistant's answers.",
    "4. Least of all, tool steps: what was done to what, and the outcome.",
    "- When a value was corrected, keep the latest as current and mark the old one as replaced.",
    "- Never make anything look further along than it was.",
    "- Copy names, numbers, ids, codes, e-mails, dates, paths and errors exactly.",
    "- Name a minor item in a few words rather than drop it: an absent item can never be found.",
    "- Write in the conversation's language.",
    "- Keep what identifies each item as the conversation described it: whose or what it is, its area or department, the system or category, what it is for. A value without that context cannot be found again.",
    "- Completeness beats brevity: aim to finish the summary within 13000 tokens. This is a target ceiling, not a minimum; use less when the conversation needs less. Keep each item's context rather than only its values.",
    "- The output limit is at most 15000 tokens, including reasoning when applicable. If the model has a smaller output limit, finish within that limit. Prioritize the rules above and finish every section before the limit; do not use the extra room to expand the summary.",
])

READS = {"Read": "file_path", "NotebookRead": "notebook_path"}
WRITES = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
FILES_RE = re.compile(r"\n*<(read-files|modified-files)>\n(.*?)\n</\1>", re.S)


def cut(text, n):
    return text if len(text) <= n else f"{text[:n]}\n\n[... {len(text) - n} more characters truncated]"


def serialize(role, content, files):
    """One transcript message as pi-coding-agent serializes it for summaries, plus file operations."""
    if isinstance(content, str):
        return [f"[{'User' if role == 'user' else 'Assistant'}]: {content}"] if content.strip() else []
    out, texts, thinking, calls = [], [], [], []
    for b in content or []:
        t = b.get("type")
        if t == "text" and b.get("text", "").strip():
            texts.append(b["text"])
        elif t == "thinking" and b.get("thinking"):
            thinking.append(b["thinking"])
        elif t == "tool_use":
            inp = b.get("input") or {}
            name = b.get("name")
            args = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in inp.items()) if isinstance(inp, dict) else json.dumps(inp)
            calls.append(f"{name}({cut(args, TOOL_ARGS_MAX_CHARS)})")
            if isinstance(inp, dict):
                if isinstance(inp.get(READS.get(name, "")), str):
                    files["read"].add(inp[READS[name]])
                if isinstance(inp.get(WRITES.get(name, "")), str):
                    files["modified"].add(inp[WRITES[name]])
        elif t == "tool_result":
            c = b.get("content")
            if isinstance(c, list):
                c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
            if c:
                out.append(f"[Tool result]: {cut(str(c), TOOL_RESULT_MAX_CHARS)}")
    if role == "user" and texts:
        out.insert(0, "[User]: " + "\n".join(texts))
    if role == "assistant":
        if thinking:
            out.append("[Assistant thinking]: " + "\n".join(thinking))
        if texts:
            out.append("[Assistant]: " + "\n".join(texts))
        if calls:
            out.append("[Assistant tool calls]: " + "; ".join(calls))
    return out


def split_files(summary):
    files = {"read": set(), "modified": set()}
    for tag, body in FILES_RE.findall(summary):
        files["read" if tag == "read-files" else "modified"].update(x for x in body.split("\n") if x.strip())
    return FILES_RE.sub("", summary).rstrip(), files


def with_files(summary, files):
    modified = sorted(files["modified"])
    read = sorted(files["read"] - files["modified"])
    parts = [f"<read-files>\n{chr(10).join(read)}\n</read-files>"] if read else []
    if modified:
        parts.append(f"<modified-files>\n{chr(10).join(modified)}\n</modified-files>")
    return summary + ("\n\n" + "\n\n".join(parts) if parts else "")


def main():
    if os.environ.get(NESTED):
        return
    utf8_stdio()
    hook = json.load(sys.stdin)
    sid, path = hook["session_id"], hook["transcript_path"]
    os.makedirs(STORE, exist_ok=True)
    lines = open(path, encoding="utf8").read().splitlines()
    start = 0
    for i, l in enumerate(lines):
        if '"compact_boundary"' in l:
            start = i + 1
    prev_path = os.path.join(STORE, sid + ".md")
    previous, files = "", {"read": set(), "modified": set()}
    if os.path.exists(prev_path):
        previous, files = split_files(open(prev_path, encoding="utf8").read())
    parts = []
    for l in lines[start:]:
        try:
            d = json.loads(l)
        except Exception:
            continue
        if d.get("type") not in ("user", "assistant") or d.get("isCompactSummary") or d.get("isMeta"):
            continue
        parts += serialize(d["type"], (d.get("message") or {}).get("content"), files)
    if not parts and previous:
        return  # nothing new since the last summary: keep it, let Claude Code summarize normally
    body = f"<conversation>\n{chr(10).join(parts)}\n</conversation>\n\n"
    if previous:
        body += f"<previous-summary>\n{previous}\n</previous-summary>\n\n"
    instructions = f"{UPDATE if previous else INITIAL}\n\nAdditional focus: {RULES}"
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_CODE_AUTO_COMPACT")}
    env[NESTED] = "1"
    env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = MAX_OUTPUT_TOKENS
    # Only needed when Claude Code logs in through an environment variable (CI, sandboxes):
    # hooks don't receive it, so point CCM_ENV_FILE at a KEY=VALUE file that has it.
    envf = os.environ.get("CCM_ENV_FILE", "")
    if envf and os.path.exists(envf):
        for l in open(envf, encoding="utf-8"):
            k, _, v = l.rstrip("\n").partition("=")
            if k and k not in env:
                env[k] = v
    # Everything long goes through stdin and a file: on Windows `claude` can be a .cmd shim, which
    # would mangle multi-line arguments.
    claude = shutil.which("claude") or "claude"
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write(SYSTEM)
    t0 = time.time()
    try:
        r = subprocess.run([claude, "-p", "--model", MODEL, "--tools", "", "--no-session-persistence",
                            "--setting-sources", "user", "--strict-mcp-config", "--system-prompt-file", f.name,
                            "--output-format", "stream-json", "--verbose"],
                           input=body + instructions, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", cwd=tempfile.gettempdir(), timeout=900, env=env)
    finally:
        os.unlink(f.name)
    # When the summary hits the output cap, Claude Code continues it in a new assistant message and
    # `result` holds only that last piece, so join the text of every assistant message instead.
    out, texts, messages = {}, [], 0
    for line in r.stdout.splitlines():
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("type") == "assistant":
            t = "".join(b.get("text", "") for b in (ev.get("message") or {}).get("content") or [] if b.get("type") == "text")
            if t:
                texts.append(t)
                messages += 1
        elif ev.get("type") == "result":
            out = ev
    summary = "".join(texts).strip()
    ok = summary and r.returncode == 0 and not out.get("is_error")
    os.makedirs(DATA, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"session": sid, "secs": round(time.time() - t0, 1), "input_chars": len(body), "model": MODEL,
                            "summary_chars": len(summary), "cost_usd": out.get("total_cost_usd"),
                            "usage": out.get("usage"), "rc": r.returncode, "stop_reason": out.get("stop_reason"),
                            "text_messages": messages,
                            "ok": bool(ok), "err": r.stderr[-500:],
                            "head": (summary or r.stdout)[:300]}) + "\n")
    if ok:
        open(prev_path, "w", encoding="utf8").write(with_files(summary, files))
        print("Write a SHORT summary, at most 600 words. Cover only the most recent work in detail: the last user "
              "requests in their own words, the current task state, the files and values in use, and the next step. "
              "Do not summarize the older history: a separate detailed summary of the whole conversation is "
              "re-injected right after compaction.")
    # on failure print nothing: Claude Code writes its normal full summary


main()
