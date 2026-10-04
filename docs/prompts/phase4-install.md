# Phase 4b prompt: `install.sh` and `install.ps1`

Paste everything below the line into a Strata session (or `claude-local`) started in the repo root. Do Phase 4a
(Windows host backend) first: the Windows installer is only useful once the collector runs there.

---

You are working in the Speculum repository (current directory): a local LLM runtime dashboard whose collector is
one stdlib-only Python process, `collector/speculum.py`, serving the dashboard on 127.0.0.1:8792. Read `AGENTS.md`,
`docs/DESIGN.md` section "5. Install", `collector/speculum.service` (the existing systemd user unit) and
`speculum.example.toml`. Keep your reading focused.

## Goal

Two installers short enough to read before running, for a user who "just wants stats on 127.0.0.1": one command,
no config needed (with no `speculum.toml` the collector probes localhost and shows what it finds).

## `install.sh` (Linux)

- Checks: `python3` >= 3.9 on PATH (print the version found and stop with a clear message if missing or older);
  `nvidia-smi` optional (warn that the GPU panel will say "no GPU" without it).
- Installs the systemd **user** unit from `collector/speculum.service` into `~/.config/systemd/user/`, rewriting
  `ExecStart` to this checkout's absolute path (do not assume `~/test/glass-monitor`), then
  `systemctl --user daemon-reload` and `systemctl --user enable --now speculum`.
- Waits up to 15 s for `http://127.0.0.1:8792/api/storage` to answer and prints the URL.
- Flags: `--uninstall` (disable, stop, remove the unit; never delete the history database or the config, say where
  they are), `--port N` (writes `[server] port` into `~/.config/speculum/speculum.toml` only if that file does not
  exist yet; never overwrite a user's config), `--dry-run` (print every action, change nothing).
- Idempotent: running it twice changes nothing the second time and says so.
- Never binds anything to 0.0.0.0, never opens a firewall port, never needs root. Print one line on remote access:
  use `tailscale serve` (or an SSH tunnel), not a LAN bind.

## `install.ps1` (Windows, PowerShell 5.1 and 7 both)

- Checks: `py -3` or `python` >= 3.9 (print what it found), `nvidia-smi.exe` optional (same warning).
- Registers a Task Scheduler task `Speculum` that starts the collector at logon for the current user, hidden
  window (`pythonw.exe` if present, else `python.exe` with `-WindowStyle Hidden`), working directory = this
  checkout, restart on failure (3 tries, 1 minute apart). No admin rights: per-user task.
- Starts it now, waits up to 15 s for `http://127.0.0.1:8792/api/storage`, prints the URL.
- Same flags as the Linux script: `-Uninstall`, `-Port N` (writes `%APPDATA%\Speculum\speculum.toml` only if
  absent), `-WhatIf` (PowerShell's own dry run: use `SupportsShouldProcess`).
- Idempotent; `Set-StrictMode -Version Latest`; `$ErrorActionPreference = 'Stop'` with try/catch around each step
  so a failure says which step and why. Approved verb-noun names for any functions (e.g. `Install-SpeculumTask`).
  2-space indentation. No emojis.

## Also

- `README.md`: a short "Install" section (both commands, `--uninstall`, where the history database lives on each
  OS, how to reach it remotely with `tailscale serve`). Do not rewrite the rest of the README (Phase 5 does).
- No secrets anywhere: an engine API key stays in an environment variable named by `api_key_env`.

## Rules

- Do not change the collector or the UI. Do not commit.
- Do not run `install.sh` for real on this machine: a Speculum service is already installed and running here. Use
  `--dry-run`, and test real runs in a throwaway way: `HOME=$(mktemp -d) XDG_CONFIG_HOME=... ./install.sh --dry-run`.
- `install.ps1` cannot run here; check it with `pwsh` only if `pwsh` exists (`command -v pwsh`), otherwise say so.

## Verify

1. `bash -n install.sh` and `shellcheck install.sh` (if shellcheck is installed; say if it is not).
2. `./install.sh --dry-run` prints the exact unit it would write, with this checkout's path in `ExecStart`.
3. `./install.sh --dry-run --uninstall` lists what it would remove and states that the database and config stay.
4. If `pwsh` exists: `pwsh -NoProfile -Command "& { . ./install.ps1 -WhatIf }"` parses and prints its plan.

Report: the files you wrote, the exact unit and task definitions they produce, and plainly what you could not test
(install.ps1 needs a Windows machine: galaxy).
