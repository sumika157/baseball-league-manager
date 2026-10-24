"""テンプレートのコンテキストプロセッサ。"""

from django.http import HttpRequest


def workspace(request: HttpRequest) -> dict:
    """今の要求のワークスペース（`workspace_view` が URL の key から解決した `WorkspaceContext`）を渡す。

    ワークスペースの外の画面（`/`・ログイン・404 など）では None。ヘッダーの導線とバッジ、`{% scoped_url %}`・
    `{% workspace_url %}` の key はここから取る。
    """
    return {"workspace": getattr(request, "workspace", None)}
