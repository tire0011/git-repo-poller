# Offline end-to-end smoke test: two real temporary Git remotes, no network/LLM.
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$root = Join-Path ([IO.Path]::GetTempPath()) ('git-poller-smoke-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $root | Out-Null
function Invoke-TestGit([string]$Where, [string[]]$GitArgs) {
    $result = @(& git -C $Where @GitArgs 2>&1)
    if ($LASTEXITCODE -ne 0) { throw "git $($GitArgs -join ' ') failed: $($result -join ' ')" }
    return (($result | Out-String).Trim())
}
function Assert([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw "SMOKE FAIL: $Message" }
}
try {
    $profiles = @()
    foreach ($id in @('alpha','beta')) {
        $bare = Join-Path $root "$id.git"
        $seed = Join-Path $root "$id-seed"
        & git init --bare $bare | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Could not init bare remote' }
        & git clone $bare $seed 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Could not clone bare remote' }
        $script = @'
Add-Content -LiteralPath (Join-Path $env:GIT_POLLER_RUN_DIR "ID-marker.txt") -Value "run"
Write-Output "dummy experiment completed"
'@.Replace('ID', $id)
        Set-Content -LiteralPath (Join-Path $seed 'run.ps1') -Value $script -Encoding utf8
        $null = Invoke-TestGit $seed @('add','run.ps1')
        $null = Invoke-TestGit $seed @('-c','user.name=Smoke','-c','user.email=smoke@example.test','commit','-m','first')
        $null = Invoke-TestGit $seed @('branch','-M','main')
        $null = Invoke-TestGit $seed @('push','origin','main')
        $profiles += [pscustomobject]@{
            id = $id; repo = $bare; branch = 'main'; entrypoint = 'run.ps1'
            timeoutSeconds = 10; runOnChange = $true; ignorePaths = @('results/')
        }
    }
    $config = [ordered]@{ workerId = 'smoke'; autoUpdate = $false; experiments = $profiles }
    $configPath = Join-Path $root 'config.json'
    $config | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $configPath -Encoding utf8
    $poll = Join-Path (Split-Path -Parent $PSScriptRoot) 'poll.ps1'
    & $poll -ConfigPath $configPath | Out-Null
    foreach ($id in @('alpha','beta')) {
        $result = Get-Content -LiteralPath (Join-Path $root "state/$id.json") -Raw | ConvertFrom-Json
        Assert ($result.status -eq 'succeeded') "$id initial run"
        Assert ((Get-Content -LiteralPath (Join-Path $root "logs/$id-marker.txt")).Count -eq 1) "$id ran once"
    }
    & $poll -ConfigPath $configPath | Out-Null
    foreach ($id in @('alpha','beta')) {
        Assert ((Get-Content -LiteralPath (Join-Path $root "logs/$id-marker.txt")).Count -eq 1) "$id did not rerun without updates"
    }
    $alphaSeed = Join-Path $root 'alpha-seed'
    Set-Content -LiteralPath (Join-Path $alphaSeed 'change.txt') -Value 'changed'
    $null = Invoke-TestGit $alphaSeed @('add','change.txt')
    $null = Invoke-TestGit $alphaSeed @('-c','user.name=Smoke','-c','user.email=smoke@example.test','commit','-m','change-alpha')
    $null = Invoke-TestGit $alphaSeed @('push','origin','main')
    & $poll -ConfigPath $configPath | Out-Null
    Assert ((Get-Content -LiteralPath (Join-Path $root 'logs/alpha-marker.txt')).Count -eq 2) 'alpha updated and reran'
    Assert ((Get-Content -LiteralPath (Join-Path $root 'logs/beta-marker.txt')).Count -eq 1) 'beta unaffected'
    $results = Join-Path $alphaSeed 'results'
    New-Item -ItemType Directory -Force -Path $results | Out-Null
    Set-Content -LiteralPath (Join-Path $results 'metrics.txt') -Value 'no rerun'
    $null = Invoke-TestGit $alphaSeed @('add','results/metrics.txt')
    $null = Invoke-TestGit $alphaSeed @('-c','user.name=Smoke','-c','user.email=smoke@example.test','commit','-m','results-only')
    $null = Invoke-TestGit $alphaSeed @('push','origin','main')
    & $poll -ConfigPath $configPath | Out-Null
    Assert ((Get-Content -LiteralPath (Join-Path $root 'logs/alpha-marker.txt')).Count -eq 2) 'results-only commit ignored'
    $alphaClone = Join-Path $root 'workspaces/alpha'
    Set-Content -LiteralPath (Join-Path $alphaClone 'untracked.txt') -Value 'keep me'
    & $poll -ConfigPath $configPath -Only alpha -Force | Out-Null
    $last = Get-Content -LiteralPath (Join-Path $root 'state/alpha.json') -Raw | ConvertFrom-Json
    Assert ($last.status -eq 'error') 'dirty tree refused'
    Assert (Test-Path -LiteralPath (Join-Path $alphaClone 'untracked.txt')) 'dirty file preserved'
    Write-Output 'SMOKE PASS: two remotes, first-run/no-change, selective update, results-only skip, dirty-tree preservation.'
} finally {
    Remove-Item -LiteralPath $root -Force -Recurse -ErrorAction SilentlyContinue
}
