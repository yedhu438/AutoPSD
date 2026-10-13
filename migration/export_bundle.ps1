<#
.SYNOPSIS
  Run on the OLD PC. Gathers everything the new PC needs that git does not carry into
  .\bundle  (git-ignored: it contains secrets - treat it like a password file).

.DESCRIPTION
  Collects: .env, template\, nssm.exe + rclone.exe, the VarsanyDaemon service settings,
  the pinned Python package list, and every custom font (union of the font staging
  folder, the non-stock files in C:\Windows\Fonts, and the per-user fonts folder;
  .ttf/.otf only). Finishes with SHA256SUMS.txt so the new PC can detect a damaged copy.
  Safe to re-run (unchanged files are skipped). Never stops or changes a service or task.

  rclone.conf holds the B2 key. From a NORMAL PowerShell window add -IncludeRcloneConf to
  copy it too. Do not use that switch from inside the Claude desktop app: its view of
  %APPDATA% can be a stale private copy (see MIGRATION.md).

  ASCII only on purpose: Windows PowerShell 5.1 reads BOM-less files as ANSI.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File C:\gimpTest\migration\export_bundle.ps1 -IncludeRcloneConf
#>
[CmdletBinding()]
param(
    [string]$ProjectDir  = 'C:\gimpTest',
    [string]$BundleDir,
    [string]$FontStaging,
    [switch]$SkipFonts,
    [switch]$SkipTemplates,
    [switch]$IncludeRcloneConf
)

$ErrorActionPreference = 'Stop'

# $PSScriptRoot can be empty inside param() defaults, so resolve the defaults here instead
$ScriptDir = $PSScriptRoot
if (-not $ScriptDir -and $MyInvocation.MyCommand.Path) { $ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $ScriptDir) { $ScriptDir = Join-Path $ProjectDir 'migration' }
if (-not $BundleDir)   { $BundleDir   = Join-Path $ScriptDir 'bundle' }
if (-not $FontStaging) { $FontStaging = Join-Path $env:USERPROFILE 'Desktop\fonts' }

function Say([string]$Message, [string]$Color = 'Gray') { Write-Host $Message -ForegroundColor $Color }

function Invoke-Native {
    param([string]$Exe, [string[]]$Arguments)
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { $out = & $Exe @Arguments 2>&1 | Out-String } finally { $ErrorActionPreference = $old }
    return ($out -replace "`0", '').Trim()
}

function Copy-IfDifferent([string]$Src, [string]$Dst) {
    if ((Test-Path -LiteralPath $Dst) -and ((Get-Item -LiteralPath $Dst).Length -eq (Get-Item -LiteralPath $Src).Length)) { return $false }
    Copy-Item -LiteralPath $Src -Destination $Dst -Force
    return $true
}

function Get-FontFiles([string]$Dir, [bool]$Recurse) {
    if (-not (Test-Path -LiteralPath $Dir)) { return @() }
    if ($Recurse) { $items = Get-ChildItem -LiteralPath $Dir -Recurse -File -ErrorAction SilentlyContinue }
    else          { $items = Get-ChildItem -LiteralPath $Dir -File -ErrorAction SilentlyContinue }
    return @($items | Where-Object { $_.Extension -match '^\.(ttf|otf)$' })
}

$cfgDir  = Join-Path $BundleDir 'config'
$toolDir = Join-Path $BundleDir 'tools'
$fontDir = Join-Path $BundleDir 'fonts'
$tplDir  = Join-Path $BundleDir 'template'
foreach ($d in $cfgDir, $toolDir, $fontDir, $tplDir) { New-Item -ItemType Directory -Force -Path $d | Out-Null }
Say "Bundle folder: $BundleDir" 'Cyan'

# 1. .env ---------------------------------------------------------------------------
$envSrc = Join-Path $ProjectDir '.env'
if (Test-Path -LiteralPath $envSrc) {
    Copy-Item -LiteralPath $envSrc -Destination (Join-Path $cfgDir '.env') -Force
    Say '[ok] .env copied (contains the DB password)' 'Green'
} else { Say '[!!] .env not found - the new PC will need one' 'Yellow' }

# 2. template\ ----------------------------------------------------------------------
if (-not $SkipTemplates) {
    $tplSrc = Join-Path $ProjectDir 'template'
    if (Test-Path -LiteralPath $tplSrc) {
        & robocopy $tplSrc $tplDir /E /R:1 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
        if ($LASTEXITCODE -ge 8) { throw "robocopy of template\ failed (exit $LASTEXITCODE)" }
        $n = @(Get-ChildItem -LiteralPath $tplDir -Recurse -File).Count
        Say "[ok] template\ -> $n file(s)" 'Green'
    } else { Say '[!!] template\ not found' 'Yellow' }
}

# 3. tools --------------------------------------------------------------------------
foreach ($t in @(@{ Src = 'C:\nssm\nssm.exe'; Name = 'nssm.exe' }, @{ Src = 'C:\rclone\rclone.exe'; Name = 'rclone.exe' })) {
    if (Test-Path -LiteralPath $t.Src) {
        [void](Copy-IfDifferent $t.Src (Join-Path $toolDir $t.Name))
        Say "[ok] tools\$($t.Name)" 'Green'
    } else { Say "[!!] $($t.Src) not found - the setup script will try to download it" 'Yellow' }
}

