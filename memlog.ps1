<#
.SYNOPSIS
  Log memory use of the Varsany pipeline (Photoshop, Python daemon, headless Chrome) to a daily CSV.

.DESCRIPTION
  One row per sample in C:\gimpTest\Temp\memlog\memlog_YYYY-MM-DD.csv: overall RAM and commit, then working set
  (RAM actually held) and private bytes (memory committed) per process group, plus the biggest other process.
  A steadily rising PS_Priv_GB or Py_Priv_GB across jobs points at a leak; RamUsedPct near 100 with CommitUsedGB
  near CommitLimitGB is the danger zone. Never stops or changes anything.
  Started hidden at sign-in by the VarsanyMemLog scheduled task. ASCII only (PowerShell 5.1 reads BOM-less files as ANSI).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File C:\gimpTest\memlog.ps1 -Once
#>
param(
    [int]$IntervalSeconds = 300,
    [string]$LogDir = 'C:\gimpTest\Temp\memlog',
    [switch]$Once
)

$groups = [ordered]@{ PS = 'Photoshop'; Py = 'python|pythonw|py'; Chrome = 'chrome' }

function Write-Sample {
    try {
        $os  = Get-CimInstance Win32_OperatingSystem
        $gb  = 1MB   # Win32_OperatingSystem reports KB, so KB / 1MB = GB
        $ramTotal = $os.TotalVisibleMemorySize / $gb
        $ramUsed  = ($os.TotalVisibleMemorySize - $os.FreePhysicalMemory) / $gb
        $row = [ordered]@{
            Time          = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
            RamUsedPct    = [math]::Round(100 * $ramUsed / $ramTotal, 1)
            RamUsedGB     = [math]::Round($ramUsed, 1)
            CommitUsedGB  = [math]::Round(($os.TotalVirtualMemorySize - $os.FreeVirtualMemory) / $gb, 1)
            CommitLimitGB = [math]::Round($os.TotalVirtualMemorySize / $gb, 1)
        }
        $procs = @(Get-Process -ErrorAction SilentlyContinue)
        $matched = @{}
        foreach ($g in $groups.Keys) {
            $set = @($procs | Where-Object { $_.ProcessName -match "^($($groups[$g]))$" })
            foreach ($p in $set) { $matched[$p.Id] = $true }
            $row["${g}_WS_GB"]   = [math]::Round((($set | Measure-Object WorkingSet64 -Sum).Sum) / 1GB, 2)
            $row["${g}_Priv_GB"] = [math]::Round((($set | Measure-Object PrivateMemorySize64 -Sum).Sum) / 1GB, 2)
            $row["${g}_Count"]   = $set.Count
        }
        $other = $procs | Where-Object { -not $matched.ContainsKey($_.Id) } | Sort-Object WorkingSet64 -Descending | Select-Object -First 1
        $row['TopOther']       = if ($other) { $other.ProcessName } else { '' }
        $row['TopOther_WS_GB'] = if ($other) { [math]::Round($other.WorkingSet64 / 1GB, 2) } else { 0 }

        if (-not (Test-Path -LiteralPath $LogDir)) { New-Item -ItemType Directory -Force -Path $LogDir | Out-Null }
        $file = Join-Path $LogDir ('memlog_{0}.csv' -f (Get-Date).ToString('yyyy-MM-dd'))
        [pscustomobject]$row | Export-Csv -LiteralPath $file -Append -NoTypeInformation -Encoding ASCII
    } catch {
        # a failed sample must never kill the logger; note it and carry on
        try { Add-Content -LiteralPath (Join-Path $LogDir 'memlog_errors.txt') -Value ("{0}  {1}" -f (Get-Date), $_.Exception.Message) } catch { }
    }
}

if ($Once) { Write-Sample; return }
while ($true) { Write-Sample; Start-Sleep -Seconds $IntervalSeconds }
