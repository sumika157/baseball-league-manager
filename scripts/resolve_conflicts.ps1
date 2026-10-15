<#
.SYNOPSIS
    base とコンフリクトした PR を探し、手元の Claude Code（claude -p）に1本ずつ直させる（#267）。

.DESCRIPTION
    既定は対象を表示するだけ。-Apply を付けると、対象の最初の1本について claude -p を切り離して起動する
    （このスクリプトは終わりを待たない。scripts/cleanup.ps1 が1時間ごとに最後に呼ぶ）。
    直し方は .claude/skills/resolve-conflicts/SKILL.md（rebase と force push はしない。判断が要るものは PR にコメントして止める）。

    起動する条件:
      - 開いている、draft でない、同じリポジトリのブランチの PR で、mergeable が CONFLICTING
      - 動いている解消のエージェントが無い（lock.json の PID が生きていれば起動しない。-TimeoutMinutes を超えたものは止める）
      - 前に試したときと、PR の先端（headRefOid）か base の先端が変わっている（止まった PR を同じ形で試し直さない）

    状態とログは %LOCALAPPDATA%\baseball-conflicts\ に置く:
      lock.json（動いているエージェント）・state.json（PR ごとに試したときの先端）・conflicts.log・runs\（1回ごとの Claude の出力。30日で消す）

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\resolve_conflicts.ps1          # 対象を表示するだけ
    powershell -ExecutionPolicy Bypass -File scripts\resolve_conflicts.ps1 -Apply   # 1本だけ直させる
#>
param(
    [switch]$Apply,
    [int]$TimeoutMinutes = 90
)

$ErrorActionPreference = 'Continue'
# gh の出力（PR のタイトル）を UTF-8 として読む
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$StateDir = Join-Path $env:LOCALAPPDATA 'baseball-conflicts'
$RunsDir = Join-Path $StateDir 'runs'
$LogFile = Join-Path $StateDir 'conflicts.log'
$LockFile = Join-Path $StateDir 'lock.json'
$StateFile = Join-Path $StateDir 'state.json'

