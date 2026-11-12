---
name: run-tests
description: このプロジェクト（Docker上のDjangoアプリ）でテストを書き・実行する。「テスト実行して」「テスト通して」「domainだけテストして」と頼まれたとき、テストを書く前にどの層に何本置くか・どの技法で入力を選ぶかを決めるとき、テストが落ちた・たまに落ちる（フレーキー）ときに使う。DBが不要なdomain層テストの高速実行と、DBが必要な統合テストを含むフルスイート実行を切り替える。
---

テストを書く前の判断・実行・落ちたときの扱い。**基本コマンド（フルスイート・domain のみ）と lint / 型チェックのゲートは
`CLAUDE.md`（実行環境・テスト）が出典**、**置き場所とファイル分割は `.claude/rules/tests.md` が出典**で、
ここには「何をどの層に書くか」「どれを実行するか」の判断と、CLAUDE.md に無い個別の実行方法・診断だけを書く。
入力の選び方（境界値・デシジョンテーブル・状態遷移など）の技法とこのプロジェクトでの前例は [design.md](design.md)。

**worktree のコードをテストするときは、以下のコマンドに `-w /app/.claude/worktrees/<名前>` とキャッシュ用の `-e` を足す**
（`worktree` スキルの手順2）。付けないと main の作業ツリーのコードをテストし、通っても worktree の変更は確かめられていない。

## 0. 書く前に: どの層に何本置くか

E2E は1ケース数十秒、domain のテストはミリ秒で、4〜5桁違う。E2E は経由する部品のどれが変わっても壊れ、
落ちたときの原因の候補も多く、部品が増えるほど関係ない理由で落ちる（0.1% の揺らぎが100個つながると約1割落ちる）。
組み合わせを E2E で網羅しようとすると、分岐の数の**積**だけケースが要る（単体なら**和**で済む）。
したがって配分は次のとおりにする（テストピラミッド）。

| 層 | 担うもの | 本数 |
| --- | --- | --- |
| `tests/domain/` | 業務ルールの分岐・境界値・組み合わせ。**分岐の網羅はここで行う** | 最も多い |
| `tests/integration/` | 配線（組み立て口・URL・権限）・リポジトリの往復・フォーム検証・テンプレート・世界の隔離 | 中 |
| `tests/e2e/` | 実ブラウザでしか分からないもの（JS・CSS・React）と、主要導線の**代表1シナリオ** | 最も少ない |

- **分岐が多い処理は、先に domain に切り出してから単体テストで網羅する。** view やテンプレートに分岐を書いて integration で
  網羅しようとしない。網羅できない形になっていたら、テストの書き方より先に設計（どの層に置くか）を疑う。
- 新しい画面の導線に E2E を足すときは、**代表的な1シナリオだけ**にする（例: 「ペナントを作って日を進め、順位を見る」）。
  分岐ごとの E2E を足さない。
- integration で検証できるものを E2E に寄せない（`.claude/rules/tests.md`）。JS・CSS が絡まない画面の確認は
  `self.client` で足りる。
- 期待値は仕様から書く（規則は `.claude/rules/tests.md` の「期待値」。特性テストの使いどころは [design.md](design.md) の「特性テスト」）。
- バグの修正に添える再発防止テストは、**修正を外すと落ちる**ことを確かめる（下記 §6.5）。

## 1. コンテナが動いているか確認する

```bash
docker compose ps
```

`web` が `Up` でなければ先に起動する。**起動は WSL 側から**（`docker-ops` スキル参照。Windows 側から
`up` するとマウントが壊れる）。

## 2. どれを実行するか判断する

| 頼まれ方 | 実行内容 |
| --- | --- |
| 「テスト実行して」「全部テストして」など指定なし | フルスイート（CLAUDE.md のコマンド） |
| 「domainだけ」「ドメイン層のテストだけ」「速いテストだけ」 | domain層のみ・DB不要（CLAUDE.md のコマンド） |
| 「E2Eだけ」「ブラウザテストだけ」 | 下記 §4 |
| ファイル・テストケース名の指定あり | 下記 §3 |

