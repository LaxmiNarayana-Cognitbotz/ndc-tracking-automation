# =============================================================================
#  NDC & F&F Pipeline - Task Scheduler Setup
#  Run this ONCE on any machine to register the daily scheduled tasks.
#
#  HOW TO RUN (on the client machine):
#  ─────────────────────────────────────────────────────────────────────────────
#  1. Open PowerShell or CMD (no Administrator needed)
#  2. cd into the project folder, e.g.:
#       cd "D:\Projects\NDC-Tracking-Automation"
#  3. Run:
#       powershell -ExecutionPolicy Bypass -File scheduler\setup_scheduler.ps1
#  ─────────────────────────────────────────────────────────────────────────────
#  After running, tasks fire silently every day at:
#    - NDC Report (10:00, 13:00, 16:00, 19:00)
#    - F&F Report (10:15, 13:15, 16:15, 19:15)
#  If the PC was OFF/asleep at trigger time, task runs automatically on next boot.
#
#  TO UNINSTALL:
#       powershell -ExecutionPolicy Bypass -File scheduler\setup_scheduler.ps1 -Uninstall
# =============================================================================

param(
    [switch]$Uninstall
)

# ── Resolve paths (auto-detects project root from this script's location) ─────
$ROOT     = Split-Path -Parent $PSScriptRoot        # scheduler/ → project root
$PYTHONW  = Join-Path $ROOT ".venv\Scripts\pythonw.exe"
$NDC_SCRIPT = Join-Path $ROOT "scheduler\run_pipeline.py"
$FNF_SCRIPT = Join-Path $ROOT "scripts\process_fnf_closed_reports.py"

$ndcTaskNames = @("NDC_Pipeline_1000", "NDC_Pipeline_1300", "NDC_Pipeline_1600", "NDC_Pipeline_1900")
$fnfTaskNames = @("FNF_Pipeline_1015", "FNF_Pipeline_1315", "FNF_Pipeline_1615", "FNF_Pipeline_1915")
$allTaskNames = $ndcTaskNames + $fnfTaskNames

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  NDC & F&F Pipeline - Task Scheduler Setup" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  Project root : $ROOT"
Write-Host "  Python       : $PYTHONW"
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

# ── Uninstall mode ────────────────────────────────────────────────────────────
if ($Uninstall) {
    Write-Host "Uninstalling all pipeline tasks..." -ForegroundColor Yellow
    foreach ($name in $allTaskNames) {
        try {
            $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
            if ($task) {
                if ($task.State -eq "Running") {
                    Stop-ScheduledTask -TaskName $name
                }
                Unregister-ScheduledTask -TaskName $name -Confirm:$false
                Write-Host "  [OK] Removed: $name" -ForegroundColor Green
            } else {
                Write-Host "  [SKIP] Not found: $name" -ForegroundColor Gray
            }
        } catch {
            Write-Host "  [FAIL] $name : $_" -ForegroundColor Red
        }
    }
    Write-Host ""
    exit 0
}

# ── Check virtual environment exists ─────────────────────────────────────────
if (-not (Test-Path $PYTHONW)) {
    Write-Host "[ERROR] pythonw.exe not found: $PYTHONW" -ForegroundColor Red
    Write-Host ""
    Write-Host "  Set up the virtual environment first:" -ForegroundColor Yellow
    Write-Host "    cd `"$ROOT`""
    Write-Host "    python -m venv .venv"
    Write-Host "    .venv\Scripts\pip install -e ."
    Write-Host ""
    exit 1
}

# ── Task configuration ────────────────────────────────────────────────────────
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -MultipleInstances        IgnoreNew `
    -ExecutionTimeLimit       (New-TimeSpan -Hours 2) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

$ndcAction = New-ScheduledTaskAction `
    -Execute   $PYTHONW `
    -Argument  "`"$NDC_SCRIPT`"" `
    -WorkingDirectory $ROOT

$fnfAction = New-ScheduledTaskAction `
    -Execute   $PYTHONW `
    -Argument  "`"$FNF_SCRIPT`"" `
    -WorkingDirectory $ROOT

$ndcTasks = @(
    @{ Name = "NDC_Pipeline_1000"; Time = "10:00" },
    @{ Name = "NDC_Pipeline_1300"; Time = "13:00" },
    @{ Name = "NDC_Pipeline_1600"; Time = "16:00" },
    @{ Name = "NDC_Pipeline_1900"; Time = "19:00" }
)

$fnfTasks = @(
    @{ Name = "FNF_Pipeline_1015"; Time = "10:15" },
    @{ Name = "FNF_Pipeline_1315"; Time = "13:15" },
    @{ Name = "FNF_Pipeline_1615"; Time = "16:15" },
    @{ Name = "FNF_Pipeline_1915"; Time = "19:15" }
)

# ── Register tasks ────────────────────────────────────────────────────────────
$failed = 0

Write-Host "Registering NDC Tasks..." -ForegroundColor Cyan
foreach ($t in $ndcTasks) {
    try {
        $trigger = New-ScheduledTaskTrigger -Daily -At $t.Time
        Register-ScheduledTask `
            -TaskName $t.Name `
            -Action   $ndcAction `
            -Trigger  $trigger `
            -Settings $settings `
            -RunLevel Limited `
            -Force -ErrorAction Stop | Out-Null
        Write-Host "  [OK] $($t.Name)  ->  $($t.Time) daily" -ForegroundColor Green
    } catch {
        Write-Host "  [FAIL] $($t.Name): $_" -ForegroundColor Red
        $failed++
    }
}

Write-Host "`nRegistering F&F Tasks..." -ForegroundColor Cyan
foreach ($t in $fnfTasks) {
    try {
        $trigger = New-ScheduledTaskTrigger -Daily -At $t.Time
        Register-ScheduledTask `
            -TaskName $t.Name `
            -Action   $fnfAction `
            -Trigger  $trigger `
            -Settings $settings `
            -RunLevel Limited `
            -Force -ErrorAction Stop | Out-Null
        Write-Host "  [OK] $($t.Name)  ->  $($t.Time) daily" -ForegroundColor Green
    } catch {
        Write-Host "  [FAIL] $($t.Name): $_" -ForegroundColor Red
        $failed++
    }
}

Write-Host ""

if ($failed -eq 0) {
    Write-Host "============================================================" -ForegroundColor Green
    Write-Host "  SUCCESS! All tasks registered." -ForegroundColor Green
    Write-Host ""
    Write-Host "  NDC Runs silently at: 10:00 | 13:00 | 16:00 | 19:00" -ForegroundColor Green
    Write-Host "  F&F Runs silently at: 10:15 | 13:15 | 16:15 | 19:15" -ForegroundColor Green
    Write-Host "  Missed triggers fire automatically on next boot/wake." -ForegroundColor Green
    Write-Host ""
    Write-Host "  Commands:" -ForegroundColor Gray
    Write-Host "    Uninstall    : scheduler\setup_scheduler.ps1 -Uninstall" -ForegroundColor Gray
    Write-Host "  Logs:" -ForegroundColor Gray
    Write-Host "    NDC Pipeline : $ROOT\logs\pipeline.log" -ForegroundColor Gray
    Write-Host "    F&F Pipeline : $ROOT\logs\process_fnf_closed_reports.log" -ForegroundColor Gray
    Write-Host "============================================================" -ForegroundColor Green
} else {
    Write-Host "[WARNING] $failed task(s) failed to register." -ForegroundColor Yellow
}

Write-Host ""
