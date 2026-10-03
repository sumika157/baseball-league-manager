---
name: issue-plan
description: 実装に着手する前に、実装計画を GitHub Issue に書く。「〜を実装して」「〜を直して」と頼まれてブランチを切る前、または「計画を Issue にして」「Issue を立てて」と頼まれたときに使う。epic の親 Issue と段階ごとの sub-issue の作り方、Issue に紐づけたブランチの切り方（gh issue develop）、計画が変わったときの Issue の更新、PR と Issue のリンクとその確認も扱う。
---

実装計画を GitHub Issue に残すワークフロー。規則の本体は `CLAUDE.md`「ブランチとコミット」。ここには進め方だけを書く。

## 0. Issue にするか判断する

- **ブランチを切って PR を出す作業は、着手前に Issue を立てる。**
- 省いてよいのは、1行で説明しきれる変更だけ（誤字・文言修正・依存の更新など）。迷ったら立てる。
- ユーザーが既存の Issue を指して頼んだ場合は新しく立てない。その Issue の計画を読み、足りなければ本文を補う（手順5）。

## 1. 重複を確かめる

```powershell
$gh = "C:\Users\sumik\AppData\Local\Microsoft\WinGet\Packages\GitHub.cli_Microsoft.Winget.Source_8wekyb3d8bbwe\bin\gh.exe"
& $gh issue list --state all --search "<キーワード>"
```

`gh` は Windows 側にしか無い（`CLAUDE.md` 参照）。PowerShell ツールから叩く。PowerShell ツールは呼び出しごとに変数が消えるので、
以下の `& $gh ...` も毎回1行目の `$gh = ...` と同じ呼び出しに入れる。同じ内容の Issue があればそれを使う。

## 2. 計画を書く

- 新機能・仕様変更で設計判断が要るものは、先に `feature-designer` エージェントで設計し、その結論を下の雛形に要約する。
  バグ修正・既存パターンの踏襲なら自分で書いてよい。
- 本文は**スクラッチパッドにファイルで書く**（`--body-file` で渡す。引用符と改行で壊れない）。リポジトリ内に置かない。
- epic（`CLAUDE.md`「段階単体では main に入れられない機能」）は**親 Issue ＋ 段階ごとの sub-issue** にする（前例: #26 と #50〜#54）。
  - 親: 「なぜ・スコープ・段階の分け方」と、段階の一覧（各行に sub-issue の番号）。
  - 段階の sub-issue: その段階のなぜ・やること・完了条件。手順とテスト計画は着手時に書き足す。
  - 詳細設計と段階ごとの実測値は `docs/design/<機能>.md` に置き、Issue からはリンクする。**同じ内容を Issue と設計書の両方に書かない。**
  - 初期範囲から外した拡張は、epic の親ではなく別の親 Issue の sub-issue にする（前例: #55）。epic の親は main 入りで閉じるため。

### 雛形

```markdown
## なぜ

（今どんな問題があり、なぜこの変更で解決するのか。1〜3文）

## やること / やらないこと

- やる: …
- やらない: …（理由を添える）

## 方針

（層ごとの変更・どの集約か・更新か参照か・データとマイグレーション・画面と導線。
該当しない項目は書かない。クラス名・関数名の提案まで。コードは書かない）

## 手順

- [ ] …（コミットの単位が分かる粒度で）

## テスト計画

（domain / integration / e2e のどこで何を検査するか。バグ修正なら再発防止テストを必ず含める）

## ドキュメント更新

（`docs/wiki/` のどのページ・README のどの節を直すか。無ければ「なし」と理由）

## 確認したいこと

（ユーザーに選んでほしい選択肢。無ければ節ごと消す）
```

## 3. Issue を作る

```powershell
& $gh issue create --title "<日本語のタイトル>" --label <ラベル> --body-file "<スクラッチパッドのパス>"
```

- タイトルは「何をするか」を日本語で。コミットメッセージと同じ調子にする。
- ラベルは既存のものから選ぶ: 新機能・改善は `enhancement`、不具合は `bug`、ドキュメント・規則だけは `documentation`。
- 出力された URL をユーザーに提示する。
- **「確認したいこと」がある場合はここで止めて回答を待つ。** 回答を受けたら本文に反映してから着手する
  （`CLAUDE.md` の「選択肢としてユーザーに提示する」判断はここで出す）。無ければそのまま実装に進んでよい。

### sub-issue（親子）にする

```powershell
& $gh issue create --parent <親の番号> --title "..." --label <ラベル> --body-file "<パス>"   # 子として新しく作る
& $gh issue edit <親の番号> --add-sub-issue <番号>,<番号>                                    # 既存の Issue を子にする（閉じた Issue も可）
```

確かめるときは Bash から叩く（PowerShell では `--jq` の `"\(...)"` が引数の分割で壊れる）:

```bash
"/c/Users/sumik/AppData/Local/Microsoft/WinGet/Packages/GitHub.cli_Microsoft.Winget.Source_8wekyb3d8bbwe/bin/gh.exe" \
  api repos/sumika157/baseball-league-manager/issues/<親の番号>/sub_issues --jq '.[] | "\(.number) \(.state) \(.title)"'
```

