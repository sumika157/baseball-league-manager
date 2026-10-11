<#
.SYNOPSIS
    scripts/cleanup.ps1 を1時間ごとに -Apply 付きで走らせるタスクを、タスクスケジューラに登録する。

.DESCRIPTION
    何度実行しても同じ結果になる（既にあれば上書きする）。-Unregister で外す。
    タスクが指すのは main の作業ツリーのスクリプト（worktree から実行しても同じ）。
    U: のような割り当てたドライブはタスクから見えないことがあるので、\\wsl.localhost\... のパスで登録する。
    conhost --headless で起動し、1時間ごとにウィンドウが開かないようにする。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\register_cleanup_task.ps1
    powershell -ExecutionPolicy Bypass -File scripts\register_cleanup_task.ps1 -Unregister
#>
param([switch]$Unregister)

$TaskName = 'baseball-league-manager cleanup'

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output "外した: $TaskName"
    return
}

$mainPath = (git -C $PSScriptRoot worktree list --porcelain | Select-Object -First 1).Substring(9)
$script = ($mainPath -replace '/', '\') + '\scripts\cleanup.ps1'
if (-not (Test-Path $script)) {
    Write-Error "main の作業ツリーに $script が無い（main へマージされてから登録する）"
    exit 1
}

$action = New-ScheduledTaskAction -Execute 'conhost.exe' `
    -Argument "--headless powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$script`" -Apply"
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Hours 1)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -MultipleInstances IgnoreNew
# ログオン中だけ動かす（WSL と Docker Desktop はログオンしたユーザーのセッションで動いている）
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description 'マージ済みの worktree と、置き去りになったテスト・e2e のプロセス（自動操作用の Chrome を含む）を片付ける（scripts/cleanup.ps1）' `
    -Force | Out-Null
Write-Output "登録した: $TaskName（1時間ごと） -> $script"
Write-Output "ログ: $env:LOCALAPPDATA\baseball-cleanup\cleanup.log"
