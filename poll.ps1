# A single, deliberately boring poll iteration. No background daemon or message bus.
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$ConfigPath,
    [string]$Only = '',
    [switch]$Force,
    [switch]$CheckOnly
)
$ErrorActionPreference = 'Stop'

function Invoke-Git([string]$Directory, [string[]]$GitArgs) {
    $lines = @(& git -C $Directory @GitArgs 2>&1)
    if ($LASTEXITCODE -ne 0) { throw "git $($GitArgs -join ' ') failed: $(($lines | Out-String).Trim())" }
    return (($lines | Out-String).Trim())
}
function Save-Json([string]$Path, [object]$Value) {
    $tmp = "$Path.tmp"
    $Value | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $tmp -Encoding utf8
    Move-Item -LiteralPath $tmp -Destination $Path -Force
}
function Invoke-Entry([string]$ScriptPath, [string]$WorkingDirectory, [int]$TimeoutSeconds, [string]$LogPrefix) {
    $stdout = "$LogPrefix.out.log"
    $stderr = "$LogPrefix.err.log"
    $pwsh = (Get-Command pwsh -ErrorAction Stop).Source
    $arguments = '-NoProfile -NonInteractive -File "' + $ScriptPath + '"'
    $proc = Start-Process -FilePath $pwsh -ArgumentList $arguments -WorkingDirectory $WorkingDirectory `
        -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
    $timedOut = -not $proc.WaitForExit([Math]::Max(1, $TimeoutSeconds) * 1000)
    if ($timedOut) {
        if ($IsWindows) {
            & taskkill.exe /F /T /PID $proc.Id *> $null
        } else {
            try { $proc.Kill($true) } catch { }
        }
        try { $null = $proc.WaitForExit(5000) } catch { }
    }
    return [pscustomobject]@{
        exitCode = $(if ($timedOut) { 124 } else { $proc.ExitCode })
        timedOut = $timedOut
        stdoutLog = $stdout
        stderrLog = $stderr
    }
}

$ConfigPath = [IO.Path]::GetFullPath($ConfigPath)
if (-not (Test-Path -LiteralPath $ConfigPath)) { throw "Configuration not found: $ConfigPath" }
$config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
$root = Split-Path -Parent $ConfigPath
if ($CheckOnly) {
    foreach ($job in @($config.experiments)) {
        if ($Only -and $job.id -ne $Only) { continue }
        Write-Output "CHECK $($job.id): $($job.repo) [$($job.branch)] -> $($job.entrypoint)"
    }
    return
}
if (Test-Path -LiteralPath (Join-Path $root 'paused.flag')) {
    Write-Output 'Poller paused. No updates or commands were run.'
    return
}
$mutex = [Threading.Mutex]::new($false, 'Local\git-repo-poller-experiment-tick')
$acquired = $false
try {
    try { $acquired = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $acquired = $true }
    if (-not $acquired) { Write-Output 'Another poller tick is running. Skipping.'; return }
    $workspaces = Join-Path $root 'workspaces'
    $states = Join-Path $root 'state'
    $logs = Join-Path $root 'logs'
    foreach ($folder in @($workspaces, $states, $logs)) {
        New-Item -ItemType Directory -Force -Path $folder | Out-Null
    }
    foreach ($job in @($config.experiments)) {
        if ($Only -and $job.id -ne $Only) { continue }
        $id = [string]$job.id
        if ($id -notmatch '^[a-zA-Z0-9_-]+$') { throw "Invalid experiment id '$id'" }
        $repo = [string]$job.repo
        $branch = [string]$job.branch
        $entry = [string]$job.entrypoint
        if (-not $repo -or -not $branch -or -not $entry) { throw "Incomplete profile: $id" }
        $dir = Join-Path $workspaces $id
        $stateFile = Join-Path $states "$id.json"
        $state = if (Test-Path -LiteralPath $stateFile) {
            Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
        } else { $null }
        $previous = if ($state) { [string]$state.lastSeenSha } else { '' }
        $record = [ordered]@{
            experimentId = $id; workerId = [string]$config.workerId
            lastSeenSha = $previous; commitSha = ''; status = 'checking'
            startedAt = $null; finishedAt = $null; exitCode = $null
            verificationStatus = 'not_configured'; stdoutLog = $null; stderrLog = $null
            error = $null
        }
        try {
            if (-not (Test-Path -LiteralPath (Join-Path $dir '.git'))) {
                if (Test-Path -LiteralPath $dir) { throw "Path exists but is not a Git clone: $dir" }
                New-Item -ItemType Directory -Force -Path $workspaces | Out-Null
                $output = @(& git clone --single-branch --branch $branch -- $repo $dir 2>&1)
                if ($LASTEXITCODE -ne 0) { throw "Clone failed: $(($output | Out-String).Trim())" }
            }
            $origin = Invoke-Git $dir @('remote','get-url','origin')
            if ($origin -ne $repo) { throw "Wrong Git remote for $id`: '$origin' (expected '$repo')" }
            $currentBranch = Invoke-Git $dir @('branch','--show-current')
            if ($currentBranch -ne $branch) { throw "Wrong branch for $id`: '$currentBranch'" }
            # Reused from AI-Native Code Bench: reconcile pending RESULT-ONLY
            # changes and commits before fetch/fast-forward. This is strictly
            # opt-in per experiment; ordinary repos never auto-commit or push.
            $resultPaths = @($job.resultPaths | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) })
            if ($resultPaths.Count -gt 0) {
                $syncScript = Join-Path $PSScriptRoot 'sync-results.ps1'
                if (-not (Test-Path -LiteralPath $syncScript)) {
                    throw 'Result sync helper missing from installed poller version'
                }
                & $syncScript -RepoPath $dir -Branch $branch -ExpectedRemote $repo -ResultPaths $resultPaths -CommitPending
                if (-not $?) { throw "Result sync failed for $id; leaving checkout untouched" }
            }
            $dirty = Invoke-Git $dir @('status','--porcelain','--untracked-files=all')
            if ($dirty) { throw "Dirty workspace for $id; refusing to pull or discard local changes" }
            $null = Invoke-Git $dir @('fetch','origin',$branch,'--quiet')
            $null = Invoke-Git $dir @('merge','--ff-only',"origin/$branch")
            $sha = Invoke-Git $dir @('rev-parse','HEAD')
            $record.commitSha = $sha
            $shouldRun = $Force -or -not $previous -or ($job.runOnChange -eq $false)
            if (-not $shouldRun -and $previous -ne $sha) {
                $changes = @( (Invoke-Git $dir @('diff','--name-only',$previous,$sha,'--')) -split "`n" | Where-Object { $_ })
                # Result commits are never a reason to rerun the experiment.
                $ignored = @($job.ignorePaths | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) }) + $resultPaths
                $relevant = @($changes | Where-Object {
                    $path = $_
                    @($ignored | Where-Object { $path.StartsWith([string]$_, [StringComparison]::OrdinalIgnoreCase) }).Count -eq 0
                })
                $shouldRun = $relevant.Count -gt 0
            }
            if (-not $shouldRun) {
                $record.status = $(if ($previous -eq $sha) { 'unchanged' } else { 'ignored_change' })
                $record.lastSeenSha = $sha
                $record.finishedAt = [DateTime]::UtcNow.ToString('o')
                Save-Json $stateFile $record
                Write-Output "$id`: $($record.status) ($($sha.Substring(0,8)))"
                continue
            }
            $repoRoot = [IO.Path]::GetFullPath($dir).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
            $script = [IO.Path]::GetFullPath((Join-Path $dir $entry))
            if (-not $script.StartsWith($repoRoot, [StringComparison]::OrdinalIgnoreCase) -or
                -not $script.EndsWith('.ps1', [StringComparison]::OrdinalIgnoreCase) -or
                -not (Test-Path -LiteralPath $script -PathType Leaf)) {
                throw "Entrypoint must be an existing .ps1 file inside the approved checkout: $entry"
            }
            $timeout = if ([int]$job.timeoutSeconds -gt 0) { [int]$job.timeoutSeconds } else { 600 }
            $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
            $prefix = Join-Path $logs "$id-$stamp"
            $record.startedAt = [DateTime]::UtcNow.ToString('o')
            # Persist the attempted commit *before* spawning: a failed task will not
            # restart every poll tick. A manual -Force is required to retry.
            $record.lastSeenSha = $sha
            $record.status = 'running'
            Save-Json $stateFile $record
            $env:GIT_POLLER_RUN_DIR = $logs
            $result = Invoke-Entry $script $dir $timeout $prefix
            $record.exitCode = $result.exitCode
            $record.stdoutLog = $result.stdoutLog
            $record.stderrLog = $result.stderrLog
            $record.status = $(if ($result.timedOut) { 'timed_out' } elseif ($result.exitCode -eq 0) { 'succeeded' } else { 'failed' })
            if ($record.status -eq 'succeeded' -and $job.verifyEntry) {
                $verification = [IO.Path]::GetFullPath((Join-Path $dir ([string]$job.verifyEntry)))
                if (-not $verification.StartsWith($repoRoot, [StringComparison]::OrdinalIgnoreCase) -or
                    -not (Test-Path -LiteralPath $verification -PathType Leaf)) { throw 'Verification script is outside checkout or missing' }
                $vr = Invoke-Entry $verification $dir $timeout "$prefix-verify"
                $record.verificationStatus = $(if ($vr.timedOut) { 'timed_out' } elseif ($vr.exitCode -eq 0) { 'passed' } else { 'failed' })
                if ($record.verificationStatus -ne 'passed') { $record.status = 'verification_failed' }
            }
        } catch {
            $record.status = 'error'
            $record.error = $_.Exception.Message
            Write-Warning "$id`: $($record.error)"
        } finally {
            $record.finishedAt = [DateTime]::UtcNow.ToString('o')
            Save-Json $stateFile $record
        }
        Write-Output "$id`: $($record.status) ($($record.commitSha))"
    }
} finally {
    if ($acquired) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
