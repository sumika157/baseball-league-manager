---
paths:
  - "myapp/templates/**/*.html"
---

# テンプレートの書き方

- **コメント**: `{# ... #}` は単一行専用。複数行にまたがると中身がそのまま画面に出る（エラーにならない）。複数行は `{% comment %}` を使い、必ず `{% extends %}` より後に置く（`extends` は最初のタグでなければならない）。検査は `tests/integration/test_templates.py` にある。
- **テンプレート検索順**: `INSTALLED_APPS` の `myapp` は `django.contrib.admin` より前に置いたまま動かさない（`registration/` テンプレートの優先順位が壊れる）。
- 文言はすべて日本語。見た目は Bootstrap 5 + `static/myapp/css/theme.css` の既存クラス（`entry-table` など）に揃え、新しい配色やフォントを持ち込まない。
- 成績表は `tabular-nums` で桁を揃える。
- **ワークスペースの中の画面（`myapp/urls.py` の `workspace_urlpatterns`。`/w/<key>/` の下）へのリンクは `{% scoped_url %}` か `{% workspace_url %}` で引く。** 素の `{% url %}` はワークスペースの外（`/`・ログイン・管理画面）だけ。共有テンプレート（実データとペナントの世界で使う）は `scoped_url`、片方専用の画面は `workspace_url`。素の `{% url %}` では key を運べない（`test_templates.py` の `WorkspaceUrlTagTest` が検査する）。
- どちらのタグも context の `workspace`（今の要求のワークスペース）の key を足し、`workspace` の無い描画では例外になる。ワークスペースの外でも描くテンプレート（`base.html`・`404.html` など）は、中の画面へのリンクを `{% if workspace %}` で囲む。`/` の一覧のように行ごとに別のワークスペースの入口を引くときは `{% workspace_entry_url key %}`。
- 書き込みの導線（登録フォーム・編集ボタン）は、**ビューが渡す権限のフラグで囲む**（`can_edit_team`・`can_edit`・`can_create_game`・世界の `is_owner`・作成フォームの有無など。ビューは `request.workspace.access`（`WorkspaceAccess`）から作る）。`{% if user.is_authenticated %}` で囲まない（ログインしただけの人や、管理ユーザーでもワークスペースの部外者には、押しても弾かれる導線になる）。未ログインならフラグは偽。ヘッダーの「管理画面」のリンクだけは `user.is_staff`（管理画面に入れる条件そのもの）。
- 選択肢の一覧（守備位置・球場の屋根種別など）をテンプレートに並べない。ドメインの値オブジェクトが唯一の出典で、ビューかフォーム経由で受け取る。
