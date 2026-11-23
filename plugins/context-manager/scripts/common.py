"""Shared paths and verified process lookup (Linux/macOS/Windows, stdlib only).

A PID is not a session identity: registry users must also compare process_identity().
Unknown launchers fail closed; RECALL_TRANSCRIPT is the explicit escape hatch.
"""
import io, os, subprocess, sys

DATA = os.environ.get("CLAUDE_PLUGIN_DATA") or os.path.join(os.path.expanduser("~"), ".claude", "context-manager")
SUMMARIES = os.path.join(DATA, "summaries")
SESSIONS = os.path.join(DATA, "sessions")
LOG = os.path.join(DATA, "log.jsonl")
NESTED = "CCM_NESTED"
SKIP = ("python", "py", "pythonw", "sh", "bash", "dash", "zsh", "cmd", "powershell", "pwsh", "env")


def utf8_stdio():
    """Use UTF-8 and LF for the MCP/hook streams, including Windows consoles."""
    for name in ("stdin", "stdout"):
        stream = getattr(sys, name)
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", newline="\n" if name == "stdout" else None)


def _kernel32():
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # HANDLE must never pass through ctypes' default 32-bit int conversion.
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    return k32


def _windows_processes():
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    k32 = _kernel32()
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for method in (k32.Process32FirstW, k32.Process32NextW):
        method.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
        method.restype = wintypes.BOOL
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)
    if snap == ctypes.c_void_p(-1).value or snap is None:
        return {}
    try:
        procs, e = {}, Entry()
        e.dwSize = ctypes.sizeof(Entry)
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            procs[e.th32ProcessID] = (e.th32ParentProcessID, e.szExeFile)
            ok = k32.Process32NextW(snap, ctypes.byref(e))
        return procs
    finally:
        k32.CloseHandle(snap)


def _linux_stat(pid):
    with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as fh:
        stat = fh.read()
    end = stat.rindex(")")
    return stat[stat.index("(") + 1:end], stat[end + 2:].split()


def proc(pid):
    """(parent pid, executable name) of a process, or (0, "") on lookup failure."""
    try:
        if sys.platform == "win32":
            return _windows_processes().get(pid, (0, ""))
        if sys.platform.startswith("linux"):
            name, rest = _linux_stat(pid)
            return int(rest[1]), name
        out = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5, check=True).stdout
        fields = out.strip().split(None, 1)
        return int(fields[0]), fields[1]
    except Exception:
        return 0, ""


def process_identity(pid):
    """Stable start-generation token, or None when verification is unavailable.

    Linux: boot UUID + start ticks. macOS: ps lstart (second resolution, C locale).
    Windows: 64-bit creation FILETIME, with explicitly typed HANDLE APIs.
    """
    if type(pid) is not int or pid <= 0:
        return None
    try:
        if sys.platform.startswith("linux"):
            _, rest = _linux_stat(pid)
            with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as fh:
                boot = fh.read().strip()
            start = int(rest[19])  # field 22; rest starts at field 3
            return f"linux:{boot}:{start}" if boot else None
        if sys.platform == "darwin":
            env = dict(os.environ, LC_ALL="C", LANG="C")
            out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=5, check=True, env=env).stdout
            start = " ".join(out.split())
            import re
            if not re.fullmatch(r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{1,2} \d{2}:\d{2}:\d{2} \d{4}", start):
                return None
            return "darwin:" + start
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes
            k32 = _kernel32()
            k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
            k32.GetProcessTimes.restype = wintypes.BOOL
            handle = k32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
            if not handle:
                return None
            try:
                created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
                if not k32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                           ctypes.byref(kernel), ctypes.byref(user)):
                    return None
                stamp = (created.dwHighDateTime << 32) | created.dwLowDateTime
                return f"win32:{stamp}" if stamp else None
            finally:
                k32.CloseHandle(handle)
    except Exception:
        return None
    return None


def _node_is_claude(pid):
    """Recognize npm Claude Code by its CLI path, never by 'node' alone.

    Native claude.exe is supported on Windows. An uninspectable Node launcher
    requires the explicit transcript override, not a guessed ancestor.
    """
    try:
        if sys.platform.startswith("linux"):
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                args = fh.read().decode("utf-8", "replace").split("\0")
        elif sys.platform == "darwin":
            out = subprocess.run(["ps", "-o", "args=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=5, check=True).stdout
            args = out.split()
        else:
            return False
        suffix = "/@anthropic-ai/claude-code/cli.js"
        for arg in args[1:]:
            if arg.replace("\\", "/").endswith(suffix):
                return True
            # npm's shebang launcher can leave an absolute symlink such as
            # /opt/homebrew/bin/claude in argv. Resolve it, but never accept
            # a generic node process or the launcher basename on its own.
            if os.path.isabs(arg) and os.path.realpath(arg).replace("\\", "/").endswith(suffix):
                return True
        return False
    except Exception:
        return False


def claude_pid(start=None):
    """Find an identified Claude ancestor; unknown/missing processes return 0.

    An arbitrary non-shell ancestor (IDE, node, service) may host many sessions.
    Only known wrappers are skipped; hooks and the MCP use the same rule.
    """
    pid = os.getppid() if start is None else start
    seen = set()
    for _ in range(32):
        if type(pid) is not int or pid <= 1 or pid in seen:
            return 0
        seen.add(pid)
        parent, name = proc(pid)
        base = name.replace("\\", "/").rsplit("/", 1)[-1].lower()
        base = base[:-4] if base.endswith(".exe") else base
        if base == "claude" or (base in ("node", "nodejs") and _node_is_claude(pid)):
            return pid
        if not base or not any(base == s or (s == "python" and base.startswith("python")) for s in SKIP):
            return 0
        pid = parent
    return 0


def readable_transcript(path):
    """Validate an explicit/registered path without discovering any other file."""
    if not isinstance(path, str) or not path.strip() or not os.path.isabs(path):
        raise ValueError("transcript_path must be an absolute path")
    if not os.path.isfile(path):
        raise ValueError("transcript_path is not a readable regular file")
    try:
        with open(path, "rb") as fh:
            fh.read(1)
    except OSError as exc:
        raise ValueError("transcript_path is not readable") from exc
    return path