複数の Issue を PowerShell の `foreach` で作るときは、ループ変数を他の変数と大文字小文字違いにしない
（PowerShell は変数名の大文字小文字を区別しない。`foreach ($s in ...)` が `$S` を上書きして全件失敗した）。

## 4. Issue に紐づけてブランチを切る

**ブランチは `git branch` / `git worktree add -b` で作らず、`gh issue develop` で作る。** Issue に紐づいたブランチ
（Issue の「Development」欄に載る）になり、そこから出した PR は自動で Issue とリンクする（GitHub Docs「Creating a branch for an issue」）。
本文の `Closes #N` は、base が main でない PR（epic のタスク）では無視され、リンクもしない（GitHub Docs「Linking a pull request to an issue」）。
ただし、Issue に紐づけたブランチから出した PR の base が main でない場合もリンクするのかは、ドキュメントに書かれておらず未検証。手順6で確かめる。

```powershell
& $gh issue develop <番号> --name <ブランチ> --base <基点>   # 基点は main か epic/<機能>
```

- このコマンドはリモートにブランチを作るだけ。手元へは取ってきて worktree にする（Bash から）:
  ```bash
  git fetch origin <ブランチ>
  git worktree add -b <ブランチ> .claude/worktrees/<名前> origin/<ブランチ>
  ```
  worktree が要らない場合は `git switch <ブランチ>` でよい。safe.directory の登録から片付けまでの手順は `worktree` スキル。
- epic は、統合ブランチ `epic/<機能>` を親 Issue に（`--base main`）、タスクブランチを段階の sub-issue に（`--base epic/<機能>`）紐づける。
- 紐づいたかは `& $gh issue develop --list <番号>` で確かめる。
- **既にブランチを切ってしまった場合**: `gh` からは既存のブランチを後から紐づけられない。PR の base が main なら本文の `Closes #N` でリンクするので
  そのまま進めてよい。base が epic なら、push 前に `gh issue develop` で作り直したブランチへ cherry-pick して移す。

## 5. 実装中に計画が変わったら

- Issue 本文を書き直す（本文が常に最新の計画であるようにする）:
  `& $gh issue edit <番号> --body-file "<パス>"`
- **変えた理由はコメントに残す**（本文を直すだけだと、何がなぜ変わったか追えない）:
  `& $gh issue comment <番号> --body-file "<パス>"`
- 手順のチェックボックスは、PR を出すときに本文を直して埋める（epic の親の段階一覧は、段階の sub-issue を閉じるときに埋める。手順6）。

## 6. PR を Issue とリンクさせる

**PR は必ず Issue とリンクさせる。** 手順4のブランチから出していれば自動でリンクする（epic のタスク PR では未検証。下で確かめる）。
加えて本文の先頭に書く:

- base が main の PR: `Closes #<番号>`（マージ時に Issue が自動で閉じる。epic を main へ入れる PR は `Closes #<親>`）
- epic のタスク PR（base が `epic/…`）: `Refs #<段階の sub-issue>`。`Closes` を書いても base が main でないので Issue は閉じず、
  キーワードだけではリンクもしない。リンクはブランチの紐づけで行う。

**作ったら確かめる**（リンクしていなくてもエラーにならない）:

```powershell
& $gh pr view <PR番号> --json closingIssuesReferences --jq '[.closingIssuesReferences[].number]'   # base が main: Issue 番号が出る
& $gh issue develop --list <Issue番号>                                                            # ブランチが Issue に紐づいているか
```

- base が main の PR は、1行目に Issue 番号が出ればよい。出なければ本文のキーワードの綴り（`Closes #N`）を見直し、
  `& $gh pr edit <PR番号> --body-file "<パス>"` で本文を直す。
- epic のタスク PR は、2行目でブランチの紐づけを確かめたうえで、**Issue のページの「Development」欄に PR が載っているかを
  ユーザーに見てもらう**（コマンドで確かめる方法を確立していない）。載っていなければ、Issue のページのサイドバーから
  PR を手で紐づけてもらう。最初の epic タスク PR で結果が分かったら、このスキルの「未検証」の記述を直す。

- セルフレビューの「読み手」観点で、PR の差分が Issue の計画と合っているか（やらないと書いたことをやっていないか、手順の漏れ）を確かめる。
  ずれていたら手順5で Issue 側を直すか、実装を計画に合わせる。
- Issue を手で閉じない。マージはユーザーが行い、そのとき閉じる。
- **例外: epic の段階の sub-issue は自動では閉じない**（base が main でないため）。次の段階に着手するとき、前の段階の PR が
  マージ済みかを `& $gh pr view <PR番号> --json state` で確かめ、`MERGED` なら
  `& $gh issue close <段階の番号> --comment "#<PR番号> で epic/<機能> に入った"` で閉じる。親 Issue の手順の行にもチェックを付ける。
