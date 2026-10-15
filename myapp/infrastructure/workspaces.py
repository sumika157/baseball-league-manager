"""ワークスペースの解決（管理コマンド・今の URL の入口用）。

- `legacy_workspace_id()`（W1a の暫定。W2 で消す）: 書き込み口がまだ「どのワークスペースの操作か」を知らない間、
  id が最小のワークスペース（移行で作った既定のもの）を返す。W1b の Q2（範囲がワークスペースを持つ形への移行）も
  この関数を使う（views に別の実装を置かない）。W2 で、書き込み口が範囲のワークスペースを渡す形に替えて消す。
- `workspace_id_of_world()`（W1b T3）: 管理コマンドが `--world` から世界のワークスペースを導く。
"""

from ..domain.exceptions import WorkspaceNotFound, WorldNotFound
from . import orm_models


def legacy_workspace_id() -> int:
    """id が最小のワークスペースの id を返す。1つも無ければ `WorkspaceNotFound`。"""
    workspace_id = orm_models.Workspace.objects.order_by("id").values_list("id", flat=True).first()
    if workspace_id is None:
        raise WorkspaceNotFound(
            "ワークスペースがありません。運営者が管理画面で最初のワークスペースを作るまで、この操作はできません。"
        )
    return int(workspace_id)


def workspace_id_of_world(world_id: int) -> int:
    """世界の行が属するワークスペースの id。無い世界なら `WorldNotFound`。

    管理コマンドの入口（`--world` で世界を指す）だけが使う。世界の台帳（`DjangoWorldRepository`）は
    ワークスペースを必須で受け取るので、`--world` から台帳と範囲のワークスペースを導くのに要る。
    画面は今の URL のワークスペースの台帳で世界を引く（ここを使わない。ほかのワークスペースの世界を開けてしまう）。
    """
    workspace_id = orm_models.PennantWorld.objects.filter(id=world_id).values_list("workspace_id", flat=True).first()
    if workspace_id is None:
        raise WorldNotFound(f"世界が見つかりません（id={world_id}）。")
    return int(workspace_id)
