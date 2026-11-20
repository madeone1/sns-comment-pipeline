# Launched by change-access-token.bat. Same entry as the macOS .command file:
# python -m sns_comment.token_cli, standard library only, no virtual environment.
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $Remaining
)

$ErrorActionPreference = 'Continue'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
try { & "$env:SystemRoot\System32\chcp.com" 65001 | Out-Null } catch { }

function Wait-ToClose {
    try {
        if ([Console]::IsInputRedirected) { return }
        Write-Host ''
        Write-Host 'Enter를 누르면 창을 닫습니다.'
        [void][Console]::ReadLine()
    } catch { }
}

function Stop-Launcher {
    param([int] $Code)
    Wait-ToClose
    exit $Code
}

function Find-Python312 {
    $candidates = @(
        @{ Name = 'py'; Prefix = @('-3.12') },
        @{ Name = 'py'; Prefix = @('-3') },
        @{ Name = 'python'; Prefix = @() },
        @{ Name = 'python3'; Prefix = @() }
    )
    foreach ($candidate in $candidates) {
        $command = Get-Command $candidate.Name -ErrorAction SilentlyContinue
        if (-not $command) { continue }
        if ($command.Source -like '*\WindowsApps\*') { continue }
        $check = @($candidate.Prefix) + @('-c', 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)')
        & $command.Source @check 1>$null 2>$null
        if ($LASTEXITCODE -eq 0) {
            return [pscustomobject]@{ Exe = $command.Source; Prefix = [string[]]$candidate.Prefix }
        }
    }
    return $null
}

function Find-Gcloud {
    # Prefer gcloud.cmd. PowerShell's Get-Command gcloud selects gcloud.ps1,
    # and the execution policy can block that script.
    foreach ($name in @('gcloud.cmd', 'gcloud')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if (-not $command) { continue }
        if ($command.Source -like '*\WindowsApps\*') { continue }
        if ($command.Source -like '*.ps1') { continue }
        return $command.Source
    }
    return $null
}

try {
    $python = Find-Python312
    $gcloud = Find-Gcloud
    if (-not $python -or -not $gcloud) {
        if (-not $python) {
            Write-Host 'Python 3.12 이상이 필요합니다.'
            Write-Host '설치 페이지: https://www.python.org/downloads/windows/'
            Write-Host '설치할 때 "Add python.exe to PATH"를 선택해주세요.'
            Write-Host '설치가 끝나면 이 창을 닫고 change-access-token.bat 을 다시 여세요.'
            Write-Host ''
        }
        if (-not $gcloud) {
            Write-Host 'Google Cloud CLI가 설치되어 있지 않습니다.'
            Write-Host '설치 페이지: https://cloud.google.com/sdk/docs/install'
            Write-Host '설치가 끝나면 새로 연 명령 프롬프트에서 회사 계정으로 로그인하세요.'
            Write-Host '  gcloud auth login josh@madeone.kr'
            Write-Host '그다음 change-access-token.bat 을 다시 여세요.'
        }
        Stop-Launcher 1
    }

    $launch = New-Object System.Collections.Generic.List[string]
    foreach ($arg in @($python.Prefix)) {
        if ($arg) { [void]$launch.Add([string]$arg) }
    }
    [void]$launch.Add('-m')
    [void]$launch.Add('sns_comment.token_cli')
    foreach ($arg in @($Remaining)) {
        if ($null -ne $arg -and $arg -ne '') { [void]$launch.Add([string]$arg) }
    }
    & $python.Exe @($launch.ToArray())
    $code = $LASTEXITCODE
    if ($null -eq $code) { $code = 1 }
    Stop-Launcher $code
} catch {
    Write-Host '실행 중 문제가 생겼습니다. 아래 안내를 확인한 뒤 창을 닫아주세요.'
    if ($_.Exception -and $_.Exception.Message) { Write-Host $_.Exception.Message }
    Stop-Launcher 1
}
