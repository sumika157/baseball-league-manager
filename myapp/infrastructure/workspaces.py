"""ワークスペースの一時的な解決（W1a の暫定。W2 で消す）。

書き込み口がまだ「どのワークスペースの操作か」を知らない間、新しい行は
id が最小のワークスペース（移行で作った既定のもの）に属させる。

- W1b の Q2（範囲がワークスペースを持つ形への移行）もこの関数を使う
  （views に別の実装を置かない）。
- W2 で、書き込み口が範囲のワークスペースを渡す形に替え、この関数ごと消す。
"""

from ..domain.exceptions import WorkspaceNotFound
from . import orm_models


def legacy_workspace_id() -> int:
    """id が最小のワークスペースの id を返す。1つも無ければ `WorkspaceNotFound`。"""
    workspace_id = orm_models.Workspace.objects.order_by("id").values_list("id", flat=True).first()
    if workspace_id is None:
        raise WorkspaceNotFound(
            "ワークスペースがありません。運営者が管理画面で最初のワークスペースを作るまで、この操作はできません。"
        )
    return int(workspace_id)
