---
paths:
  - "myapp/templates/**/*.html"
---

# テンプレートの書き方

- **コメント**: `{# ... #}` は単一行専用。複数行にまたがると中身がそのまま画面に出る（エラーにならない）。複数行は `{% comment %}` を使い、必ず `{% extends %}` より後に置く（`extends` は最初のタグでなければならない）。検査は `tests/integration/test_templates.py` にある。
- **テンプレート検索順**: `INSTALLED_APPS` の `myapp` は `django.contrib.admin` より前に置いたまま動かさない（`registration/` テンプレートの優先順位が壊れる）。
- 文言はすべて日本語。見た目は Bootstrap 5 + `static/myapp/css/theme.css` の既存クラス（`entry-table` など）に揃え、新しい配色やフォントを持ち込まない。
- 成績表は `tabular-nums` で桁を揃える。
- 書き込みの導線（登録フォーム・編集ボタン）は未ログインの人に表示しない。`{% if user.is_authenticated %}` で囲む。
- 選択肢の一覧（守備位置・球場の屋根種別など）をテンプレートに並べない。ドメインの値オブジェクトが唯一の出典で、ビューかフォーム経由で受け取る。