フルスイートは CLAUDE.md のとおり2回に分ける（domain + integration を `--parallel 4`、e2e を直列）。
並列数は `auto` にしない（コンテナは他のエージェントと共有で、20本にしたら Docker のエンジンが応答しなくなった）。
並列で落ちたのに `TypeError: cannot pickle 'traceback' object` しか出ないときは、イメージに `tblib` が無い（古い）。
どれが落ちたかは、`--parallel` を外して直列で流し直せば分かる。
`--slowest-classes` で遅いクラスを測るときも直列で流す。

### 実装中の回し方（内側から外へ）

直しては確かめる間は、**変更に近い速いテストから**回し、広げるのは通ってから:

1. 触った domain のテストファイルだけ（`python -m unittest myapp.tests.domain.test_xxx`。DB 不要で数秒）
2. 触った範囲の integration のファイルだけ（§3）
3. push の前に1回だけフルスイート（CLAUDE.md）

E2E を直しては流す繰り返しは避ける（1周が分単位になる）。E2E が落ちたら、まず同じことを integration か domain で再現できないかを考え、
できればそちらにテストを置いて直す。

## 3. 一部だけ実行する

```bash
docker compose exec web python manage.py test myapp.tests.integration
docker compose exec web python manage.py test myapp.tests.integration.test_games
docker compose exec web python manage.py test myapp.tests.domain.test_entities.PlayerTest
```

`tests/integration/` は対象ごとにファイルが分かれている（`test_players.py`・`test_games.py`・
`test_admin_validation.py` など）。変更した範囲のファイルだけ回すと速い。
domain 層を個別に、Django 設定なしで回す場合は `python -m unittest myapp.tests.domain.test_value_objects`。

## 4. E2Eのみ（Playwright実ブラウザ、DB必要）

```bash
docker compose exec web python manage.py test myapp.tests.e2e
```

- 実行前にフロントエンドのビルド成果物が必要（`make frontend-build`。`up` 済みなら watch で最新）。
- コンテナ内の Chromium（headless）で動く。`docker-compose.yml` に `shm_size: '1gb'` 設定済み
  （既定の64MBだと Chromium がクラッシュしやすい）。
- ブラウザが見つからないエラーが出たらイメージが古い。再ビルドする（`docker-ops` 参照）。
- E2E の書き方（待ち方・操作の共通化・データの準備）は [design.md](design.md) の「E2E を書くとき」。

## 5. Lint・型チェック

「lintも」「型チェックも」と言われたら、あわせて実行する（コミット前は必須。対象コマンドは CLAUDE.md）。

## 5.5 性能の確認

「遅い」「重い」「速くして」と言われたら、まず実測する。テストでは出ない（テストのデータ量が
小さいため）。判断基準は `.claude/rules/performance.md`。

```bash
docker compose exec web python manage.py measure_pages
MSYS_NO_PATHCONV=1 docker compose exec web python manage.py measure_pages --profile /games/
```

## 5.6 カバレッジ（テストの抜けを探す）

```bash
make coverage t=myapp.tests.domain.test_decisions   # WSL から。範囲は t= で絞れる
# Windows 側（make が無い）からは Makefile の coverage ターゲットと同じものを直接叩く
docker compose exec -e COVERAGE_FILE=/tmp/coverage-main web python -m coverage run manage.py test myapp.tests.domain.test_decisions
docker compose exec -e COVERAGE_FILE=/tmp/coverage-main web python -m coverage report
```

未実行の行を、**足すべきテストの候補**として読む。率を目標にしない（通っただけの行は確かめた行ではない。
アサーションの無いテストでも率は上がる）。抜けていた分岐は §0 の配分で、置く層を決めてから足す。

## 6. 失敗したときの一次診断

