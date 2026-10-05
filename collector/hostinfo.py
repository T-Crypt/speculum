#!/usr/bin/env python3
"""Host metrics for Speculum — one module, two backends.

The collector's host section (CPU total / per-core, RAM + swap, load,
uptime, engine process RSS) read `/proc` directly in speculum.py.  This
module is now the ONLY place that does: speculum.py calls the functions
below and diffs the `cpu_times()` counters between 1 s samples, exactly as
before.

Linux backend: the former `/proc` logic, moved not rewritten.

Windows backend: `ctypes` only (no psutil, no pywin32):
    GlobalMemoryStatusEx                        RAM + page file
    GetSystemTimes                              total CPU (kernel includes idle)
    NtQuerySystemInformation (per-processor)    per-core CPU
    EnumProcesses + OpenProcess +
    QueryFullProcessImageNameW + GetProcessMemoryInfo (psapi)   processes
    GetTickCount64                              uptime
  `load()` is None: Windows has no load average; the UI renders absent
  keys as n/a.

`cpu_times()` returns (total, cores); every entry is (all, idle) in the
platform's native units — only diffs matter, so the 100 ns Windows clock
and the /proc jiffy clock are interchangeable.  `memory()` and
`processes()` return the same dict shapes the snapshot has always carried
(`*_gb` keys; `{"name", "pid", "rss_gb", "cmd"}` records).  On Windows
`cmd` is the image path (argv is not read), so a hand-started
ninfer-serve.exe resolves to its default port via ninfer_port_of().
"""

import os
import sys

IS_WINDOWS = sys.platform == "win32"


# --------------------------------------------------------------------------
# Linux backend (the former /proc logic from speculum.py)
# --------------------------------------------------------------------------

def _linux_cpu_times():
    cores, total = [], None
    try:
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("cpu"):
                    parts = line.split()
                    if not parts[1:]:
                        continue
                    idle = int(parts[4]) + (int(parts[5]) if len(parts) > 5 else 0)
                    total_v = sum(int(x) for x in parts[1:])
                    vals = (total_v, idle)
                    if line.startswith("cpu "):
                        total = vals
                    else:
                        cores.append(vals)
    except OSError:
        pass
    return total, cores


def _linux_memory():
    info = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                info[k] = int(v.strip().split()[0])
    except OSError:
        pass
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", 0)
    return {
        "ram_total_gb": total / 1e6,
        "ram_used_gb": max(0.0, (total - avail)) / 1e6 if total else 0.0,
        "swap_total_gb": info.get("SwapTotal", 0) / 1e6,
        "swap_used_gb": (info.get("SwapTotal", 0) - info.get("SwapFree", 0)) / 1e6,
    }


def _linux_processes():
    found = []
    try:
        pids = [d for d in os.listdir("/proc") if d.isdigit()]
    except OSError:
        return found
    for pid in pids:
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as f:
                cmd = f.read(512).replace(b"\0", b" ").decode("utf-8", "replace").strip()
        except OSError:
            continue
        name = None
        if "llama-server" in cmd:
            name = "llama-server"
        elif "ninfer-serve" in cmd:
            name = "ninfer-serve"
        elif "server.py" in cmd and "strata" in cmd:
            name = "strata"
        if name is None:
            continue
        rss_kb = None
        try:
            with open("/proc/%s/status" % pid) as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        rss_kb = int(line.split()[1])
                        break
        except OSError:
            pass
        found.append({"name": name, "pid": int(pid),
                      "rss_gb": round(rss_kb / 1e6, 2) if rss_kb else None,
                      "cmd": cmd[:120]})
    return found


def _linux_load():
    try:
        return [float(x) for x in open("/proc/loadavg").read().split()[:3]]
    except OSError:
        return None


def _linux_uptime_s():
    try:
        return float(open("/proc/uptime").read().split()[0])
    except OSError:
        return None


# --------------------------------------------------------------------------
# Windows backend (ctypes only)
# --------------------------------------------------------------------------

