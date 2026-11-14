# Interactive, per-user Windows install. Never runs a real experiment during setup.
[CmdletBinding()]
param(
    [string]$InstallRoot = '',
    [switch]$NonInteractive,
    [string]$ConfigPath = '',
    [switch]$AddExperiment,
    [switch]$Repair,
    [switch]$DryRun,
    [switch]$Uninstall,
    [switch]$Pause,
    [switch]$Resume,
    [switch]$RunSmokeTest,
    [switch]$ScheduledTask,
    [switch]$NoScheduledTask
)
$ErrorActionPreference = 'Stop'
if (-not $InstallRoot) {
    $base = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { Join-Path $HOME '.local/share' }
    $InstallRoot = Join-Path $base 'GitRepoPoller'
}
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
$installedConfig = Join-Path $InstallRoot 'config.json'
$launcher = Join-Path $InstallRoot 'launcher.ps1'

function Ask([string]$Prompt, [string]$Default = '') {
    if ($NonInteractive) { return $Default }
    $suffix = if ($Default) { " [$Default]" } else { '' }
    $answer = Read-Host "$Prompt$suffix"
    if ([string]::IsNullOrWhiteSpace($answer)) { return $Default }
    return $answer.Trim()
}
function Ask-Bool([string]$Prompt, [bool]$Default) {
    if ($NonInteractive) { return $Default }
    $suffix = if ($Default) { '[Y/n]' } else { '[y/N]' }
    $a = (Read-Host "$Prompt $suffix").Trim().ToLowerInvariant()
    if (-not $a) { return $Default }
    return $a -in @('y','yes','j','ja')
}
function Read-Configuration([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { throw "Config not found: $Path" }
    return Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
}
function Task-Name([string]$WorkerId) { return "GitRepoPoller-$WorkerId" }
function Ensure-Git {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw 'Git for Windows is required. Install with: winget install Git.Git' }
    if (-not (Get-Command pwsh -ErrorAction SilentlyContinue)) { throw 'PowerShell 7 is required. Install with: winget install Microsoft.PowerShell' }
}
function Add-ExperimentWizard([object]$Settings) {
    if ($NonInteractive) { throw 'For unattended setup, supply experiments in -ConfigPath JSON.' }
    do {
        $id = Ask 'Experiment ID (letters, digits, hyphen or underscore)' 'demo'
        if ($id -notmatch '^[a-zA-Z0-9_-]+$') { Write-Warning 'Invalid experiment ID'; continue }
        if (@($Settings.experiments | Where-Object { $_.id -eq $id }).Count -gt 0) {
            Write-Warning 'An experiment with this ID already exists. Choose another ID.'; continue
        }
        $url = Ask 'Trusted Git repository URL'
        if (-not $url) { Write-Warning 'Git URL cannot be empty'; continue }
        $branch = Ask 'Branch' 'main'
        $isBench = $url -match '/ai-native-code-bench(?:\.git)?
        $seconds = [int](Ask 'Command timeout in seconds' '600')
        $onChange = Ask-Bool 'Run only when Git changes (first check also runs)?' $true
        $resultDefaults = if ($isBench) { 'results/runs/,results/logs/' } else { '' }
        $publish = Ask 'Result directories to sync before polling (comma separated; empty disables all commits/pushes)' $resultDefaults
        $ignore = Ask 'Ignore changes under these prefixes (comma separated)' $resultDefaults
        $profile = [pscustomobject]@{
            id = $id; repo = $url; branch = $branch; entrypoint = $entry
            timeoutSeconds = $seconds; runOnChange = $onChange
            ignorePaths = @($ignore.Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ })
            resultPaths = @($publish.Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ })
        }
        $Settings.experiments = @($Settings.experiments) + @($profile)
        Write-Output "Added $id -> $url"
    } while (Ask-Bool 'Add another experiment?' $false)
}
function Register-PollerTask([object]$Settings) {
    if (-not $IsWindows) { throw 'Windows Task Scheduler is available only on Windows' }
    $name = Task-Name ([string]$Settings.workerId)
    $psExe = (Get-Command pwsh -ErrorAction Stop).Source
    $args = '-NoProfile -NonInteractive -File "' + $launcher + '"'
    $action = New-ScheduledTaskAction -Execute $psExe -Argument $args -WorkingDirectory $InstallRoot
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes ([Math]::Max(1,[int]$Settings.intervalMinutes)))
    $principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) `
        -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Hours 12) -StartWhenAvailable
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Force | Out-Null
    Write-Output "Scheduled task registered (current user): $name"
}

if ($Pause -or $Resume) {
    if ($Pause) {
        New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
        Set-Content -LiteralPath (Join-Path $InstallRoot 'paused.flag') -Value 'paused' -Encoding ascii
        Write-Output 'Poller paused.'
    } else {
        Remove-Item -LiteralPath (Join-Path $InstallRoot 'paused.flag') -Force -ErrorAction SilentlyContinue
        Write-Output 'Poller resumed.'
    }
    return
}
if ($Uninstall) {
    if (Test-Path -LiteralPath $installedConfig) {
        $cfg = Read-Configuration $installedConfig
        if ($IsWindows) {
            Unregister-ScheduledTask -TaskName (Task-Name $cfg.workerId) -Confirm:$false -ErrorAction SilentlyContinue
        }
    }
    Remove-Item -LiteralPath $launcher,$installedConfig -Force -ErrorAction SilentlyContinue
    Write-Output "Uninstalled launcher/config. Repositories, results, logs and engine kept at $InstallRoot."
    return
}
Ensure-Git
if ($ConfigPath) {
    $config = Read-Configuration $ConfigPath
} elseif (Test-Path -LiteralPath $installedConfig) {
    $config = Read-Configuration $installedConfig
} else {
    if ($NonInteractive) { throw 'Provide -ConfigPath with -NonInteractive.' }
    $workerId = Ask 'Worker ID (e.g. home-gpu)' ([Environment]::MachineName.ToLowerInvariant())
    $interval = [int](Ask 'Polling interval (minutes)' '5')
    $update = Ask-Bool 'Automatically update the poller from its own Git repository?' $true
    $config = [pscustomobject]@{
        workerId = $workerId; intervalMinutes = $interval; autoUpdate = $update
        engineRemote = 'https://github.com/tire0011/git-repo-poller.git'
        engineBranch = 'main'; experiments = @()
    }
    Add-ExperimentWizard $config
}
if ($AddExperiment) { Add-ExperimentWizard $config }
if ($config.workerId -notmatch '^[a-zA-Z0-9_-]+$') { throw 'Invalid worker ID' }
if (-not $config.engineRemote -or -not $config.engineBranch) { throw 'Missing engineRemote or engineBranch' }
foreach ($job in @($config.experiments)) {
    if ($job.id -notmatch '^[a-zA-Z0-9_-]+$' -or -not $job.repo -or -not $job.branch -or -not $job.entrypoint) {
        throw "Incomplete/invalid experiment profile: $($job.id)"
    }
}
Write-Output "Install folder: $InstallRoot"
Write-Output "Worker: $($config.workerId); experiments: $(@($config.experiments).Count)"
foreach ($job in @($config.experiments)) { Write-Output "  $($job.id): $($job.repo) [$($job.branch)] -> $($job.entrypoint)" }
if ($DryRun) {
    Write-Output 'DRY RUN: configuration checked; no clone, execution, scheduled task or writes performed.'
    return
}

New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
$engine = Join-Path $InstallRoot 'engine'
if (Test-Path -LiteralPath (Join-Path $engine '.git')) {
    $actual = (& git -C $engine remote get-url origin 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $actual -ne [string]$config.engineRemote) { throw 'Existing engine has a different Git remote.' }
    $dirty = @(& git -C $engine status --porcelain --untracked-files=all)
    if (@($dirty | Where-Object { $_ }).Count -gt 0) { throw 'Engine clone is dirty; refusing to update.' }
} else {
    if (Test-Path -LiteralPath $engine) { throw 'Engine directory exists but is not a clone.' }
    & git clone --single-branch --branch ([string]$config.engineBranch) -- ([string]$config.engineRemote) $engine
    if ($LASTEXITCODE -ne 0) { throw 'Engine clone failed; check Git credentials/network.' }
}
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'launcher.ps1') -Destination $launcher -Force
$config | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $installedConfig -Encoding utf8
if ($ScheduledTask -or ((-not $NoScheduledTask) -and (Ask-Bool 'Register current-user Windows scheduled task?' $false))) {
    Register-PollerTask $config
}
Write-Output "Installed. Config: $installedConfig"
Write-Output "Dry check: pwsh -File `"$launcher`" -CheckOnly"
Write-Output "Run once:  pwsh -File `"$launcher`""
Write-Output "Pause:     pwsh -File `"$PSScriptRoot/setup.ps1`" -InstallRoot `"$InstallRoot`" -Pause"
Write-Output "Uninstall: pwsh -File `"$PSScriptRoot/setup.ps1`" -InstallRoot `"$InstallRoot`" -Uninstall"
if ($RunSmokeTest) {
    & (Join-Path $PSScriptRoot 'tests/Smoke.Tests.ps1')
    if (-not $?) { throw 'Synthetic smoke test failed' }
}

        $entry = Ask 'Entrypoint inside the repo' $(if ($isBench) { 'poller-run.ps1' } else { 'run.ps1' })
        $seconds = [int](Ask 'Command timeout in seconds' '600')
        $onChange = Ask-Bool 'Run only when Git changes (first check also runs)?' $true
        $ignore = Ask 'Ignore changes under these prefixes (comma separated; e.g. results/,logs/)' ''
        $profile = [pscustomobject]@{
            id = $id; repo = $url; branch = $branch; entrypoint = $entry
            timeoutSeconds = $seconds; runOnChange = $onChange
            ignorePaths = @($ignore.Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ })
        }
        $Settings.experiments = @($Settings.experiments) + @($profile)
        Write-Output "Added $id -> $url"
    } while (Ask-Bool 'Add another experiment?' $false)
}
function Register-PollerTask([object]$Settings) {
    if (-not $IsWindows) { throw 'Windows Task Scheduler is available only on Windows' }
    $name = Task-Name ([string]$Settings.workerId)
    $psExe = (Get-Command pwsh -ErrorAction Stop).Source
    $args = '-NoProfile -NonInteractive -File "' + $launcher + '"'
    $action = New-ScheduledTaskAction -Execute $psExe -Argument $args -WorkingDirectory $InstallRoot
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes ([Math]::Max(1,[int]$Settings.intervalMinutes)))
    $principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) `
        -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Hours 12) -StartWhenAvailable
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Force | Out-Null
    Write-Output "Scheduled task registered (current user): $name"
}

if ($Pause -or $Resume) {
    if ($Pause) {
        New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
        Set-Content -LiteralPath (Join-Path $InstallRoot 'paused.flag') -Value 'paused' -Encoding ascii
        Write-Output 'Poller paused.'
    } else {
        Remove-Item -LiteralPath (Join-Path $InstallRoot 'paused.flag') -Force -ErrorAction SilentlyContinue
        Write-Output 'Poller resumed.'
    }
    return
}
if ($Uninstall) {
    if (Test-Path -LiteralPath $installedConfig) {
        $cfg = Read-Configuration $installedConfig
        if ($IsWindows) {
            Unregister-ScheduledTask -TaskName (Task-Name $cfg.workerId) -Confirm:$false -ErrorAction SilentlyContinue
        }
    }
    Remove-Item -LiteralPath $launcher,$installedConfig -Force -ErrorAction SilentlyContinue
    Write-Output "Uninstalled launcher/config. Repositories, results, logs and engine kept at $InstallRoot."
    return
}
Ensure-Git
if ($ConfigPath) {
    $config = Read-Configuration $ConfigPath
} elseif (Test-Path -LiteralPath $installedConfig) {
    $config = Read-Configuration $installedConfig
} else {
    if ($NonInteractive) { throw 'Provide -ConfigPath with -NonInteractive.' }
    $workerId = Ask 'Worker ID (e.g. home-gpu)' ([Environment]::MachineName.ToLowerInvariant())
    $interval = [int](Ask 'Polling interval (minutes)' '5')
    $update = Ask-Bool 'Automatically update the poller from its own Git repository?' $true
    $config = [pscustomobject]@{
        workerId = $workerId; intervalMinutes = $interval; autoUpdate = $update
        engineRemote = 'https://github.com/tire0011/git-repo-poller.git'
        engineBranch = 'main'; experiments = @()
    }
    Add-ExperimentWizard $config
}
if ($AddExperiment) { Add-ExperimentWizard $config }
if ($config.workerId -notmatch '^[a-zA-Z0-9_-]+$') { throw 'Invalid worker ID' }
if (-not $config.engineRemote -or -not $config.engineBranch) { throw 'Missing engineRemote or engineBranch' }
foreach ($job in @($config.experiments)) {
    if ($job.id -notmatch '^[a-zA-Z0-9_-]+$' -or -not $job.repo -or -not $job.branch -or -not $job.entrypoint) {
        throw "Incomplete/invalid experiment profile: $($job.id)"
    }
}
Write-Output "Install folder: $InstallRoot"
Write-Output "Worker: $($config.workerId); experiments: $(@($config.experiments).Count)"
foreach ($job in @($config.experiments)) { Write-Output "  $($job.id): $($job.repo) [$($job.branch)] -> $($job.entrypoint)" }
if ($DryRun) {
    Write-Output 'DRY RUN: configuration checked; no clone, execution, scheduled task or writes performed.'
    return
}

New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
$engine = Join-Path $InstallRoot 'engine'
if (Test-Path -LiteralPath (Join-Path $engine '.git')) {
    $actual = (& git -C $engine remote get-url origin 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $actual -ne [string]$config.engineRemote) { throw 'Existing engine has a different Git remote.' }
    $dirty = @(& git -C $engine status --porcelain --untracked-files=all)
    if (@($dirty | Where-Object { $_ }).Count -gt 0) { throw 'Engine clone is dirty; refusing to update.' }
} else {
    if (Test-Path -LiteralPath $engine) { throw 'Engine directory exists but is not a clone.' }
    & git clone --single-branch --branch ([string]$config.engineBranch) -- ([string]$config.engineRemote) $engine
    if ($LASTEXITCODE -ne 0) { throw 'Engine clone failed; check Git credentials/network.' }
}
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'launcher.ps1') -Destination $launcher -Force
$config | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $installedConfig -Encoding utf8
if ($ScheduledTask -or ((-not $NoScheduledTask) -and (Ask-Bool 'Register current-user Windows scheduled task?' $false))) {
    Register-PollerTask $config
}
Write-Output "Installed. Config: $installedConfig"
Write-Output "Dry check: pwsh -File `"$launcher`" -CheckOnly"
Write-Output "Run once:  pwsh -File `"$launcher`""
Write-Output "Pause:     pwsh -File `"$PSScriptRoot/setup.ps1`" -InstallRoot `"$InstallRoot`" -Pause"
Write-Output "Uninstall: pwsh -File `"$PSScriptRoot/setup.ps1`" -InstallRoot `"$InstallRoot`" -Uninstall"
if ($RunSmokeTest) {
    & (Join-Path $PSScriptRoot 'tests/Smoke.Tests.ps1')
    if (-not $?) { throw 'Synthetic smoke test failed' }
}
