---
name: open-pr
description: 実装を終えたブランチを、3周のセルフレビューを済ませてから push し、Issue とリンクした PR を作って URL を提示する。「PR を出して」「PR にして」と頼まれたとき、または実装が終わって CLAUDE.md の流れで PR を出す段になったときに使う。PR を作った後の Issue の更新（チェック・epic の段階の sub-issue を閉じる）も扱う。
---

PR を出すまでの手順。**規則（3周セルフレビューの観点と終わる条件・コミットの粒度・マージはユーザーが行う）は
`CLAUDE.md`「ブランチとコミット」が出典**で、ここには順番とコマンドだけを書く。Issue の書き方とリンクの仕組みは `issue-plan` スキル。

以下、PowerShell の例は毎回この行から始める（PowerShell ツールは呼び出しごとに変数が消える）:

```powershell
$gh = "C:\Users\sumik\AppData\Local\Microsoft\WinGet\Packages\GitHub.cli_Microsoft.Winget.Source_8wekyb3d8bbwe\bin\gh.exe"
```

## 0. 前提を確かめる

- ブランチが Issue に紐づいている: `& $gh issue develop --list <Issue番号>` にブランチが出る。出なければ `issue-plan` の手順4の「既にブランチを切ってしまった場合」。
- base を決める: 通常は `main`。epic のタスクなら `epic/<機能>`（間違えると前の段階の差分まで載った PR になる）。
- 変更が1つの機能につき1コミットにまとまっている（`git log --oneline origin/<base>..HEAD`）。

## 1. 3周セルフレビュー

1周 ＝ 差分の全体（`git diff origin/<base>...HEAD`）を **正しさ・規則・読み手の3観点すべて**で読み、見つけたものを直すところまで。

| 観点 | 何を使って見るか |
| --- | --- |
| 正しさ | 差分を読む。コードの変更なら `/code-review` を使ってよい |
| 規則 | `CLAUDE.md` と、触ったファイルに効く `.claude/rules/`。domain・集約・リポジトリ・クエリに触れたら `ddd-boundary-reviewer` エージェント |
| 読み手 | Issue の計画（`& $gh issue view <番号>`）・コミットメッセージ・README・`docs/wiki/`・PR 本文の下書き。デバッグ出力・一時ファイル・無関係な変更の混入 |

- 直しは `git commit --amend` で機能のコミットに含める（push 前なので書き換えてよい）。
- 周の途中でコードを直したら、その周の終わりに lint と、直した範囲のテストを通し直す（worktree なら `worktree` スキルの手順2。`-w` を忘れない）。
- 最後の周の後、push の前に1回フルテストを流す（手元と CI の分担は `CLAUDE.md`「テストと品質のゲート」）。
- 周ごとに「何を直したか」を1行ずつメモしておく（手順3の「セルフレビュー」節に貼る）。
- 計画からずれたら Issue 本文を直し、理由をコメントに残す（`issue-plan` の手順5）。

## 2. push する

```bash
git -C <作業ツリー> push -u origin <ブランチ>
```

ブランチは `gh issue develop` でリモートに作ってあるので、最初の push でもブランチの作成にはならない。

## 3. PR を作る

**作る前に、同じブランチの PR が無いかを見る**（push 後にユーザーが先に作っていることがある。2本目を作るとスカッシュマージで main に空コミットが残る）:

```powershell
& $gh pr list --head <ブランチ> --state all
```

出たら新しく作らず、その PR の本文を `& $gh pr edit <PR番号> --body-file "<パス>"` で直す。

本文はスクラッチパッドにファイルで書く。雛形:

```markdown
Closes #<Issue番号>          ← base が epic なら Refs #<段階の sub-issue>

## 概要

（何をなぜ変えたか。2〜4文。Issue の「なぜ」を言い換えるだけでなく、結果として何がどう変わったか）

## 変更

- （ファイルや層ごとに1行）

## セルフレビュー

- 1周目: （直したこと。無ければ「指摘なし」）
- 2周目: …
- 3周目: …
- 見送り: （直さずに残した軽微な指摘と理由。無ければ行ごと消す）

🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

末尾の帰属行は、その会話で指示された文言に合わせる。

```powershell
& $gh pr create --base <base> --head <ブランチ> --title "<日本語のタイトル>" --label <Issue と同じラベル> --body-file "<パス>"
```

タイトルはコミットの1行目と同じ調子（「〜した」）。

PR を作ったら、Issue（epic のタスクなら段階の sub-issue）を Project で「レビュー待ち」（選択肢の ID `2d63e9ec`）にする。
コマンドと ID は `issue-plan` の手順4「Project の状態を『作業中』にする」と同じで、選択肢の ID だけを替える。
マージされて Issue が閉じれば、組み込みのワークフローが「完了」に動かす（epic の段階の sub-issue は手順6で閉じたときに動く）。

## 4. リンクを確かめる

```powershell
& $gh pr view <PR番号> --json closingIssuesReferences --jq '[.closingIssuesReferences[].number]'
```

- base が main なら Issue 番号が出る。出なければ本文の `Closes #N` の綴りを直す（作った直後は反映が遅れて空のことがある。少し置いて見直す）。
- base が epic のときは出ない（キーワードが効かない）。確かめ方は `issue-plan` の手順6。

## 5. CI の結果を確かめる

PR を作ると CI（`.github/workflows/ci.yml`）が走る（初回は数分）。終わるまで待って結果を見る。待つあいだに手順6を進めてよい。

```powershell
& $gh pr checks <PR番号> --watch --fail-fast   # 失敗したら終了コードが 0 以外になる
```

- 落ちたら、Actions のログ（`& $gh run view <実行ID> --log-failed`）で原因を見て直し、amend して `git push --force-with-lease` で push し直す
  （PR が OPEN のうちは、1つの機能を1コミットに保つためにこうする。下の確認を先に）。
- **PR を出した後にブランチへ追記する（amend＋force-push・本文の編集）ときは、その直前に `& $gh pr view <PR番号> --json state` が `OPEN` かを確かめ、`MERGED` なら止まる。**
  ユーザーは PR を見るとすぐマージすることがあり、マージ済みのブランチへの push も本文の編集もエラーにならないまま main に届かない（#83 と #132 で実際に起きた）。
  マージ済みなら、追記は main から切った新しいブランチ（と Issue）で出し直す。
- PR の本文の「確認したこと」に、CI が通ったこと（実行の URL）を書き足す。

## 6. Issue を更新する

- Issue 本文の手順のチェックを埋める（`& $gh issue edit <番号> --body-file "<パス>"`）。
- **epic の段階の sub-issue は自動では閉じない。** 次の段階に着手するときに、前の段階の PR が `MERGED` か確かめて閉じる（`issue-plan` の手順6）。

## 7. 報告して終わる

PR の URL と CI の結果を提示して終わる。**マージはしない**（ユーザーが GitHub 上で行う）。

マージされたら:
- worktree を片付ける（`worktree` スキルの手順4）。
- **base（main・epic）への push で走った CI の結果を確かめる**（`& $gh run list --branch <base> --limit 3`）。
  並行した PR の組み合わせで先端が壊れていないかは、これで分かる（手で先端のフルテストを流す代わり）。落ちていたら次の作業より先に直す。
