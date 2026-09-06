<#
  Start IPO Pulse — everything, from one command.

      .\run.ps1                  studio + API on http://localhost:8000
      .\run.ps1 -Open            ...and open a browser at it
      .\run.ps1 -Tasks           also register the scheduled jobs, then serve
      .\run.ps1 -Port 8080       somewhere else
      .\run.ps1 -Lan             bind 0.0.0.0 — read the warning below first
      .\run.ps1 -Check           run the preflight and exit, starting nothing

  THERE IS ONLY ONE SERVER. The studio is static files and the API is routes
  on the same port; `serve` hands out both. So "start the frontend" and "start
  the backend" are one action, and a second command for the other half would
  be theatre. What this script adds over `ipopulse serve` is the preflight —
  the four things that, when wrong, produce a symptom that looks like
  something else entirely.
#>

[CmdletBinding()]
param(
  [int]$Port = 8000,
  [switch]$Open,
  [switch]$Tasks,
  [switch]$Lan,
  [switch]$Check,
  # Kill whatever already holds the port instead of refusing to start.
  [switch]$Force
)

$ErrorActionPreference = 'Stop'
$repo = $PSScriptRoot
$ok = $true

function Say([string]$state, [string]$what, [string]$detail = '') {
  $colour = @{ 'ok' = 'Green'; '!!' = 'Red'; '??' = 'Yellow' }[$state]
  Write-Host ("  {0,-3}" -f $state) -ForegroundColor $colour -NoNewline
  Write-Host ("{0,-22}" -f $what) -NoNewline
  Write-Host $detail -ForegroundColor DarkGray
}

Write-Host "`nIPO Pulse — preflight`n" -ForegroundColor Cyan

# ── 1. Python, and the package actually installed into it ─────────────────
# `pip` and `python` on this machine can point at different interpreters, and
# installing into the wrong one gives you an `ipopulse` command that dies on
# import. So resolve the interpreter first and install with -m pip against it.
# Windows PowerShell 5.1 has no null-conditional operator, and this script
# has to run on the shell that ships with Windows rather than one you install.
$pyCmd = Get-Command python -ErrorAction SilentlyContinue
$py = if ($pyCmd) { $pyCmd.Source } else { $null }
if (-not $py) { Say '!!' 'python' 'not on PATH'; $ok = $false }
else {
  $ver = & $py -c "import sys;print('.'.join(map(str,sys.version_info[:3])))"
  Say 'ok' 'python' "$ver  $py"

  # cli.py uses PEP 701 f-strings in places; 3.12 is what the Dockerfile pins
  # and what this is developed against.
  $major, $minor = $ver.Split('.')[0, 1]
  if ([int]$major -eq 3 -and [int]$minor -lt 12) {
    Say '??' 'python version' "3.12+ recommended; you have $ver"
  }

  $have = & $py -c "import importlib.util as u;print(bool(u.find_spec('ipopulse')))"
  if ($have -ne 'True') {
    Say '??' 'ipopulse package' 'not installed — installing now (editable)'
    & $py -m pip install -e "$repo\backend" --quiet
    if ($LASTEXITCODE -ne 0) { Say '!!' 'pip install' 'failed'; $ok = $false }
    else { Say 'ok' 'ipopulse package' 'installed' }
  } else {
    Say 'ok' 'ipopulse package' 'importable'
  }
}

# ── 2. .env, and the one variable whose absence is a security hole ────────
$envFile = Join-Path $repo '.env'
if (-not (Test-Path $envFile)) {
  Say '!!' '.env' 'missing — copy .env.example and fill it in'
  $ok = $false
} else {
  $envText = Get-Content $envFile -Raw
  Say 'ok' '.env' 'present'

  function Has([string]$key) {
    return [bool]([regex]::Match($envText, "(?m)^\s*$key\s*=\s*(\S.*)$").Success)
  }

  if (Has 'GOOGLE_SHEETS_ID') { Say 'ok' 'sheet id' 'set' }
  else { Say '!!' 'sheet id' 'GOOGLE_SHEETS_ID empty — the store IS the sheet'; $ok = $false }

  if (Has 'GOOGLE_SHEETS_KEY') { Say 'ok' 'sheet key' 'set' }
  else { Say '??' 'sheet key' 'GOOGLE_SHEETS_KEY empty — reads/writes will fail' }

  # The trigger panel runs pipeline jobs. On localhost an unset password is
  # merely inconvenient; bound to 0.0.0.0 it is a job runner open to the LAN,
  # so -Lan without it is refused rather than warned about.
  if (Has 'IPOPULSE_TRIGGER_PASSWORD') {
    Say 'ok' 'trigger password' 'set — the panel will ask for it'
  } elseif ($Lan) {
    Say '!!' 'trigger password' 'unset, and -Lan would expose the job runner'
    $ok = $false
  } else {
    Say '??' 'trigger password' 'unset — trigger panel disabled (fine locally)'
  }
}

# ── 3. The port ───────────────────────────────────────────────────────────
# Two servers on 8000 is a specific, confusing failure: the studio loads and
# every /api call 404s, because a plain static server got there first.
$held = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($held) {
  $pids = $held.OwningProcess | Select-Object -Unique
  $names = ($pids | ForEach-Object {
    (Get-Process -Id $_ -ErrorAction SilentlyContinue).ProcessName }) -join ', '
  if ($Force) {
    $pids | ForEach-Object { Stop-Process -Id $_ -Force }
    Say 'ok' "port $Port" "was held by $names — killed (-Force)"
  } else {
    Say '!!' "port $Port" "already listening ($names). Re-run with -Force"
    $ok = $false
  }
} else {
  Say 'ok' "port $Port" 'free'
}

# ── 4. ffmpeg, needed only for rendering ──────────────────────────────────
if (Get-Command ffmpeg -ErrorAction SilentlyContinue) { Say 'ok' 'ffmpeg' 'on PATH' }
else { Say '??' 'ffmpeg' 'missing — serving is fine, rendering is not' }

if (-not $ok) {
  Write-Host "`nNot starting. Fix the red lines above.`n" -ForegroundColor Red
  exit 1
}

# ── The scheduled jobs, on request ────────────────────────────────────────
# Deliberately opt-in. The GitHub Actions workflow in .github/workflows/
# already runs this same chain on a schedule, and two schedulers writing the
# same sheet is not redundancy — a save clears a tab before rewriting it and
# there is no lock, so an overlap loses whichever run finished first.
if ($Tasks) {
  Write-Host "`nRegistering scheduled jobs…" -ForegroundColor Cyan
  Write-Host "  Note: schedule.yml already runs these on GitHub Actions." -ForegroundColor DarkYellow
  Write-Host "  Two schedulers on one sheet overwrite each other. Pick one.`n" -ForegroundColor DarkYellow
  & (Join-Path $repo 'deploy\windows\Register-IpoPulseTasks.ps1')
}

if ($Check) { Write-Host "`nPreflight only — nothing started.`n"; exit 0 }

# ── Serve ─────────────────────────────────────────────────────────────────
$bind = if ($Lan) { '0.0.0.0' } else { '127.0.0.1' }
$url = "http://localhost:$Port"

Write-Host "`nStudio + API -> $url" -ForegroundColor Cyan
if ($Lan) {
  Write-Host "Bound to 0.0.0.0 — reachable from your network." -ForegroundColor Yellow
}
Write-Host "Ctrl+C to stop.`n" -ForegroundColor DarkGray

if ($Open) { Start-Process $url }

Push-Location (Join-Path $repo 'backend')
try   { & $py -m ipopulse.cli serve --host $bind --port $Port }
finally { Pop-Location }
