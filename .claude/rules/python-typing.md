---
paths:
  - "myapp/**/*.py"
  - "config/**/*.py"
---

# 型注釈と静的検査

- **`domain` / `application` / `infrastructure` の関数は引数・戻り値に型注釈を付ける**（`pyproject.toml` で `disallow_untyped_defs` が効いている）。注釈の無い関数は **mypy の検査対象から外れ、中身がどれだけ間違っていても黙って通る**。`application/services.py` の `__init__` に注釈を付けただけで、同ファイルから29件の指摘が出た前例がある。`presentation` は `request` など注釈しにくい引数が多いため対象外。
- 注釈が付けにくいときに `Any` や `# type: ignore` で通すのは、**その関数が本当に型を選ばない場合だけ**（例: 何が来ても int に直す `domain/value_objects.py` の `_require_non_negative`）。理由をコメントに書く。`# type: ignore` は `[misc]` のようにコードまで書く。
- **同じスコープで、型の違う値に同じ変数名を使わない。** mypy は最初の代入で型を固定するため、2つ目以降が検査されない・誤った指摘になる。前例: ボックススコアで打撃と投球のループ変数をどちらも `entry` / `line` にしていた（`outing` / `pitched` に改名して解消）。
- 保存済みの集約から取り出す id（型は `int | None`）は `application/services.py` の `_saved_id()` を通す。内包表記の中でも同じ書き方で済む。
- mypy が `INTERNAL ERROR` を出したときは型の問題ではなく、アプリのどこかが import に失敗している。先に `python manage.py check` で切り分ける。
