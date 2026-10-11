<#
.SYNOPSIS
    scripts/claude_mentions.ps1 を1分ごとに -Apply 付きで走らせるタスクを、タスクスケジューラに登録する。

.DESCRIPTION
    何度実行しても同じ結果になる（既にあれば上書きする）。-Unregister で外す。
    タスクが指すのは main の作業ツリーのスクリプト（worktree から実行しても同じ）。
    U: のような割り当てたドライブはタスクから見えないことがあるので、\\wsl.localhost\... のパスで登録する。
    conhost --headless で起動し、1分ごとにウィンドウが開かないようにする。
    前の回が Claude の対応中なら、次の回は起動しない（MultipleInstances IgnoreNew。対応は1件ずつ）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\register_claude_mentions_task.ps1
    powershell -ExecutionPolicy Bypass -File scripts\register_claude_mentions_task.ps1 -Unregister
#>
param([switch]$Unregister)

$TaskName = 'baseball-league-manager claude mentions'

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output "外した: $TaskName"
    return
}

$mainPath = (git -C $PSScriptRoot worktree list --porcelain | Select-Object -First 1).Substring(9)
$script = ($mainPath -replace '/', '\') + '\scripts\claude_mentions.ps1'
if (-not (Test-Path $script)) {
    Write-Error "main の作業ツリーに $script が無い（main へマージされてから登録する）"
    exit 1
}

$action = New-ScheduledTaskAction -Execute 'conhost.exe' `
    -Argument "--headless powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$script`" -Apply"
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
# 制限時間は、スクリプト側の1件あたりの打ち切り（120分）より長くする
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 6) -MultipleInstances IgnoreNew
# ログオン中だけ動かす（WSL と Docker Desktop はログオンしたユーザーのセッションで動いている）
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description 'GitHub で @claude とメンションしたコメントを見回り、手元の Claude Code に対応させる（scripts/claude_mentions.ps1）' `
    -Force | Out-Null
Write-Output "登録した: $TaskName（1分ごと） -> $script"
Write-Output "ログ: $env:LOCALAPPDATA\baseball-claude-mentions\mentions.log"
