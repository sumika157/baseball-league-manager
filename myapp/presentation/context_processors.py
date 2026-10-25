"""テンプレートのコンテキストプロセッサ。"""

from django.http import HttpRequest

from .views import my_workspaces


def workspace(request: HttpRequest) -> dict:
    """今の要求のワークスペース（`workspace_view` が URL の key から解決した `WorkspaceContext`）を渡す。

    ワークスペースの外の画面（`/`・ログイン・404 など）では None。ヘッダーの導線とバッジ、`{% scoped_url %}`・
    `{% workspace_url %}` の key はここから取る。

    - `can_set_up`: ヘッダーの「登録」とダッシュボードの登録の案内の表示条件（`WorkspaceAccess.can_set_up()`）。
    - `can_manage_workspace`: ヘッダーの「ワークスペースの設定」の表示条件（持ち主だけ。`can_change_settings()`）。
    - `my_workspaces`: ヘッダーの切り替えに出す自分のワークスペースの一覧。ワークスペースの中でログインしているとき
      だけで、描いたときに1回だけ読む遅延オブジェクト（それ以外は空）。

    権限のフラグは解決済みの権限から作るので DB は読まない。ワークスペースの外ではどれも偽か空。
    """
    workspace = getattr(request, "workspace", None)
    if workspace is None:
        return {"workspace": None, "can_set_up": False, "can_manage_workspace": False, "my_workspaces": []}
    return {
        "workspace": workspace,
        "can_set_up": workspace.access.can_set_up(),
        "can_manage_workspace": workspace.access.can_change_settings(),
        "my_workspaces": my_workspaces(request),
    }
