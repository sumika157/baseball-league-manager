"""テンプレートのコンテキストプロセッサ。"""

from django.http import HttpRequest


def workspace(request: HttpRequest) -> dict:
    """今の要求のワークスペース（`workspace_view` が URL の key から解決した `WorkspaceContext`）を渡す。

    ワークスペースの外の画面（`/`・ログイン・404 など）では None。ヘッダーの導線とバッジ、`{% scoped_url %}`・
    `{% workspace_url %}` の key はここから取る。

    `can_set_up` はヘッダーの「登録」とダッシュボードの登録の案内の表示条件（`WorkspaceAccess.can_set_up()`。
    解決済みの権限から作るので DB は読まない）。ワークスペースの外では偽。
    """
    workspace = getattr(request, "workspace", None)
    return {"workspace": workspace, "can_set_up": workspace is not None and workspace.access.can_set_up()}
