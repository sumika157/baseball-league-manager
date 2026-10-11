<#
.SYNOPSIS
    マージ済みの worktree と、置き去りになったテスト・e2e のプロセス（自動操作用の Chrome を含む）を片付ける。

.DESCRIPTION
    既定は対象を表示するだけ。-Apply を付けると実際に消し、消したものを
    %LOCALAPPDATA%\baseball-cleanup\cleanup.log に残す。
    タスクスケジューラが1時間ごとに -Apply 付きで呼ぶ（登録は scripts/register_cleanup_task.ps1）。

    消すのは次の条件をすべて満たす worktree だけ（疑わしいものは残して表示する）:
      - .claude/worktrees/ の下にあり、epic/ ブランチではなく、ロックされていない
      - 未コミットの変更が無い
      - ブランチの PR がマージ済みかクローズ済みで、開いている PR が無い
      - worktree の HEAD が PR の先端と一致する（マージ後に積んだコミットが無い）
    あわせて、登録の外れた残骸ディレクトリ（更新から -OrphanDirAgeHours を超えたもの）と、
    今ある worktree に対応しない safe.directory の登録を外す。

    プロセスは、起動から -MaxProcessAgeHours を超えた次のものを、子プロセスごと止める:
      - web・frontend コンテナで docker exec から始めたテスト・検査・e2e（scripts/reap_stale_processes.sh）
      - Windows 側の自動操作用の Chrome（headless・リモートデバッグ・Playwright や Puppeteer が起動したもの）。
        普段使いの Chrome はこれらの起動オプションを持たないので対象にならない

    gh と、worktree の中で動く git が Windows 側にしか無いので PowerShell で書いている。
    コンテナ（root）が作った生成物は Windows からは消せないので、ディレクトリはコンテナ経由で消す。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\cleanup.ps1          # 対象を表示するだけ
    powershell -ExecutionPolicy Bypass -File scripts\cleanup.ps1 -Apply   # 実際に消す
#>
param(
    [switch]$Apply,
    [int]$MaxProcessAgeHours = 3,
    [int]$OrphanDirAgeHours = 24
)

$ErrorActionPreference = 'Continue'
$WebContainer = 'baseball-web'
$ProcessContainers = @('baseball-web', 'baseball-frontend')
$LogFile = Join-Path $env:LOCALAPPDATA 'baseball-cleanup\cleanup.log'

function Write-Log([string]$Message) {
    $line = '{0:yyyy-MM-dd HH:mm:ss} {1}{2}' -f (Get-Date), $(if ($Apply) { '' } else { '[確認のみ] ' }), $Message
    Write-Output $line
    if (-not $Apply) { return }
    $dir = Split-Path $LogFile
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
    # 1MB を超えたら1世代だけ残して切り替える
    if ((Test-Path $LogFile) -and (Get-Item $LogFile).Length -gt 1MB) {
        Move-Item -Path $LogFile -Destination "$LogFile.old" -Force
    }
    $line | Out-File -FilePath $LogFile -Append -Encoding utf8
}

function Find-Gh {
    $cmd = Get-Command gh -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $path = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages\GitHub.cli_Microsoft.Winget.Source_8wekyb3d8bbwe\bin\gh.exe'
    if (Test-Path $path) { return $path }
    return $null
}

function Test-ContainerRunning([string]$Name) {
    $state = docker inspect -f '{{.State.Running}}' $Name 2>$null
    return ($LASTEXITCODE -eq 0 -and $state -eq 'true')
}

