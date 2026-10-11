<#
.SYNOPSIS
    GitHub で @claude とメンションしたコメントを見回り、手元の Claude Code（claude -p）に対応させる。

.DESCRIPTION
    既定は対応する対象を表示するだけ。-Apply を付けると、対象ごとに Claude Code を起動する。
    タスクスケジューラが1分ごとに -Apply 付きで呼ぶ（登録は scripts/register_claude_mentions_task.ps1）。
    同時に動かすのは1件ずつ（タスクは MultipleInstances IgnoreNew。対応中は次の見回りが起動しない）。

    拾うもの（前回の見回り以降に作成・更新されたもの）:
      - Issue・PR の会話のコメント
      - PR のレビューコメント（差分の行に付けたもの）
      - Issue・PR の本文
    レビューの総評（Approve などと一緒に書く本文）は拾わない。

    対応するのは次の条件をすべて満たすものだけ:
      - 書いたのが gh の認証ユーザー本人（リポジトリが公開なので、他人のコメントで手元の Claude を動かさない）
      - コードブロック・インラインコードの外に @claude がある（@claude-code などは別物として扱う）
      - Claude の返信の印（$ReplyMarker）が無い（自分の返信に反応して回り続けない）
      - 本人の 👀 リアクションがまだ無い（👀 が「受け付け済み」の印。手元の状態が消えても重ねて動かない）

    受け付けたら 👀 を付けて Claude Code を起動し、終わったら成功なら 🚀、失敗なら 😕 とログの場所を書いたコメントを付ける。
    途中で PC が落ちるなどして 👀 だけ残ったものは、👀 を外せば次の見回りで拾い直す。

    状態とログは %LOCALAPPDATA%\baseball-claude-mentions\ に置く:
      state.json（前回の見回りの時刻）・mentions.log（受け付けと結果）・runs\（1件ごとの Claude の出力。30日で消す）
    初回は過去のコメントを拾わず、時刻を記録するだけで終わる。

    gh が Windows 側にしか無いので PowerShell で書いている。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\claude_mentions.ps1          # 対象を表示するだけ
    powershell -ExecutionPolicy Bypass -File scripts\claude_mentions.ps1 -Apply   # 対応させる
#>
param(
    [switch]$Apply,
    [int]$TimeoutMinutes = 120
)

$ErrorActionPreference = 'Continue'
# gh の出力（コメントの本文）を UTF-8 として読む。既定のコードページのままだと日本語が化けて Claude に渡る
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$StateDir = Join-Path $env:LOCALAPPDATA 'baseball-claude-mentions'
$StateFile = Join-Path $StateDir 'state.json'
$LogFile = Join-Path $StateDir 'mentions.log'
$RunsDir = Join-Path $StateDir 'runs'
$ReplyMarker = '<!-- claude-mentions -->'
# 時計のずれと、API への反映の遅れで取りこぼさないよう、前回の時刻から少し戻って探す（重複は 👀 で除く）
$OverlapMinutes = 5

