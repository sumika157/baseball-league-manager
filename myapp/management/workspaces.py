"""管理コマンドが読み書きする範囲を決める（W1b の暫定。T5 で `--workspace` と世界からの導出に替える）。

ワークスペースは一時関数 `legacy_workspace_id()`（id が最小のもの）で決める。1つも無ければ
トレースバックではなく日本語の `CommandError` にする。1回の実行で1回だけ呼ぶ（呼ぶ側が範囲を持ち回る）。
"""

from django.core.management.base import CommandError

from ..domain.exceptions import WorkspaceNotFound
from ..domain.pennant.world import WorldScope
from ..infrastructure.workspaces import legacy_workspace_id


def _workspace_id() -> int:
    try:
        return legacy_workspace_id()
    except WorkspaceNotFound as error:
        raise CommandError(str(error)) from error


def legacy_real_scope() -> WorldScope:
    """今のワークスペースの実データの範囲。"""
    return WorldScope.real(_workspace_id())


def legacy_world_scope(world_id: int) -> WorldScope:
    """今のワークスペースの、ペナントの世界ひとつの範囲。

    暫定（W1b T2）: ワークスペースは一時関数のもので、世界の行の workspace とは照合していない。
    台帳がワークスペースで絞られる T3 からは、世界の行からワークスペースを導く形にする必要がある
    （そうしないと、ほかのワークスペースの世界を `--world` で指したときに台帳で見つからない）。
    """
    return WorldScope.pennant(_workspace_id(), world_id)
