# The only PowerShell file: forwards all arguments to the Python implementation.
#   pwsh -File .\poller.ps1 setup
#   pwsh -File .\poller.ps1 launch [--force]
#   pwsh -File .\poller.ps1 check --config .\examples\config.example.json
# Set GIT_POLLER_PYTHON to pick a specific interpreter.
$ErrorActionPreference = 'Stop'
if ($env:GIT_POLLER_PYTHON) {
    $exe = $env:GIT_POLLER_PYTHON; $pre = @()
} elseif ($IsWindows -and (Get-Command py -ErrorAction SilentlyContinue)) {
    $exe = 'py'; $pre = @('-3')
} elseif (Get-Command python3 -ErrorAction SilentlyContinue) {
    $exe = 'python3'; $pre = @()
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $exe = 'python'; $pre = @()
} else {
    Write-Error 'Python 3.10+ is required. Windows: winget install Python.Python.3.12'
    exit 2
}
$sep = [IO.Path]::PathSeparator
$env:PYTHONPATH = if ($env:PYTHONPATH) { "$PSScriptRoot$sep$env:PYTHONPATH" } else { $PSScriptRoot }
& $exe @pre -m git_repo_poller @args
exit $LASTEXITCODE