function Write-Log([string]$Message) {
    $line = '{0:yyyy-MM-dd HH:mm:ss} {1}{2}' -f (Get-Date), $(if ($Apply) { '' } else { '[確認のみ] ' }), $Message
    Write-Output $line
    if (-not $Apply) { return }
    if (-not (Test-Path $StateDir)) { New-Item -ItemType Directory -Path $StateDir | Out-Null }
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

# スタンドアロン版を優先し、無ければ VSCode 拡張に同梱の最新版を使う（scripts/claude_mentions.ps1 と同じ）
function Find-Claude {
    $path = Join-Path $env:USERPROFILE '.local\bin\claude.exe'
    if (Test-Path $path) { return $path }
    $bundled = Get-ChildItem (Join-Path $env:USERPROFILE '.vscode\extensions') -Directory -Filter 'anthropic.claude-code-*' -ErrorAction SilentlyContinue |
        ForEach-Object { Join-Path $_.FullName 'resources\native-binary\claude.exe' } |
        Where-Object { Test-Path $_ } |
        Sort-Object { [version](($_ -replace '^.*anthropic\.claude-code-([\d.]+)-.*$', '$1')) } -Descending |
        Select-Object -First 1
    return $bundled
}

function Read-Json([string]$Path) {
    if (-not (Test-Path $Path)) { return $null }
    try { return Get-Content $Path -Raw -Encoding utf8 | ConvertFrom-Json } catch { return $null }
}

function Write-Json([string]$Path, $Value) {
    if (-not (Test-Path $StateDir)) { New-Item -ItemType Directory -Path $StateDir | Out-Null }
    [IO.File]::WriteAllText($Path, ($Value | ConvertTo-Json -Depth 5), (New-Object Text.UTF8Encoding($false)))
}

# ---- 動いているエージェント ----------------------------------------------------------

$lock = Read-Json $LockFile
if ($lock) {
    $proc = Get-Process -Id $lock.pid -ErrorAction SilentlyContinue
    if ($proc -and $proc.ProcessName -like 'claude*') {
        $minutes = [int]((Get-Date) - [datetime]$lock.started).TotalMinutes
        if ($minutes -lt $TimeoutMinutes) {
            Write-Output "動いている解消のエージェントがあるので起動しない: PR #$($lock.pr)（$minutes 分）"
            exit 0
        }
        if ($Apply) { taskkill /PID $lock.pid /T /F 2>$null | Out-Null }
        Write-Log "$TimeoutMinutes 分で終わらなかったので止めた: PR #$($lock.pr) ログ: $($lock.log)"
    }
    if ($Apply) { Remove-Item $LockFile -Force -ErrorAction SilentlyContinue }
}

# ---- 対象を集める -------------------------------------------------------------------

$gh = Find-Gh
if (-not $gh) { Write-Log 'gh が見つからないので中止した'; exit 1 }

$porcelain = git -C $PSScriptRoot worktree list --porcelain 2>$null
if ($LASTEXITCODE -ne 0 -or -not $porcelain) { Write-Log "git worktree list が失敗したので中止した（$PSScriptRoot）"; exit 1 }
$mainPath = ($porcelain | Select-Object -First 1).Substring(9)
$repo = (git -C $mainPath remote get-url origin) -replace '^.*github\.com[:/]', '' -replace '\.git$', ''

$json = & $gh pr list -R $repo --state open --limit 100 --json 'number,title,isDraft,isCrossRepository,mergeable,headRefName,headRefOid,baseRefName' 2>$null
if ($LASTEXITCODE -ne 0) { Write-Log 'gh pr list が失敗したので中止した'; exit 1 }
# PowerShell 5.1 の ConvertFrom-Json は配列を1つのオブジェクトとして流すので、ForEach-Object で展開する
$prs = @(($json -join "`n") | ConvertFrom-Json | ForEach-Object { $_ })
$conflicting = @($prs | Where-Object { $_.mergeable -eq 'CONFLICTING' -and -not $_.isDraft -and -not $_.isCrossRepository })
if ($conflicting.Count -eq 0) { Write-Output 'コンフリクトした PR は無い'; exit 0 }

$state = Read-Json $StateFile
$tried = @{}
if ($state) { foreach ($p in $state.PSObject.Properties) { $tried[$p.Name] = $p.Value } }

$target = $null
foreach ($pr in $conflicting) {
    $baseSha = (& $gh api "repos/$repo/branches/$($pr.baseRefName)" --jq .commit.sha 2>$null)
    $key = "$($pr.number)"
    $seen = $tried[$key]
    if ($seen -and $seen.head -eq $pr.headRefOid -and $seen.base -eq $baseSha) {
        Write-Output "試し直さない: PR #$($pr.number)（前に試したときから先端も base も動いていない。PR のコメントを見る）"
        continue
    }
    $target = $pr
    $target | Add-Member -NotePropertyName baseSha -NotePropertyValue $baseSha
    break
}
if (-not $target) { exit 0 }

$label = "PR #$($target.number) $($target.baseRefName) <- $($target.headRefName)「$($target.title)」"
if (-not $Apply) { Write-Log "直させる: $label"; exit 0 }

# ---- 起動する（切り離して、終わりを待たない） -------------------------------------------

$claude = Find-Claude
if (-not $claude) { Write-Log 'claude が見つからないので中止した'; exit 1 }

# 対話中のセッションと同じ U: のパスで起動する（Claude のプロジェクトの記憶と許可の設定がパスごとに分かれるため）
$workDir = $mainPath -replace '/', '\'
$drive = Get-PSDrive -PSProvider FileSystem -ErrorAction SilentlyContinue |
    Where-Object { $_.DisplayRoot -and $workDir.StartsWith($_.DisplayRoot, [StringComparison]::OrdinalIgnoreCase) } |
    Select-Object -First 1
if ($drive -and (Test-Path $drive.Root)) { $workDir = $drive.Root.TrimEnd('\') + $workDir.Substring($drive.DisplayRoot.Length) }

if (-not (Test-Path $RunsDir)) { New-Item -ItemType Directory -Path $RunsDir -Force | Out-Null }
Get-ChildItem $RunsDir -File -ErrorAction SilentlyContinue | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } | Remove-Item -Force

$prompt = @"
GitHub（$repo）の PR #$($target.number)（$($target.baseRefName) <- $($target.headRefName)）が base とコンフリクトしています。
手元で自動で起動されたヘッドレスの実行で、途中でユーザーに質問はできません。

resolve-conflicts スキルの手順どおりに直してください。
- rebase と force push はしない。持ち主のセッションの worktree には触らない（一時的な作業場所 .claude/worktrees/conflict-$($target.number) を使う）
- 判断が要るコンフリクトは推測で直さず、取り込みをやめて PR にコメントで状況を書いて止める
- 止めたときも、一時的な作業場所は必ず片付ける
- 共有のコンテナを再起動・作り直ししない（ほかのセッションのテストが落ちる）
- GitHub に書くものには「@claude」をコードの外に書かない
"@

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$base = Join-Path $RunsDir "$stamp-pr$($target.number)"
[IO.File]::WriteAllText("$base.prompt.txt", $prompt, (New-Object Text.UTF8Encoding($false)))

$proc = Start-Process -FilePath $claude -WorkingDirectory $workDir -WindowStyle Hidden -PassThru `
    -ArgumentList @('-p', '--permission-mode', 'auto', '--permission-prompts', 'none', '--name', "resolve-conflicts-pr$($target.number)") `
    -RedirectStandardInput "$base.prompt.txt" -RedirectStandardOutput "$base.log" -RedirectStandardError "$base.err.log"

Write-Json $LockFile ([ordered]@{ pid = $proc.Id; pr = $target.number; started = (Get-Date).ToString('o'); log = "$base.log" })
$tried[$key] = [ordered]@{ head = $target.headRefOid; base = $target.baseSha; at = (Get-Date).ToString('o') }
Write-Json $StateFile $tried
Write-Log "起動した: $label pid=$($proc.Id) ログ: $base.log"
