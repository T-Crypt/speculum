# Phase 4a prompt: Windows host backend (`collector/hostinfo.py`)

Paste everything below the line into a Strata session (or `claude-local`) started in the repo root.

---

You are working in the Speculum repository (current directory): a local LLM runtime dashboard whose collector is
one stdlib-only Python process, `collector/speculum.py`, serving static panels plus `/api/*` on 127.0.0.1:8792.
Read `AGENTS.md` and `docs/DESIGN.md` section "4. Windows" first. Keep your reading focused: grep for a function
before reading a large file whole (`collector/speculum.py` is about 1,600 lines).

## Goal

Make the collector's host numbers work on Windows as well as Linux, with no third-party packages, by moving every
`/proc` read into a new module `collector/hostinfo.py` with one interface and two backends.

## What reads /proc today (all in collector/speculum.py; find them with `grep -n "/proc" collector/speculum.py`)

- CPU: `/proc/stat` (total and per core, as busy fractions between two samples)
- Memory: `/proc/meminfo` (RAM used/total, swap used/total)
- Processes: `/proc/<pid>/cmdline` and `/proc/<pid>/status` (VmRSS of llama-server / ninfer-serve / strata, the
  "Top process" readout)
- Load average `/proc/loadavg`, uptime `/proc/uptime`
- `ninfer_serve_port()`: scans `/proc/<pid>/cmdline` for an `ninfer-serve` process and passes its argv to
  `ninfer_port_of(args)` (keep `ninfer_port_of` as it is; it is unit-tested)

## Design

`collector/hostinfo.py` exposes plain functions that return plain dicts/lists, and picks the backend once at import
(`sys.platform == "win32"`):

- `cpu_times()` -> `{"total": (busy, all), "cores": [(busy, all), ...]}` (cumulative counters; the caller diffs)
- `memory()` -> `{"ram_used", "ram_total", "swap_used", "swap_total"}` in bytes
- `processes()` -> `[{"pid", "name", "argv": [...], "rss": bytes}]` (argv may be `[]` when it cannot be read)
- `load()` -> `[1m, 5m, 15m]` on Linux, `None` on Windows (there is no load average; the UI already shows n/a)
- `uptime_s()` -> float

Linux backend: exactly today's `/proc` logic, moved, not rewritten. Windows backend, `ctypes` only:
`GlobalMemoryStatusEx` (RAM and page file), `GetSystemTimes` (total CPU) and `NtQuerySystemInformation`
(`SystemProcessorPerformanceInformation`) for per core, `EnumProcesses` + `OpenProcess` +
`GetProcessMemoryInfo` (psapi) + `QueryFullProcessImageNameW` for processes, `GetTickCount64` for uptime. Process
argv on Windows: leave `argv` empty unless you can read it cheaply and safely; then `ninfer_serve_port()` falls back
to its default port 8080 for an `ninfer-serve.exe` it finds by name.

Then change `collector/speculum.py` to call `hostinfo` instead of opening `/proc` directly, keeping every field it
publishes today (snapshot `host` and the 1 Hz tick must not change shape).

## Rules

- Stdlib only. No psutil, no pywin32.
- Do not change the collector's HTTP API, the UI files, or any behaviour on Linux.
- Keep the code style of `collector/` (comment density, naming, no type-annotation sprawl).
- Do not start, stop or restart any service, do not run anything on the GPU, do not commit.

## Verify (all on Linux)

1. `cd collector && python3 -m pytest -q`: every existing test passes (50 today).
2. New `collector/test_hostinfo.py`:
   - the Linux backend against the real `/proc` (`memory()["ram_total"] > 0`, `cpu_times()` has one core entry
     per `os.cpu_count()`, `processes()` contains this test process with `rss > 0`)
   - the Windows backend imports on Linux without calling any Win32 function (import-safe), and its pure
     helpers (struct sizes, unit conversions) are tested with fixed numbers.
3. Before/after on the running box: `curl -s http://127.0.0.1:8792/api/snapshot | python3 -c 'import json,sys;
   print(json.load(sys.stdin)["host"])'` from a collector started by hand on another port
   (`python3 collector/speculum.py --port 8799`, stop it after) shows the same keys and plausible values as the
   service on 8792.

Report: files and line ranges changed, which Win32 calls you used for what, and plainly what you could not test
(the Windows backend can only be exercised on a Windows machine: galaxy).