# git の表記（//wsl.localhost/...）を Windows のパス（\\wsl.localhost\...）にする
function ConvertTo-WinPath([string]$Path) { return $Path -replace '/', '\' }

# ---- 作業ツリーの一覧 -------------------------------------------------------------

$porcelain = git -C $PSScriptRoot worktree list --porcelain 2>$null
if ($LASTEXITCODE -ne 0 -or -not $porcelain) {
    Write-Log "git worktree list が失敗したので中止した（$PSScriptRoot）"
    exit 1
}

$worktrees = @()
$current = $null
foreach ($line in $porcelain) {
    if ($line -like 'worktree *') {
        $current = [ordered]@{ Path = $line.Substring(9); Head = $null; Branch = $null; Locked = $false; Prunable = $false }
        $worktrees += $current
    } elseif ($line -like 'HEAD *') {
        $current.Head = $line.Substring(5)
    } elseif ($line -like 'branch refs/heads/*') {
        $current.Branch = $line.Substring(18)
    } elseif ($line -like 'locked*') {
        $current.Locked = $true
    } elseif ($line -like 'prunable*') {
        $current.Prunable = $true
    }
}

# 1件目が main の作業ツリー
$mainPath = $worktrees[0].Path
$worktreesRoot = "$mainPath/.claude/worktrees/"
$repo = (git -C $mainPath remote get-url origin) -replace '^.*github\.com[:/]', '' -replace '\.git$', ''
$gh = Find-Gh
$webRunning = Test-ContainerRunning $WebContainer

# コンテナ（root）が作った生成物ごと消せるよう、ディレクトリはコンテナ経由で消す
function Remove-WorktreeDir([string]$Name) {
    if ($Name -notmatch '^[A-Za-z0-9._-]+$') { return $false }
    if (-not $webRunning) { return $false }
    docker exec $WebContainer rm -rf "/app/.claude/worktrees/$Name" 2>$null
    return ($LASTEXITCODE -eq 0)
}

# ---- worktree ---------------------------------------------------------------------

foreach ($wt in $worktrees) {
    if (-not $wt.Path.StartsWith($worktreesRoot)) { continue }
    $name = $wt.Path.Substring($worktreesRoot.Length)
    $label = "worktree $name（$($wt.Branch)）"

    if ($wt.Prunable) { continue }  # ディレクトリが既に無い。下の git worktree prune で登録が外れる
    if (-not $wt.Branch) { Write-Output "残す: $label ブランチが無い（detached）"; continue }
    if ($wt.Branch -like 'epic/*') { Write-Output "残す: $label 統合ブランチ"; continue }
    if ($wt.Locked) { Write-Output "残す: $label ロックされている"; continue }

    $dirty = git -C $wt.Path status --porcelain 2>$null
    if ($LASTEXITCODE -ne 0) { Write-Log "残す: $label git status が失敗した（safe.directory の登録漏れなど）"; continue }
    if ($dirty) { Write-Output "残す: $label 未コミットの変更がある"; continue }

    if (-not $gh) { Write-Log "残す: $label gh が見つからない"; continue }
    $json = & $gh pr list -R $repo --head $wt.Branch --state all --json 'number,state,headRefOid' 2>$null
    if ($LASTEXITCODE -ne 0) { Write-Log "残す: $label gh pr list が失敗した"; continue }
    # PowerShell 5.1 の ConvertFrom-Json は配列を1つのオブジェクトとして流すので、ForEach-Object で展開する
    $prs = @(($json -join "`n") | ConvertFrom-Json | ForEach-Object { $_ })
    if ($prs | Where-Object { $_.state -eq 'OPEN' }) { Write-Output "残す: $label 開いている PR がある"; continue }
    $done = $prs | Where-Object { $_.state -in @('MERGED', 'CLOSED') -and $_.headRefOid -eq $wt.Head } | Select-Object -First 1
    if (-not $done) {
        if ($prs.Count -eq 0) { Write-Output "残す: $label PR が無い" }
        else { Write-Output "残す: $label PR の後に積んだコミットがある" }
        continue
    }

    if (-not $Apply) { Write-Log "消す: $label PR #$($done.number) $($done.state)"; continue }
    if (Remove-WorktreeDir $name) {
        git -C $mainPath worktree prune 2>$null
        git -C $mainPath branch -D $wt.Branch 2>$null | Out-Null
        Write-Log "消した: $label PR #$($done.number) $($done.state)"
    } elseif (-not $webRunning) {
        # コンテナが止まっていれば git に任せる。生成物が残っていれば失敗するので、次の回に回す
        git -C $mainPath worktree remove $wt.Path 2>$null
        if ($LASTEXITCODE -eq 0) {
            git -C $mainPath branch -D $wt.Branch 2>$null | Out-Null
            Write-Log "消した: $label PR #$($done.number) $($done.state)"
        } else {
            Write-Log "消せなかった: $label web コンテナが止まっていて、生成物が残っている"
        }
    } else {
        Write-Log "消せなかった: $label コンテナ経由の削除が失敗した"
    }
}
git -C $mainPath worktree prune 2>$null

# ---- 登録の外れた残骸ディレクトリ ---------------------------------------------------
# git worktree remove が登録だけ外して中身を残すことがある（frontend-build 後など）。
# 中身が push 済みかは確かめようがないので、しばらく触られていないものだけ消す。

$registered = @(git -C $mainPath worktree list --porcelain 2>$null | Where-Object { $_ -like 'worktree *' } | ForEach-Object { $_.Substring(9) })
$rootWin = ConvertTo-WinPath $worktreesRoot
if (Test-Path $rootWin) {
    foreach ($dir in Get-ChildItem -Path $rootWin -Directory) {
        if ($registered -contains "$worktreesRoot$($dir.Name)") { continue }
        if ($dir.LastWriteTime -gt (Get-Date).AddHours(-$OrphanDirAgeHours)) {
            Write-Output "残す: 登録の無いディレクトリ $($dir.Name) 更新から $OrphanDirAgeHours 時間たっていない"
            continue
        }
        if (-not $Apply) { Write-Log "消す: 登録の無いディレクトリ $($dir.Name)"; continue }
        if (Remove-WorktreeDir $dir.Name) { Write-Log "消した: 登録の無いディレクトリ $($dir.Name)" }
        else { Write-Log "消せなかった: 登録の無いディレクトリ $($dir.Name)" }
    }
}

# ---- safe.directory ---------------------------------------------------------------
# worktree ごとに1行登録している。消した worktree の行が残り続けるので、ディレクトリの無いものを外す。

$prefix = "%(prefix)/$worktreesRoot"
$stale = @(git config --global --get-all safe.directory 2>$null |
    # git の一覧にもディスクにも無いものだけ外す（WSL に一時的に届かず Test-Path が偽になっても、
    # 作業中の worktree の登録は外さない）
    Where-Object {
        $_.StartsWith($prefix) -and
        $registered -notcontains $_.Substring(10) -and
        -not (Test-Path (ConvertTo-WinPath $_.Substring(10)))
    } |
    Sort-Object -Unique)
if ($stale.Count -gt 0) {
    if ($Apply) {
        foreach ($entry in $stale) { git config --global --fixed-value --unset-all safe.directory $entry 2>$null }
    }
    Write-Log "safe.directory: 無くなった worktree の登録 $($stale.Count) 件を外す"
}

# ---- コンテナ内の置き去りのプロセス --------------------------------------------------

$maxAgeSeconds = $MaxProcessAgeHours * 3600
$toplevel = git -C $PSScriptRoot rev-parse --show-toplevel 2>$null
# このスクリプトのある作業ツリー（worktree から試すときはその worktree）の版を使う。表記がずれたら main の版
$relative = if ($toplevel -and $toplevel.StartsWith($mainPath)) { $toplevel.Substring($mainPath.Length) } else { '' }
$reaper = "/app$relative/scripts/reap_stale_processes.sh"
foreach ($container in $ProcessContainers) {
    if (-not (Test-ContainerRunning $container)) { continue }
    $args_ = @('exec', $container, 'sh', $reaper, "$maxAgeSeconds")
    if ($Apply) { $args_ += 'apply' }
    $out = & docker @args_ 2>&1
    if ($LASTEXITCODE -ne 0) { Write-Log "${container}: プロセスの確認が失敗した: $out"; continue }
    foreach ($line in @($out | Where-Object { $_ })) {
        $verb = if ($Apply) { '止めた' } else { '止める' }
        Write-Log "${verb}: $container の置き去りのプロセス（pid 経過秒 コマンド）: $line"
    }
}

# ---- Windows 側の自動操作用の Chrome -------------------------------------------------
# e2e や MCP などの自動操作が起動した Chrome は、呼び出し元が落ちると残り続ける。
# ブラウザ本体（--type= の無いプロセス）の起動オプションで見分け、レンダラなどの子ごと止める。

$automationPattern = '--headless|--remote-debugging-(port|pipe)|--enable-automation|ms-playwright|playwright_|puppeteer'
$deadline = (Get-Date).AddHours(-$MaxProcessAgeHours)
$browsers = @(Get-CimInstance Win32_Process -Filter "Name='chrome.exe' or Name='chromium.exe' or Name='chrome-headless-shell.exe'" -ErrorAction SilentlyContinue |
    Where-Object {
        $_.CommandLine -and $_.CommandLine -notmatch '--type=' -and
        $_.CommandLine -match $automationPattern -and $_.CreationDate -lt $deadline
    })
foreach ($p in $browsers) {
    $hours = [int]((Get-Date) - $p.CreationDate).TotalHours
    $cmd = $p.CommandLine.Substring(0, [Math]::Min(200, $p.CommandLine.Length))
    if (-not $Apply) { Write-Log "止める: 自動操作用の Chrome pid=$($p.ProcessId)（$hours 時間）: $cmd"; continue }
    taskkill /PID $p.ProcessId /T /F 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-Log "止めた: 自動操作用の Chrome pid=$($p.ProcessId)（$hours 時間）: $cmd" }
    else { Write-Log "止められなかった: 自動操作用の Chrome pid=$($p.ProcessId): $cmd" }
}
