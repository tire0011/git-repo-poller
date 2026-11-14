# Stable bootstrapper: stays OUTSIDE the updating engine checkout.
[CmdletBinding()]
param(
    [string]$InstallRoot = $PSScriptRoot,
    [string]$Only = '',
    [switch]$Force,
    [switch]$CheckOnly,
    [switch]$NoUpdate
)
$ErrorActionPreference = 'Stop'
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
$configPath = Join-Path $InstallRoot 'config.json'
if (-not (Test-Path -LiteralPath $configPath)) { throw "Run setup.ps1 first: $configPath not found" }
$config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
$engine = Join-Path $InstallRoot 'engine'
$versions = Join-Path $InstallRoot 'versions'
$selectedFile = Join-Path $InstallRoot 'current-version.txt'
if (-not (Test-Path -LiteralPath (Join-Path $engine '.git'))) { throw 'Engine checkout missing; run setup.ps1 -Repair' }
if (-not (Test-Path -LiteralPath $versions)) { New-Item -ItemType Directory -Path $versions -Force | Out-Null }
if ($CheckOnly) {
    & (Join-Path $engine 'poll.ps1') -ConfigPath $configPath -Only $Only -CheckOnly
    return
}
if (Test-Path -LiteralPath (Join-Path $InstallRoot 'paused.flag')) {
    Write-Output 'Poller is paused.'
    return
}
$mutex = [Threading.Mutex]::new($false, 'Local\git-repo-poller-launcher')
$acquired = $false
try {
    try { $acquired = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $acquired = $true }
    if (-not $acquired) { Write-Output 'Another launcher is active. Skipping tick.'; return }
    $old = if (Test-Path -LiteralPath $selectedFile) { (Get-Content $selectedFile -Raw).Trim() } else { '' }
    $sha = $old
    if (-not $NoUpdate -and $config.autoUpdate -ne $false) {
        $remote = @(& git -C $engine remote get-url origin 2>&1)
        if ($LASTEXITCODE -ne 0 -or ($remote | Out-String).Trim() -ne [string]$config.engineRemote) {
            throw 'Engine origin differs from configured trusted remote. Update refused.'
        }
        $dirty = @(& git -C $engine status --porcelain --untracked-files=all)
        if ($LASTEXITCODE -ne 0 -or @($dirty | Where-Object { $_ }).Count -gt 0) {
            throw 'Engine checkout is dirty. Refusing self-update.'
        }
        $null = & git -C $engine fetch origin ([string]$config.engineBranch) --quiet
        if ($LASTEXITCODE -ne 0) { Write-Warning 'Engine fetch failed (offline?). Keeping current installed version.' }
        else {
            $null = & git -C $engine merge --ff-only ("origin/" + $config.engineBranch)
            if ($LASTEXITCODE -ne 0) { Write-Warning 'Engine cannot fast-forward. Keeping last known good version.' }
        }
    }
    $candidate = @(& git -C $engine rev-parse HEAD 2>&1)
    if ($LASTEXITCODE -ne 0) { throw 'Cannot read engine revision' }
    $candidateSha = ($candidate | Out-String).Trim()
    $candidatePath = Join-Path (Join-Path $versions $candidateSha) 'poll.ps1'
    if (-not (Test-Path -LiteralPath $candidatePath)) {
        New-Item -ItemType Directory -Path (Split-Path -Parent $candidatePath) -Force | Out-Null
        Copy-Item -LiteralPath (Join-Path $engine 'poll.ps1') -Destination $candidatePath -Force
    }
    # A new engine is only selected after its read-only config smoke test passes.
    $psExe = (Get-Command pwsh -ErrorAction Stop).Source
    $probe = & $psExe -NoProfile -NonInteractive -File $candidatePath -ConfigPath $configPath -CheckOnly 2>&1
    if ($LASTEXITCODE -eq 0) {
        $sha = $candidateSha
        Set-Content -LiteralPath $selectedFile -Value $sha -Encoding ascii
    } elseif ($old) {
        $sha = $old
        Write-Warning "New engine candidate failed check-only; keeping previous revision $old"
    } else {
        throw "No valid engine version available: $(($probe | Out-String).Trim())"
    }
    $script = Join-Path (Join-Path $versions $sha) 'poll.ps1'
    if (-not (Test-Path -LiteralPath $script)) { throw "Selected engine is missing: $script" }
    Write-Output "Engine: $sha"
    $cmd = @('-NoProfile','-NonInteractive','-File',$script,'-ConfigPath',$configPath)
    if ($Only) { $cmd += @('-Only',$Only) }
    if ($Force) { $cmd += '-Force' }
    & $psExe @cmd
    if ($LASTEXITCODE -ne 0) { throw "Engine tick failed (exit $LASTEXITCODE). Last-known-good snapshot retained." }
} finally {
    if ($acquired) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
