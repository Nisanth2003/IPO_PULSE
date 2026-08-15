<#
.SYNOPSIS
    Register the IPO Pulse schedule with Windows Task Scheduler.

.DESCRIPTION
    The Windows counterpart of deploy/systemd. systemd does not exist on
    Windows, and this repo's primary working machine is Windows 11, so this
    is the file that actually runs the schedule day to day. It registers the
    same jobs, at the same times, calling the same `ipopulse job <name>`
    entry point, so both platforms stay in step.

    Times are local. The systemd units pin Asia/Kolkata explicitly; Task
    Scheduler has no equivalent, so set the machine's timezone if these hours
    are meant to track the Indian market.

.PARAMETER Remove
    Unregister every IPO Pulse task instead of creating them.

.PARAMETER RunAsSystem
    Run whether or not you are logged in. Requires an elevated shell. Without
    it the tasks only fire while your user is signed in.

.EXAMPLE
    .\Register-IpoPulseTasks.ps1
    .\Register-IpoPulseTasks.ps1 -Remove
#>
[CmdletBinding()]
param(
    [switch]$Remove,
    [switch]$RunAsSystem
)

$ErrorActionPreference = 'Stop'
$FolderName = '\IPO Pulse'

$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Backend = Join-Path $Repo 'backend'

# Same jobs and times as deploy/systemd/*.timer. Keep the two in step.
#
# Indian IPO bidding runs 10:00-17:00 IST. Subscription is a running total
# that only moves inside that window, so a single evening pull would miss the
# whole intraday story - hence three triggers on the daily chain rather than
# one. Chained jobs (daily = sync,enrich,doctor,build) run as ONE task so a
# later step cannot start before the earlier one has exited 0; two tasks a
# fixed 15 minutes apart is a race on a slow NSE day.
$Jobs = @(
    @{ Name = 'daily'
       Why  = 'sync,enrich,doctor,build. 10:00 as bidding opens, 18:35 once it has closed.'
       Triggers = @(
           @{ Kind = 'Daily'; At = '10:00' },
           @{ Kind = 'Daily'; At = '18:35' }
       ) },
    @{ Name = 'grey'
       # `Run` overrides the command when it should differ from the task name.
       # The 21:00 slot does GMP and THEN the full chain, so `build` runs last
       # and verifies everything the night wrote, GMP included. Renaming the
       # task to match would orphan the one already registered on this machine.
       Run  = 'grey daily'
       Why  = 'GMP once the grey market settles, then the full chain. 21:00.'
       Triggers = @( @{ Kind = 'Daily'; At = '21:00' } ) },
    @{ Name = 'translate'
       Why  = 'Cached 30 days; only changes when the prose does.'
       Triggers = @( @{ Kind = 'Weekly'; At = '03:00'; Day = 'Sunday' } ) },
    @{ Name = 'report'
       Why  = 'After translate, so the workbook has the week final copy.'
       Triggers = @( @{ Kind = 'Weekly'; At = '04:00'; Day = 'Sunday' } ) }
)

function Get-TaskName($job) { "IPO Pulse - $($job.Name)" }
function Get-JobArgs($job) { if ($job.Run) { $job.Run } else { $job.Name } }

# Sweep the whole folder rather than only the names in $Jobs. Renaming or
# regrouping a job would otherwise orphan its old task, which keeps firing
# `ipopulse job <gone>` forever with nobody watching.
function Remove-AllIpoPulseTasks {
    $tasks = @(Get-ScheduledTask -TaskPath "$FolderName\" -ErrorAction SilentlyContinue)
    foreach ($t in $tasks) {
        Unregister-ScheduledTask -TaskName $t.TaskName -TaskPath $t.TaskPath -Confirm:$false
        Write-Host "removed  $($t.TaskName)"
    }
    return $tasks.Count
}

if ($Remove) {
    $n = Remove-AllIpoPulseTasks
    Write-Host "`n$n IPO Pulse task(s) unregistered."
    return
}

# Resolve the interpreter the same way the CLI is normally run. Prefer a repo
# venv so a scheduled run cannot pick a different set of packages than you do.
$Python = $null
foreach ($candidate in @(
    (Join-Path $Repo '.venv\Scripts\python.exe'),
    (Join-Path $Repo 'backend\.venv\Scripts\python.exe')
)) { if (Test-Path $candidate) { $Python = $candidate; break } }
if (-not $Python) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $cmd) { throw "No python found on PATH and no .venv in $Repo." }
    $Python = $cmd.Source
}

