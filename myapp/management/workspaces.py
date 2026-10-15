"""管理コマンドが読み書きする範囲を決める（W1b の暫定。T5 で `--workspace` に替える）。

- 実データ（`legacy_real_scope`）: ワークスペースは一時関数 `legacy_workspace_id()`（id が最小のもの）で決める。
  1つも無ければトレースバックではなく日本語の `CommandError` にする。1回の実行で1回だけ呼ぶ（呼ぶ側が範囲を持ち回る）。
- 世界（`legacy_world_scope`・`legacy_world_real_scope`）: ワークスペースは `--world` の世界の行から導く（W1b T3）。
  世界の台帳はワークスペースで絞られるので、一時関数のワークスペースで範囲を作ると、ほかのワークスペースの
  世界を `--world` で指したときに台帳で見つからない。無い世界は日本語の `CommandError`。
"""

from django.core.management.base import CommandError

from ..domain.exceptions import WorkspaceNotFound, WorldNotFound
from ..domain.pennant.world import WorldScope
from ..infrastructure.workspaces import legacy_workspace_id, workspace_id_of_world


def _workspace_id() -> int:
    try:
        return legacy_workspace_id()
    except WorkspaceNotFound as error:
        raise CommandError(str(error)) from error


def _workspace_id_of_world(world_id: int) -> int:
    try:
        return workspace_id_of_world(world_id)
    except WorldNotFound as error:
        raise CommandError(str(error)) from error


def legacy_real_scope() -> WorldScope:
    """今のワークスペースの実データの範囲。"""
    return WorldScope.real(_workspace_id())


def legacy_world_scope(world_id: int) -> WorldScope:
    """ペナントの世界ひとつの範囲。ワークスペースは世界の行のもの（`World.scope` と同じ）。"""
    return WorldScope.pennant(_workspace_id_of_world(world_id), world_id)


def legacy_world_real_scope(world_id: int) -> WorldScope:
    """世界が属するワークスペースの実データの範囲。世界を消すコマンドが、その世界の台帳を組み立てるのに使う。"""
    return WorldScope.real(_workspace_id_of_world(world_id))