# 4. service settings, python version, pinned packages, machine facts -----------------
$info = [ordered]@{
    exportedAt = (Get-Date).ToString('s')
    hostname   = $env:COMPUTERNAME
    user       = $env:USERNAME
    timeZone   = (Get-TimeZone).Id
}
$key = 'HKLM:\SYSTEM\CurrentControlSet\Services\VarsanyDaemon'
$p = Get-ItemProperty -Path "$key\Parameters" -ErrorAction SilentlyContinue
$pyPath = $null
if ($p) {
    $cim = Get-CimInstance Win32_Service -Filter "Name='VarsanyDaemon'" -ErrorAction SilentlyContinue
    $startName = $null; $startMode = $null
    if ($cim) { $startName = $cim.StartName; $startMode = $cim.StartMode }
    $info['service'] = [ordered]@{
        Application   = $p.Application
        AppParameters = $p.AppParameters
        AppDirectory  = $p.AppDirectory
        AppStdout     = $p.AppStdout
        AppStderr     = $p.AppStderr
        StartName     = $startName
        StartMode     = $startMode
    }
    if ($p.PSObject.Properties.Name -contains 'AppEnvironmentExtra') { Say '[!!] the service sets extra environment variables (AppEnvironmentExtra) - NOT exported, re-add by hand' 'Yellow' }
    $pyPath = $p.Application
    Say "[ok] service settings captured: $($p.AppParameters)" 'Green'
} else { Say '[!!] VarsanyDaemon service not found - service settings not captured' 'Yellow' }

if ($pyPath -and (Test-Path -LiteralPath $pyPath)) {
    $ver = Invoke-Native $pyPath @('--version')
    $info['python'] = [ordered]@{ path = $pyPath; version = $ver }
    $freeze = @((Invoke-Native $pyPath @('-m', 'pip', 'list', '--format=freeze')) -split "`r?`n" | Where-Object { $_ -match '^[^=\s]+==\S+$' -and $_ -notmatch '^(pip|setuptools|wheel)==' })
    Set-Content -Path (Join-Path $cfgDir 'requirements.lock.txt') -Value $freeze -Encoding ASCII
    Say "[ok] $ver, $($freeze.Count) pinned packages" 'Green'
} else { Say '[!!] could not find the service Python - requirements.lock.txt not written' 'Yellow' }

$psExe = 'C:\Program Files\Adobe\Adobe Photoshop 2026\Photoshop.exe'
if (Test-Path -LiteralPath $psExe) { $info['photoshop'] = (Get-Item -LiteralPath $psExe).VersionInfo.ProductVersion }
Set-Content -Path (Join-Path $cfgDir 'old_pc.json') -Value ($info | ConvertTo-Json -Depth 5) -Encoding UTF8
Say '[ok] config\old_pc.json written' 'Green'

# 5. rclone.conf (optional) ---------------------------------------------------------
if ($IncludeRcloneConf) {
    $rc = Join-Path $env:APPDATA 'rclone\rclone.conf'
    if (Test-Path -LiteralPath $rc) {
        Copy-Item -LiteralPath $rc -Destination (Join-Path $cfgDir 'rclone.conf') -Force
        Say ("[ok] rclone.conf copied (last written {0}) - SECRET (B2 key)" -f (Get-Item -LiteralPath $rc).LastWriteTime) 'Green'
    } else { Say "[!!] $rc not found" 'Yellow' }
} else {
    Say '[--] rclone.conf not included (re-run with -IncludeRcloneConf from a normal PowerShell window, or type the B2 key on the new PC)' 'DarkGray'
}

# 6. fonts --------------------------------------------------------------------------
if (-not $SkipFonts) {
    $stock = 'NT SERVICE\TrustedInstaller'
    Say 'Collecting fonts (checking owners in C:\Windows\Fonts takes a few seconds)...' 'Gray'
    $winFonts = @(Get-FontFiles 'C:\Windows\Fonts' $false | Where-Object { try { (Get-Acl -LiteralPath $_.FullName).Owner -ne $stock } catch { $false } })
    $sources = @(
        @{ Label = 'staging folder';        Files = @(Get-FontFiles $FontStaging $true) },
        @{ Label = 'C:\Windows\Fonts';      Files = $winFonts },
        @{ Label = 'per-user fonts folder'; Files = @(Get-FontFiles (Join-Path $env:LOCALAPPDATA 'Microsoft\Windows\Fonts') $false) }
    )
    $seen = @{}
    foreach ($s in $sources) {
        $added = 0
        foreach ($f in $s.Files) {
            $k = $f.Name.ToLower()
            if ($seen.ContainsKey($k)) { continue }
            $seen[$k] = $true
            [void](Copy-IfDifferent $f.FullName (Join-Path $fontDir $f.Name))
            $added++
        }
        Say ('[ok] fonts from {0,-22} {1,5} unique file(s) added (source has {2})' -f $s.Label, $added, @($s.Files).Count) 'Green'
    }
    Say "[ok] fonts\ now holds $($seen.Count) unique .ttf/.otf files (.ttc/.fon are stock Windows fonts - skipped)" 'Green'
}

# 7. checksums ----------------------------------------------------------------------
Say 'Hashing the bundle (a minute or so)...' 'Gray'
$root = (Resolve-Path -LiteralPath $BundleDir).Path.TrimEnd('\')
$lines = foreach ($f in (Get-ChildItem -LiteralPath $root -Recurse -File | Where-Object { $_.Name -ne 'SHA256SUMS.txt' })) {
    '{0}  {1}' -f (Get-FileHash -LiteralPath $f.FullName -Algorithm SHA256).Hash, $f.FullName.Substring($root.Length + 1)
}
Set-Content -Path (Join-Path $root 'SHA256SUMS.txt') -Value $lines -Encoding UTF8

$total = Get-ChildItem -LiteralPath $root -Recurse -File | Measure-Object Length -Sum
Say ('Done: {0} files, {1:N0} MB in {2}' -f $total.Count, ($total.Sum / 1MB), $root) 'Cyan'
Say 'Next: copy that folder to <project>\migration\bundle on the new PC (USB/LAN). It holds secrets - delete the copies afterwards.' 'Cyan'
