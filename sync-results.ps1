# Adapted from tire0011/ai-native-code-bench/sync-results.ps1.
# The original benchmark owns the result-producing experiment; this reusable
# helper only reconciles ALLOWLISTED result paths before an experiment Git poll.
# It never rebases code commits, resets a worktree or force pushes.
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$RepoPath,
    [Parameter(Mandatory)][string]$Branch,
    [Parameter(Mandatory)][string]$ExpectedRemote,
    [Parameter(Mandatory)][string[]]$ResultPaths,
    [switch]$CommitPending
)
$ErrorActionPreference = 'Stop'
$RepoPath = [IO.Path]::GetFullPath($RepoPath)
if (-not (Test-Path -LiteralPath (Join-Path $RepoPath '.git'))) { throw "Not a Git checkout: $RepoPath" }
if (-not $ResultPaths -or $ResultPaths.Count -eq 0) { throw 'Explicit result paths required' }
$prefixes = @($ResultPaths | ForEach-Object {
    $p = [string]$_
    $p = $p.Replace('\', '/').Trim()
    if ($p.StartsWith('/') -or $p -match '^[A-Za-z]:') { throw "Absolute result path not allowed: $_" }
    $p = $p.Trim('/')
    if (-not $p -or $p -eq '.' -or $p -match '(^|/)\.\.(/|$)' -or $p.StartsWith('/') -or $p.Contains('*')) {
        throw "Unsafe result path: $_"
    }
    $p + '/'
})
function Is-ResultPath([string]$Path) {
    $p = $Path.Replace('\', '/').TrimStart([char[]]@('.','/'))
    foreach ($prefix in $prefixes) {
        if ($p.StartsWith($prefix, [StringComparison]::Ordinal)) { return $true }
    }
    return $false
}
function Git([string[]]$Arguments) {
    $output = @(& git -C $RepoPath @Arguments 2>&1)
    if ($LASTEXITCODE -ne 0) { throw "git $($Arguments -join ' ') failed: $($output -join ' ')" }
    return $output
}
$remote = (Git @('remote', 'get-url', 'origin') | Select-Object -First 1).Trim()
$currentBranch = (Git @('branch', '--show-current') | Select-Object -First 1).Trim()
if ($remote -ne $ExpectedRemote -or $currentBranch -ne $Branch) {
    throw "Refusing results sync: unexpected remote/branch $remote [$currentBranch]"
}
$tracked = @(Git @('diff', '--name-only', 'HEAD') | Where-Object { $_ })
$outside = @($tracked | Where-Object { -not (Is-ResultPath ([string]$_)) })
if ($outside.Count -gt 0) { throw "Non-result changes: $($outside -join ', '). Preserved." }
$staged = @(Git @('diff', '--cached', '--name-only') | Where-Object { $_ })
if (@($staged | Where-Object { -not (Is-ResultPath ([string]$_)) }).Count -gt 0) {
    throw 'Non-result files are staged. Refusing automatic commit/rebase.'
}
if ($CommitPending) {
    $pending = @(Git (@('status', '--porcelain', '--') + $ResultPaths) | Where-Object { $_ })
    if ($pending.Count -gt 0) {
        # Only stage paths with changes; leave every other file untouched.
        $candidatePaths = @()
        foreach ($line in $pending) {
            $path = ([string]$line).Substring(3).Trim('"')
            if (-not (Is-ResultPath $path)) { throw "Unexpected pending path: $path" }
            $candidatePaths += $path
        }
        if ($candidatePaths.Count -gt 0) {
            Git (@('add', '-A', '--') + $candidatePaths) | Out-Null
            Git @('commit', '-m', ('Experiment results ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))) | Out-Null
            Write-Output "Committed $($candidatePaths.Count) result-path change(s)."
        }
    }
}
if (@(Git @('status', '--porcelain', '--untracked-files=no') | Where-Object { $_ }).Count -gt 0) {
    throw 'Tracked worktree changes remain. Refusing automatic rebase.'
}

# From the original benchmark: bounded fetch/rebase/push retry and guard that
# EVERY local-only commit touches only designated result files.
for ($attempt = 1; $attempt -le 4; $attempt++) {
    Git @('fetch', 'origin', $Branch, '--quiet') | Out-Null
    $local = (Git @('rev-parse', 'HEAD') | Select-Object -First 1).Trim()
    $remoteHead = (Git @('rev-parse', "origin/$Branch") | Select-Object -First 1).Trim()
    if ($local -ne $remoteHead) {
        $commits = @(Git @('rev-list', '--reverse', "origin/$Branch..HEAD") | Where-Object { $_ })
        foreach ($sha in $commits) {
            $parents = ((Git @('rev-list', '--parents', '-n', '1', $sha) | Select-Object -First 1) -split '\s+')
            if ($parents.Count -ne 2) { throw "Local merge/root commit $sha needs manual review" }
            $paths = @(Git @('diff-tree', '--no-commit-id', '--name-only', '-r', $sha) | Where-Object { $_ })
            if ($paths.Count -eq 0 -or @($paths | Where-Object { -not (Is-ResultPath ([string]$_)) }).Count -gt 0) {
                throw "Local commit $sha contains non-result files; no automatic rebase."
            }
        }
        if ($commits.Count -gt 0) {
            Write-Output "Rebasing $($commits.Count) result-only commit(s) on origin/$Branch..."
            & git -C $RepoPath rebase "origin/$Branch"
            if ($LASTEXITCODE -ne 0) {
                & git -C $RepoPath rebase --abort 2>&1 | Out-Null
                throw 'Rebase conflicted; aborted without discarding local commits.'
            }
        } else {
            Git @('merge', '--ff-only', "origin/$Branch") | Out-Null
        }
    }
    $ahead = @(Git @('rev-list', "origin/$Branch..HEAD") | Where-Object { $_ })
    if ($ahead.Count -eq 0) { return }
    Write-Output "Publishing result-only commits (attempt $attempt/4)..."
    & git -C $RepoPath push origin $Branch
    if ($LASTEXITCODE -eq 0) { Write-Output 'Experiment results published'; return }
    Start-Sleep -Seconds 2
}
throw 'Result-only push failed after four attempts; local commits preserved.'