if IS_WINDOWS:
    import ctypes
    import struct
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _psapi = ctypes.WinDLL("psapi", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll", use_last_error=True)

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _SYS_PROCESSOR_PERF_INFO = 8          # SystemProcessorPerformanceInformation

    # ProcessorNameString[64], IdleTime, KernelTime, UserTime (100 ns),
    # ProcessingModeMask, NumberOfProcessors — one block per logical core.
    _PROC_PERF_FMT = "64sqqqII"
    _PROC_PERF_STRIDE = struct.calcsize(_PROC_PERF_FMT)

    class _MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", wintypes.DWORD),
                    ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_uint64),
                    ("ullAvailPhys", ctypes.c_uint64),
                    ("ullTotalPageFile", ctypes.c_uint64),
                    ("ullAvailPageFile", ctypes.c_uint64),
                    ("ullTotalVirtual", ctypes.c_uint64),
                    ("ullAvailVirtual", ctypes.c_uint64),
                    ("ullAvailExtendedVirtual", ctypes.c_uint64)]

    class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD),
                    ("CreationTime", wintypes.FILETIME),
                    ("ExitTime", wintypes.FILETIME),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t)]

    def _win_cpu_times():
        idle = wintypes.LARGE_INTEGER()
        kernel = wintypes.LARGE_INTEGER()
        user = wintypes.LARGE_INTEGER()
        total = None
        if _kernel32.GetSystemTimes(ctypes.byref(idle),
                                    ctypes.byref(kernel),
                                    ctypes.byref(user)):
            total = (user.quad + kernel.quad, idle.quad)
        return total, _win_cpu_cores()

    def _win_cpu_cores():
        """Per-core (all, idle) in 100 ns units; kernel time includes idle,
        so the same all-idle diff as /proc gives busy."""
        needed = wintypes.DWORD(0)
        if _ntdll.NtQuerySystemInformation(_SYS_PROCESSOR_PERF_INFO,
                                           None, 0, ctypes.byref(needed)) != 0:
            return []
        buf = ctypes.create_string_buffer(needed.value)
        if _ntdll.NtQuerySystemInformation(_SYS_PROCESSOR_PERF_INFO,
                                           buf, needed, ctypes.byref(needed)) != 0:
            return []
        out = []
        for block in range(needed.value // _PROC_PERF_STRIDE):
            _name, idle, kernel, user, _mask, _n = struct.unpack_from(
                _PROC_PERF_FMT, buf.raw, block * _PROC_PERF_STRIDE)
            out.append((user + kernel, idle))
        return out

    def _win_memory():
        st = _MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if not _kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return {"ram_total_gb": 0.0, "ram_used_gb": 0.0,
                    "swap_total_gb": 0.0, "swap_used_gb": 0.0}
        phys, avail = st.ullTotalPhys, st.ullAvailPhys
        pf = st.ullTotalPageFile
        return {
            "ram_total_gb": phys / 1e9,
            "ram_used_gb": max(0.0, (phys - avail)) / 1e9,
            "swap_total_gb": pf / 1e9,
            "swap_used_gb": (pf - st.ullAvailPageFile) / 1e9,
        }

    def _win_process_name(image_path):
        """Engine label from the image basename; None when not an engine.
        Windows argv is not read, so a python.exe-launched `server.py`
        (strata) is visible only when the launcher's path says strata."""
        base = os.path.basename(image_path).lower()
        if "llama-server" in base:
            return "llama-server"
        if "ninfer-serve" in base:
            return "ninfer-serve"
        if "strata" in base:
            return "strata"
        return None

    def _win_processes():
        found = []
        pids_buf = ctypes.create_string_buffer(65536)   # 16384 PIDs
        needed = wintypes.DWORD(0)
        if not _kernel32.EnumProcesses(pids_buf, ctypes.sizeof(pids_buf),
                                       ctypes.byref(needed)):
            return found
        n = needed.value // 4
        pids = struct.unpack_from("%dI" % n, pids_buf.raw)
        for pid in pids:
            if not pid:
                continue
            h = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION,
                                      False, pid)
            if not h:
                continue
            try:
                name_buf = ctypes.create_string_buffer(1024)
                size = wintypes.DWORD(ctypes.sizeof(name_buf))
                if not _kernel32.QueryFullProcessImageNameW(
                        h, 0, name_buf, ctypes.byref(size)):
                    continue
                name = _win_process_name(name_buf.value)
                if name is None:
                    continue
                pmi = _PROCESS_MEMORY_COUNTERS()
                pmi.cb = ctypes.sizeof(_PROCESS_MEMORY_COUNTERS)
                rss = None
                if _psapi.GetProcessMemoryInfo(h, ctypes.byref(pmi),
                                               ctypes.sizeof(pmi)):
                    rss = round(pmi.WorkingSetSize / 1e9, 2)
                found.append({"name": name, "pid": pid, "rss_gb": rss,
                              "cmd": name_buf.value[:120]})
            finally:
                _kernel32.CloseHandle(h)
        return found

    def _win_load():
        return None

    def _win_uptime_s():
        return _kernel32.GetTickCount64() / 1000.0

    cpu_times = _win_cpu_times
    memory = _win_memory
    processes = _win_processes
    load = _win_load
    uptime_s = _win_uptime_s
else:
    cpu_times = _linux_cpu_times
    memory = _linux_memory
    processes = _linux_processes
    load = _linux_load
    uptime_s = _linux_uptime_s