function Write-Log([string]$Message) {
    $line = '{0:yyyy-MM-dd HH:mm:ss} {1}{2}' -f (Get-Date), $(if ($Apply) { '' } else { '[確認のみ] ' }), $Message
    Write-Output $line
    if (-not $Apply) { return }
    if (-not (Test-Path $StateDir)) { New-Item -ItemType Directory -Path $StateDir | Out-Null }
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

# スタンドアロン版を優先し、無ければ VSCode 拡張に同梱の最新版を使う（拡張の更新でパスが変わるため毎回探す）
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

# gh api を叩いて JSON を配列で返す。失敗したら $null
function Invoke-GhApi([string[]]$Arguments) {
    $out = & $script:gh api @Arguments 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    # 空の配列は return で $null に化けるので、単項のカンマで配列のまま返す（$null は失敗の意味に取っておく）
    if (-not $out) { return ,@() }
    # PowerShell 5.1 の ConvertFrom-Json は配列を1つのオブジェクトとして流すので、ForEach-Object で展開する
    return ,@(($out -join "`n") | ConvertFrom-Json | ForEach-Object { $_ })
}

# --paginate は1ページごとに配列を出すので、つないでから読む
function Invoke-GhApiPaged([string]$Path) {
    $out = & $script:gh api --paginate $Path --jq '.[]' 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    return ,@($out | Where-Object { $_ } | ForEach-Object { $_ | ConvertFrom-Json })
}

# コードブロックとインラインコードを除いたうえで @claude を探す
function Test-Mention([string]$Body) {
    if (-not $Body) { return $false }
    if ($Body.Contains($ReplyMarker)) { return $false }
    $text = [regex]::Replace($Body, '(?s)```.*?```', '')
    $text = [regex]::Replace($text, '`[^`\r\n]*`', '')
    return [regex]::IsMatch($text, '(?<![\w@/.-])@claude(?![\w-])', 'IgnoreCase')
}

# ---- 準備 -------------------------------------------------------------------------

$gh = Find-Gh
if (-not $gh) { Write-Log 'gh が見つからないので中止した'; exit 1 }
$claude = Find-Claude
if (-not $claude) { Write-Log 'claude が見つからないので中止した'; exit 1 }

$porcelain = git -C $PSScriptRoot worktree list --porcelain 2>$null
if ($LASTEXITCODE -ne 0 -or -not $porcelain) { Write-Log "git worktree list が失敗したので中止した（$PSScriptRoot）"; exit 1 }
# 1件目が main の作業ツリー（//wsl.localhost/Ubuntu/...）
$mainPath = ($porcelain | Select-Object -First 1).Substring(9)
$repo = (git -C $mainPath remote get-url origin) -replace '^.*github\.com[:/]', '' -replace '\.git$', ''

# 対話中のセッションと同じ U: のパスで起動する（Claude のプロジェクトの記憶と許可の設定がパスごとに分かれるため）。
# U: が見えなければ \\wsl.localhost\... のまま
$workDir = $mainPath -replace '/', '\'
$drive = Get-PSDrive -PSProvider FileSystem -ErrorAction SilentlyContinue |
    Where-Object { $_.DisplayRoot -and $workDir.StartsWith($_.DisplayRoot, [StringComparison]::OrdinalIgnoreCase) } |
    Select-Object -First 1
if ($drive -and (Test-Path $drive.Root)) { $workDir = $drive.Root.TrimEnd('\') + $workDir.Substring($drive.DisplayRoot.Length) }

$me = (& $gh api user --jq .login 2>$null)
if ($LASTEXITCODE -ne 0 -or -not $me) { Write-Log 'gh の認証ユーザーが取れないので中止した'; exit 1 }

# ---- 前回の見回りの時刻 -------------------------------------------------------------

$now = (Get-Date).ToUniversalTime()
$state = $null
if (Test-Path $StateFile) {
    try { $state = Get-Content $StateFile -Raw -Encoding utf8 | ConvertFrom-Json } catch { $state = $null }
}
if (-not $state -or -not $state.lastCheck) {
    if ($Apply) {
        if (-not (Test-Path $StateDir)) { New-Item -ItemType Directory -Path $StateDir | Out-Null }
        @{ lastCheck = $now.ToString('o') } | ConvertTo-Json | Out-File -FilePath $StateFile -Encoding utf8
    }
    Write-Log "初回なので、過去のコメントは拾わずに時刻だけ記録した（$repo）"
    exit 0
}
$since = ([datetime]::Parse($state.lastCheck)).ToUniversalTime().AddMinutes(-$OverlapMinutes).ToString('yyyy-MM-ddTHH:mm:ssZ')

# ---- 対象を集める -------------------------------------------------------------------

$issueComments = Invoke-GhApiPaged "repos/$repo/issues/comments?since=$since&per_page=100"
$reviewComments = Invoke-GhApiPaged "repos/$repo/pulls/comments?since=$since&per_page=100"
$issues = Invoke-GhApiPaged "repos/$repo/issues?since=$since&state=all&per_page=100"
if ($null -eq $issueComments -or $null -eq $reviewComments -or $null -eq $issues) {
    Write-Log 'GitHub からの取得が失敗したので、次の見回りでやり直す'
    exit 1
}

$candidates = @()
foreach ($c in $issueComments) {
    $number = [int]($c.issue_url -replace '^.*/', '')
    $candidates += [pscustomobject]@{
        Kind = 'issue_comment'; Id = $c.id; Number = $number; Author = $c.user.login; Body = $c.body
        Url = $c.html_url; Created = $c.created_at; Reactions = "repos/$repo/issues/comments/$($c.id)/reactions"
    }
}
foreach ($c in $reviewComments) {
    $number = [int]($c.pull_request_url -replace '^.*/', '')
    $candidates += [pscustomobject]@{
        Kind = 'review_comment'; Id = $c.id; Number = $number; Author = $c.user.login; Body = $c.body
        Url = $c.html_url; Created = $c.created_at; Reactions = "repos/$repo/pulls/comments/$($c.id)/reactions"
        Path = $c.path; Line = $c.line
    }
}
foreach ($i in $issues) {
    $candidates += [pscustomobject]@{
        Kind = $(if ($i.pull_request) { 'pr_body' } else { 'issue_body' }); Id = $i.number; Number = $i.number
        Author = $i.user.login; Body = $i.body; Url = $i.html_url; Created = $i.created_at
        Reactions = "repos/$repo/issues/$($i.number)/reactions"
    }
}

$targets = @()
foreach ($t in ($candidates | Where-Object { $_.Author -eq $me -and (Test-Mention $_.Body) } | Sort-Object Created)) {
    $eyes = Invoke-GhApi @("$($t.Reactions)?content=eyes&per_page=100")
    if ($null -eq $eyes) { Write-Log "リアクションの取得が失敗したので次の見回りに回す: $($t.Url)"; continue }
    if ($eyes | Where-Object { $_.user.login -eq $me }) { continue }  # 受け付け済み
    $targets += $t
}

# 取得は済んだので、時刻を進める（対応中に付いたコメントは、見回りの始めの時刻以降として次で拾う）
if ($Apply) {
    @{ lastCheck = $now.ToString('o') } | ConvertTo-Json | Out-File -FilePath $StateFile -Encoding utf8
}

if ($targets.Count -eq 0) { exit 0 }

# ---- 対応させる --------------------------------------------------------------------

if ($Apply) {
    if (-not (Test-Path $RunsDir)) { New-Item -ItemType Directory -Path $RunsDir | Out-Null }
    Get-ChildItem $RunsDir -File | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } | Remove-Item -Force
    # Claude が gh を名前で呼べるようにする（タスクの環境の PATH には載っていないことがある）
    $env:PATH = (Split-Path $gh) + ';' + $env:PATH
}

foreach ($t in $targets) {
    $label = "$($t.Kind) #$($t.Number) $($t.Url)"
    if (-not $Apply) { Write-Log "対応する: $label"; continue }

    & $gh api -X POST $t.Reactions -f content=eyes 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Log "👀 を付けられなかったので次の見回りに回す: $label"; continue }
    Write-Log "受け付けた: $label"

    $where = switch ($t.Kind) {
        'issue_comment' { "#$($t.Number) の会話のコメント（comment id $($t.Id)）" }
        'review_comment' { "PR #$($t.Number) のレビューコメント（comment id $($t.Id)、$($t.Path) の $($t.Line) 行目）" }
        'pr_body' { "PR #$($t.Number) の本文" }
        default { "Issue #$($t.Number) の本文" }
    }
    $reply = if ($t.Kind -eq 'review_comment') {
        "そのレビューコメントのスレッドに返信する: ``gh api -X POST repos/$repo/pulls/$($t.Number)/comments/$($t.Id)/replies -F body=@<ファイル>``"
    } else {
        "``gh issue comment $($t.Number) -R $repo --body-file <ファイル>`` で返信する（PR も同じ番号で返信できる）"
    }
    $prompt = @"
GitHub（$repo）で、リポジトリの持ち主（$me）があなたに @claude で依頼しました。手元で自動で起動されたヘッドレスの実行で、途中でユーザーに質問はできません。

- 場所: $where
- URL: $($t.Url)
- 依頼の本文:

$($t.Body)

進め方:
1. まず gh でスレッドの前後（Issue・PR の本文とコメント、PR なら差分）を読み、依頼の文脈をつかむ。過去の @claude への依頼とあなたの返信もスレッドにある。
2. CLAUDE.md と、該当するスキル（issue-plan・worktree・open-pr など）の流れに従って対応する。
   - 質問なら調べて答える。
   - 変更の依頼なら、Issue → worktree → 実装 → 3周のセルフレビュー → PR の流れで進める。既存の PR への修正依頼なら、その PR がマージ済みでないことを確かめてから、その PR のブランチの worktree で直して push する。
   - ユーザーに選んでほしいことがあれば、選択肢を返信に書いて止める（回答は次の @claude で届く）。
   - マージはしない。コンテナを再起動しない（他の作業のテストが落ちる）。
3. 最後に、結果（何をしたか・PR の URL・答え）を日本語で返信する。$reply
   - 返信の本文の1行目には必ず $ReplyMarker を入れる（見回りが自分の返信に反応しないための印）。
4. GitHub に書くもの（返信・Issue と PR の本文・コメント）には、「@claude」をコードの外に書かない。書く必要があればインラインコードで囲む（見回りがメンションとして拾い、自分を起動し続ける）。
"@

    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $base = Join-Path $RunsDir "$stamp-$($t.Kind)-$($t.Id)"
    [IO.File]::WriteAllText("$base.prompt.txt", $prompt, (New-Object Text.UTF8Encoding($false)))

    $started = Get-Date
    $proc = Start-Process -FilePath $claude -WorkingDirectory $workDir -NoNewWindow -PassThru `
        -ArgumentList @('-p', '--permission-mode', 'auto', '--permission-prompts', 'none', '--name', "github-mention-$($t.Kind)-$($t.Id)") `
        -RedirectStandardInput "$base.prompt.txt" -RedirectStandardOutput "$base.log" -RedirectStandardError "$base.err.log"
    # PowerShell 5.1 は、ハンドルを先に取っておかないと終了後の ExitCode が空になる
    $null = $proc.Handle
    $finished = $proc.WaitForExit($TimeoutMinutes * 60 * 1000)
    if (-not $finished) {
        taskkill /PID $proc.Id /T /F 2>$null | Out-Null
        $result = "$TimeoutMinutes 分で終わらなかったので止めた"
    } elseif ($proc.ExitCode -ne 0) {
        $result = "終了コード $($proc.ExitCode)"
    } else {
        $result = $null
    }
    $minutes = [int]((Get-Date) - $started).TotalMinutes

    if (-not $result) {
        & $gh api -X POST $t.Reactions -f content=rocket 2>$null | Out-Null
        Write-Log "終わった（$minutes 分）: $label"
        continue
    }
    & $gh api -X POST $t.Reactions -f content=confused 2>$null | Out-Null
    $body = "$ReplyMarker`n手元の Claude Code の対応が失敗しました（$result、$minutes 分）。`n`nログ: ``$base.log``（エラー出力は ``$base.err.log``）`n`nやり直すときは、この依頼の 👀 を外すと次の見回りで拾い直します。"
    $bodyFile = "$base.failure.md"
    [IO.File]::WriteAllText($bodyFile, $body, (New-Object Text.UTF8Encoding($false)))
    if ($t.Kind -eq 'review_comment') {
        & $gh api -X POST "repos/$repo/pulls/$($t.Number)/comments/$($t.Id)/replies" -F "body=@$bodyFile" 2>$null | Out-Null
    } else {
        & $gh issue comment $t.Number -R $repo --body-file $bodyFile 2>$null | Out-Null
    }
    Write-Log "失敗した（$result）: $label ログ: $base.log"
}
