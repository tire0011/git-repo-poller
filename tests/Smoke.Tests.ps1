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
    # Smoke-test the installer and stable launcher with a third, local Git remote.
    $engineBare = Join-Path $root 'poller-engine.git'
    $engineSeed = Join-Path $root 'poller-engine-seed'
    & git init --bare $engineBare | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Engine bare init failed' }
    & git clone $engineBare $engineSeed 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Engine clone failed' }
    $sourceRoot = Split-Path -Parent $PSScriptRoot
    Copy-Item -LiteralPath (Join-Path $sourceRoot 'poll.ps1') -Destination (Join-Path $engineSeed 'poll.ps1')
    Copy-Item -LiteralPath (Join-Path $sourceRoot 'launcher.ps1') -Destination (Join-Path $engineSeed 'launcher.ps1')
    $null = Invoke-TestGit $engineSeed @('add','poll.ps1','launcher.ps1')
    $null = Invoke-TestGit $engineSeed @('-c','user.name=Smoke','-c','user.email=smoke@example.test','commit','-m','engine-v1')
    $null = Invoke-TestGit $engineSeed @('branch','-M','main')
    $null = Invoke-TestGit $engineSeed @('push','origin','main')
    $config['engineRemote'] = $engineBare
    $config['engineBranch'] = 'main'
    $config['intervalMinutes'] = 5
    $config | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $configPath -Encoding utf8
    $install = Join-Path $root 'installed poller with spaces'
    & (Join-Path $sourceRoot 'setup.ps1') -NonInteractive -ConfigPath $configPath -InstallRoot $install -NoScheduledTask
    Assert (Test-Path -LiteralPath (Join-Path $install 'launcher.ps1')) 'installer copied stable launcher'
    Assert (Test-Path -LiteralPath (Join-Path $install 'engine/.git')) 'installer cloned engine'
    & (Join-Path $install 'launcher.ps1') -InstallRoot $install -CheckOnly | Out-Null
    & (Join-Path $install 'launcher.ps1') -InstallRoot $install -Only beta -Force | Out-Null
    $betaMarker = Join-Path $install 'logs/beta-marker.txt'
    Assert (Test-Path -LiteralPath $betaMarker) 'launcher executed beta in a separate installed workspace'
    Assert ((Get-Content -LiteralPath $betaMarker).Count -eq 1) 'launcher ran beta exactly once'
    Write-Output 'SMOKE PASS: two remotes, selective changes, ignored results, dirty preservation, unattended installation and launcher.'
} finally {
    Remove-Item -LiteralPath $root -Force -Recurse -ErrorAction SilentlyContinue
}