# pythonw.exe runs without opening a console window each time a task fires.
$Pythonw = $Python -replace 'python\.exe$', 'pythonw.exe'
if (Test-Path $Pythonw) { $Runner = $Pythonw } else { $Runner = $Python }

Write-Host "repo   : $Repo"
Write-Host "python : $Runner"

# Fail before registering six tasks that would all fail at 18:30.
& $Python -c "import sys; sys.path.insert(0,r'$Backend'); import ipopulse" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "$Python cannot import ipopulse. Run: $Python -m pip install -r $Backend\requirements.txt"
}
if (-not (Test-Path (Join-Path $Repo '.env'))) {
    # NOTE: this file is deliberately pure ASCII. Windows PowerShell 5.1 reads
    # a BOM-less .ps1 as ANSI, so a stray UTF-8 dash here is a parse error.
    Write-Warning "No .env in $Repo - jobs needing a key or the sheet will fail."
}

if ($RunAsSystem) {
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
} else {
    # Interactive: the task runs as you, only while you are logged in.
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive
}

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 20) `
    -MultipleInstances IgnoreNew `
    -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 5)

# -StartWhenAvailable is the Persistent=true equivalent: a machine that was
# asleep at 18:30 still runs the job when it wakes, rather than losing a day.

# Clear the folder first so a previous layout cannot leave stragglers behind.
Remove-AllIpoPulseTasks | Out-Null

foreach ($job in $Jobs) {
    $name = Get-TaskName $job

    $action = New-ScheduledTaskAction -Execute $Runner `
        -Argument "-m ipopulse.cli job $(Get-JobArgs $job)" -WorkingDirectory $Backend

    # A task may carry several triggers; that is how the daily chain gets its
    # three runs without registering three separate tasks that could overlap.
    $triggers = @()
    $labels = @()
    foreach ($t in $job.Triggers) {
        switch ($t.Kind) {
            'Weekly'   {
                $triggers += New-ScheduledTaskTrigger -Weekly -DaysOfWeek $t.Day -At $t.At
                $labels += "$($t.Day) $($t.At)"
            }
            'Weekdays' {
                $triggers += New-ScheduledTaskTrigger -Weekly `
                    -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $t.At
                $labels += "Mon-Fri $($t.At)"
            }
            default    {
                $triggers += New-ScheduledTaskTrigger -Daily -At $t.At
                $labels += "daily $($t.At)"
            }
        }
    }

    $existing = Get-ScheduledTask -TaskName $name -TaskPath "$FolderName\" -ErrorAction SilentlyContinue
    if ($existing) { Unregister-ScheduledTask -TaskName $name -TaskPath "$FolderName\" -Confirm:$false }

    Register-ScheduledTask -TaskName $name -TaskPath "$FolderName\" `
        -Action $action -Trigger $triggers -Principal $principal -Settings $settings `
        -Description "$($job.Why)  [ipopulse job $(Get-JobArgs $job)]" | Out-Null

    Write-Host ("registered  {0,-26} {1}" -f $name, ($labels -join ' | '))
}

Write-Host "`nInspect :  Get-ScheduledTask -TaskPath '$FolderName\'"
Write-Host "Run now :  Start-ScheduledTask -TaskName 'IPO Pulse - sync' -TaskPath '$FolderName\'"
Write-Host "History :  Get-WinEvent -LogName Microsoft-Windows-TaskScheduler/Operational -MaxEvents 20"
Write-Host "Remove  :  .\Register-IpoPulseTasks.ps1 -Remove"
