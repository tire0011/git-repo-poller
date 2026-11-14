# Offline integration of the original benchmark's results-only commit/rebase flow.
# No Ollama, network, VM, public remote, or actual benchmark is involved.
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$root = Join-Path ([IO.Path]::GetTempPath()) ('poller result sync with spaces ' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $root | Out-Null
function G([string]$Dir, [string[]]$GitArgs) {
    $out = @(& git -C $Dir @GitArgs 2>&1)
    if ($LASTEXITCODE -ne 0) { throw "git $($GitArgs -join ' '): $($out -join ' ')" }
    return $out
}
function Assert([bool]$Condition, [string]$Reason) { if (-not $Condition) { throw "RESULT SYNC TEST FAIL: $Reason" } }
try {
    $bare = Join-Path $root 'bench.git'
    $seed = Join-Path $root 'seed with spaces'
    & git init --bare $bare | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create local bare remote' }
    & git clone $bare $seed 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Failed to clone seed' }
    @'
New-Item -ItemType Directory -Force -Path (Join-Path $PSScriptRoot 'results/runs') | Out-Null
$counter = Join-Path $env:GIT_POLLER_RUN_DIR 'launch-count.txt'
$count = if (Test-Path -LiteralPath $counter) { @(Get-Content -LiteralPath $counter).Count } else { 0 }
Add-Content -LiteralPath $counter -Value 'run'
Set-Content -LiteralPath (Join-Path $PSScriptRoot "results/runs/result-$count.txt") -Value "result $count"
'@ | Set-Content -LiteralPath (Join-Path $seed 'run.ps1') -Encoding utf8
    'initial code' | Set-Content -LiteralPath (Join-Path $seed 'code.txt')
    $null = G $seed @('add','run.ps1','code.txt')
    $null = G $seed @('-c','user.name=BenchTest','-c','user.email=bench@example.test','commit','-m','initial')
    $null = G $seed @('branch','-M','main')
    $null = G $seed @('push','origin','main')

    $cfg = [ordered]@{
        workerId = 'result-sync-test'
        autoUpdate = $false
        experiments = @([ordered]@{
            id = 'bench'; repo = $bare; branch = 'main'
            entrypoint = 'run.ps1'; timeoutSeconds = 20
            runOnChange = $true
            ignorePaths = @('results/runs/','results/logs/')
            resultPaths = @('results/runs/','results/logs/')
        })
    }
    $configFile = Join-Path $root 'config.json'
    $cfg | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $configFile -Encoding utf8
    $poll = Join-Path (Split-Path -Parent $PSScriptRoot) 'poll.ps1'
    $marker = Join-Path $root 'logs/launch-count.txt'

    # 1. First code version is executed once and creates an uncommitted result.
    & $poll -ConfigPath $configFile | Out-Null
    Assert (Test-Path -LiteralPath $marker) 'first run missing'
    Assert (@(Get-Content -LiteralPath $marker).Count -eq 1) 'first run count'
    $checkout = Join-Path $root 'workspaces/bench'
    Assert (Test-Path -LiteralPath (Join-Path $checkout 'results/runs/result-0.txt')) 'first result missing'

    # 2. Pre-poll result sync commits/pushes ONLY result data and never reruns.
    & $poll -ConfigPath $configFile | Out-Null
    Assert (@(Get-Content -LiteralPath $marker).Count -eq 1) 'result-only update caused rerun'
    $pushedResult = G $checkout @('ls-tree','-r','--name-only','origin/main','--','results/runs/')
    Assert (($pushedResult -join '') -match 'result-0.txt') 'results were not published'

    # 3. A new remote *code* commit causes the expected next run.
    $null = G $seed @('fetch','origin','main')
    $null = G $seed @('merge','--ff-only','origin/main')
    'code version 2' | Set-Content -LiteralPath (Join-Path $seed 'code.txt')
    $null = G $seed @('add','code.txt')
    $null = G $seed @('-c','user.name=BenchTest','-c','user.email=bench@example.test','commit','-m','code-v2')
    $null = G $seed @('push','origin','main')
    & $poll -ConfigPath $configFile | Out-Null
    Assert (@(Get-Content -LiteralPath $marker).Count -eq 2) 'new code was not executed'

    # 4. Remote code moves again while local results are dirty.
    #    The extracted sync first commits results, rebases ONLY result commits,
    #    pushes those metrics, and then updates/runs the new code.
    'code version 3' | Set-Content -LiteralPath (Join-Path $seed 'code.txt')
    $null = G $seed @('add','code.txt')
    $null = G $seed @('-c','user.name=BenchTest','-c','user.email=bench@example.test','commit','-m','code-v3')
    $null = G $seed @('push','origin','main')
    & $poll -ConfigPath $configFile | Out-Null
    Assert (@(Get-Content -LiteralPath $marker).Count -eq 3) 'code change + pending result did not run once'
    $pushedResults = @(G $checkout @('ls-tree','-r','--name-only','origin/main','--','results/runs/'))
    Assert (($pushedResults -join ',') -match 'result-1.txt') 'pending result lost during rebase'

    # 5. Results from the third run should publish without a fourth run.
    & $poll -ConfigPath $configFile | Out-Null
    Assert (@(Get-Content -LiteralPath $marker).Count -eq 3) 'results-only commit retriggered benchmark'

    # 6. A local CODE commit is never silently rebased/published.
    'unpublished local code' | Set-Content -LiteralPath (Join-Path $checkout 'code.txt')
    $null = G $checkout @('add','code.txt')
    $null = G $checkout @('-c','user.name=BenchTest','-c','user.email=bench@example.test','commit','-m','local-code')
    $headBefore = (G $checkout @('rev-parse','HEAD') | Select-Object -First 1)
    & $poll -ConfigPath $configFile | Out-Null
    $state = Get-Content -LiteralPath (Join-Path $root 'state/bench.json') -Raw | ConvertFrom-Json
    Assert ($state.status -eq 'error') 'local code commit was not blocked'
    $headAfter = (G $checkout @('rev-parse','HEAD') | Select-Object -First 1)
    Assert ($headBefore -eq $headAfter) 'local code commit was modified'
    Assert (@(Get-Content -LiteralPath $marker).Count -eq 3) 'unsafe local commit caused a run'
    Write-Output 'RESULT SYNC PASS: results-only commits/pushes, remote code/rebase, no false reruns, local code preserved.'
} finally {
    Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
}
