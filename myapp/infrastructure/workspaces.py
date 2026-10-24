"""ワークスペースの解決（管理コマンドの入口用）。画面は URL の key を `workspace_view` が解決する（W2 T4）。

- `workspace_id_of_world()`（W1b T3）: 管理コマンドが `--world` から世界のワークスペースを導く。
- `workspace_id_for_key()`（W1b T5）: 管理コマンドが `--workspace <key>` からワークスペースを引く。
"""

from ..domain.exceptions import WorkspaceNotFound, WorldNotFound
from . import orm_models


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


def workspace_id_for_key(key: str) -> int:
    """key のワークスペースの id。無い key なら `WorkspaceNotFound`。"""
    workspace_id = orm_models.Workspace.objects.filter(key=key).values_list("id", flat=True).first()
    if workspace_id is None:
        raise WorkspaceNotFound(f"ワークスペースが見つかりません（key={key}）。")
    return int(workspace_id)
