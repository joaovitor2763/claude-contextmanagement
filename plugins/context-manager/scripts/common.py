"""Shared paths and process lookup for the context-manager hooks and the session_recall server.

Works on Linux, macOS and Windows with the standard library only.
"""
import io, os, subprocess, sys

DATA = os.environ.get("CLAUDE_PLUGIN_DATA") or os.path.join(os.path.expanduser("~"), ".claude", "context-manager")
SUMMARIES = os.path.join(DATA, "summaries")
SESSIONS = os.path.join(DATA, "sessions")
LOG = os.path.join(DATA, "log.jsonl")
NESTED = "CCM_NESTED"  # set on the summarizer call so its own hooks do nothing

# Processes that can sit between Claude Code and these scripts: interpreters, launchers, shells.
SKIP = ("python", "py", "pythonw", "sh", "bash", "dash", "zsh", "cmd", "powershell", "pwsh", "env")


def utf8_stdio():
    """Claude Code speaks UTF-8 with \\n line ends; Windows consoles default to a code page and \\r\\n."""
    for name in ("stdin", "stdout"):
        stream = getattr(sys, name)
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", newline="\n" if name == "stdout" else None)


def _windows_processes():
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    procs, e = {}, Entry()
    e.dwSize = ctypes.sizeof(Entry)
    ok = k32.Process32FirstW(snap, ctypes.byref(e))
    while ok:
        procs[e.th32ProcessID] = (e.th32ParentProcessID, e.szExeFile)
        ok = k32.Process32NextW(snap, ctypes.byref(e))
    k32.CloseHandle(snap)
    return procs


def proc(pid):
    """(parent pid, executable name) of a process, or (0, "")."""
    try:
        if sys.platform == "win32":
            return _windows_processes().get(pid, (0, ""))
        if os.path.exists(f"/proc/{pid}/stat"):
            with open(f"/proc/{pid}/stat", "rb") as fh:
                stat = fh.read().decode("utf-8", "replace")
            name, rest = stat[stat.index("(") + 1:stat.rindex(")")], stat[stat.rindex(")") + 2:].split()
            return int(rest[1]), name
        out = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout
        parent, _, name = out.strip().partition(" ")
        return int(parent or 0), name.strip()
    except Exception:
        return 0, ""


def claude_pid(start=None):
    """The Claude Code process that runs this hook or server: the first ancestor that is not an
    interpreter, launcher or shell. Hooks and the MCP server resolve to the same process."""
    pid = start or os.getppid()
    for _ in range(8):
        if not pid or pid == 1:
            return 0
        parent, name = proc(pid)
        base = os.path.basename(name).lower()
        base = base[:-4] if base.endswith(".exe") else base
        if not any(base == s or (s == "python" and base.startswith("python")) for s in SKIP):
            return pid
        pid = parent
    return 0