- `ImproperlyConfigured: ... DJANGO_SECRET_KEY ...` → `.env` が無い、または読み込まれていない。コンテナ再起動を確認する。
- domain層テストのはずが Django のエラーが出る → 対象ファイルが誤って Django に依存するコードを import していないか確認する（`ddd-boundary-reviewer` agent でレビューできる）。
- `No module named 'myapp.tests...'` → 移動・分割したテストファイルへの古い import が残っている。
- 統合テストのみ失敗し domain層は通る → `myapp/infrastructure/` か `myapp/presentation/` 側の変更を疑う。
- E2Eのみ失敗し integration は通る → 静的ファイル・JS・テンプレートの配線を疑う（E2Eだけが実ブラウザでCSS/JSまで読む）。
- 型注釈を足した直後に mypy の指摘が増えた → 想定どおり（注釈が無い関数は検査されていなかった）。CLAUDE.md の「型と静的検査」参照。

### 落ちたテストの期待値を書き換える前に

落ちたテストの期待値を実装の出力に合わせて書き換えない（規則は `.claude/rules/tests.md` の「期待値」）。
書き換える前に「仕様が変わったのか、実装が壊れたのか」を決める:

- 今回の変更で仕様を意図して変えたか、Issue・ユーザーの決定・Wiki の仕様と照らす。決定があれば期待値を直す。
- 決定が無い → 実装を直す。判断がつかなければユーザーに聞く。
- ゴールデンファイル（`tests/integration/golden/`）は、差分を1行ずつ読み、意図した変更だけであることを確かめてから更新する。

### たまに落ちる（フレーキー）とき

**通るまで流し直して済ませない。** 1回でも落ちたなら原因がある。単独・直列で流し直して再現条件を絞る:

| 症状 | 疑う原因 |
| --- | --- |
| 並列でだけ落ちる | テスト間で共有している状態（クラス変数・モジュールの変数・キャッシュ）。同じ worktree で別のテスト実行が同時にテスト DB を使っている（同じ worktree で同時に流さない） |
| 単独では通り、スイートでは落ちる | 前のテストが残した状態（`setUpTestData` で作ったオブジェクトをテストの中で書き換えている、など） |
| 日付や年が変わると落ちる | `date.today()`（`Team.add_player` などの年の既定値。CLAUDE.md「既知の罠」）。年は明示して渡す |
| シミュレーションの結果がぶれる | 乱数の種を固定していない。`make_random(seed)` か、テスト内の `ScriptedRandom` で出目を指定する |
| E2E でだけ時々落ちる | 待ち方。`time.sleep` ではなく `wait_for_url`・`locator(...).wait_for()` で状態を待つ |
| exit 137 で落ちる | 並行中のコンテナの再起動・メモリ不足（テストの不具合ではない。並行作業中はコンテナを再起動しない） |

直したら、落ちていた条件（並列・単独・繰り返し）で数回流して再現しないことを確かめる。直せないまま `skip` にしない。

## 6.5 再発防止テストが本当に効くかを確かめる

バグの修正に添えたテストは、**修正を外した状態で落ちる**ことを1回確かめる（確認テスト。小さなミューテーションテスト）:

```bash
git -C <作業ツリー> stash push -- myapp/<修正したファイル>   # 修正だけを一時的に外す（テストは残す）
# → 追加したテストだけを流し、落ちることを確かめる
git -C <作業ツリー> stash pop
```

修正をもうコミットしてあるときは、`git -C <作業ツリー> show HEAD -- myapp/<修正したファイル> | git -C <作業ツリー> apply -R` で外し、
確かめた後に `git -C <作業ツリー> checkout -- myapp/<修正したファイル>` で戻す。

修正を外しても通るテストは、バグを捕まえていない（条件が緩い・別の経路を通っている・アサーションが無い）。
新しい業務ルールのテストでも、要の条件（`<` と `<=`、`and` と `or` など）を1か所だけ逆にして落ちるかを見ると、境界を押さえられているか分かる。
確かめた後は必ず元に戻し、`git diff` で戻ったことを見る。
