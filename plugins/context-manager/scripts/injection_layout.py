"""Bounded layout for the six SessionStart hooks; never silently drop overflow.

The complete checkpoint stays on disk. An oversized summary is explicitly an
excerpt (beginning and end), with a path to read the complete summary. PreCompact
must not ask for a short native summary when summary_fits() returns False.
"""
PARTS = 6
CAP = 9600
SUMMARY_BUDGET = PARTS * 8600  # conservative room for hook headers
HEAD = (
    "DETAILED SUMMARY OF THE EARLIER HISTORY (cumulative, written by a separate summarizer). "
    "The native summary covers the recent work. For an exact old fact (number, name, date, file, "
    "or reason for a decision), use session_recall (search, then expand) and read the original "
    "evidence before stating it.\n\n"
)


def summary_fits(text):
    return len(text) <= SUMMARY_BUDGET


def injection_chunks(text, full_path):
    """At most PARTS chunks, each <= CAP characters, with explicit overflow recovery."""
    prefix = HEAD
    body = text
    if not summary_fits(text):
        prefix = (
            "WARNING: SUMMARY EXCERPT, NOT THE COMPLETE CUMULATIVE SUMMARY. "
            "It exceeds the six-hook injection budget. The native compaction was NOT asked "
            "to produce a short summary. The middle is omitted below; do not infer that an "
            "absent item never happened. Use Read on the complete summary file: "
            + str(full_path) + "\nUse session_recall to verify exact historical facts.\n\n"
        )
        # The first prefix includes the complete recovery path; continuation headers
        # reserve 60 characters each. Preserve both the beginning and the file-list tail.
        room = CAP - len(prefix) + (PARTS - 1) * (CAP - 60)
        if room < 200:
            raise ValueError("summary recovery path exceeds the hook output budget")
        keep = min(room - 200, SUMMARY_BUDGET - 200)
        front = keep * 2 // 3
        back = keep - front
        omitted = len(text) - keep
        body = (text[:front] + "\n\n[... " + str(omitted)
                + " characters omitted here; read the complete file above ...]\n\n" + text[-back:])
    chunks = [prefix + body[:CAP - len(prefix)]]
    rest = body[CAP - len(prefix):]
    while rest:
        n = len(chunks) + 1
        chunks.append("(summary continued, part " + str(n) + ")\n" + rest[:CAP - 60])
        rest = rest[CAP - 60:]
    if len(chunks) > PARTS or any(len(chunk) > CAP for chunk in chunks):
        raise ValueError("summary layout exceeds the configured hook budget")
    return chunks
