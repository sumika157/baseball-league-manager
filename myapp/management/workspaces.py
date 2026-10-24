"""管理コマンドが読み書きする範囲を決める（W1b T5）。

- 実データ（`real_scope_for_key`）: `--workspace <key>` で指したワークスペース。**省けばエラー**で、
  ワークスペースが1つしか無くても推し量らない（範囲を取り違えた投入は、エラーにならずにほかの
  ワークスペースのデータを書き換える）。無い key も日本語の `CommandError`。`add_workspace_argument` で引数を足す。
- 世界（`world_scope`・`world_real_scope`）: ワークスペースは `--world` の世界の行から導く（W1b T3）。
  世界の台帳はワークスペースで絞られるので、ほかのワークスペースの世界を `--world` で指しても、その世界の
  ワークスペースの台帳で引ける。無い世界は日本語の `CommandError`。
- `legacy_real_scope`（暫定。W2 で消す）: `measure_pages` だけが使う。今の URL は一時関数のワークスペース
  （id が最小のもの）しか開けないので、測る画面のデータも同じワークスペースから拾う（`--workspace` は W2）。
"""

from typing import Any

from django.core.management.base import CommandError

from ..domain.exceptions import WorkspaceNotFound, WorldNotFound
from ..domain.pennant.world import WorldScope
from ..infrastructure.workspaces import legacy_workspace_id, workspace_id_for_key, workspace_id_of_world

WORKSPACE_OPTION = "--workspace"


def add_workspace_argument(parser: Any, *, help: str = "読み書きするワークスペースの key（必須）") -> None:
    """`--workspace <key>` を足す。必須の検査は `real_scope_for_key` が日本語で行う

    （argparse の `required=True` にすると、省いたときのエラーが英語になる）。
    """
    parser.add_argument(WORKSPACE_OPTION, dest="workspace", default=None, metavar="KEY", help=help)


def real_scope_for_key(key: str | None) -> WorldScope:
    """`--workspace` で指したワークスペースの実データの範囲。省いた・無い key は `CommandError`。"""
    if not key:
        raise CommandError(f"{WORKSPACE_OPTION} でワークスペースの key を指定してください。")
    try:
        return WorldScope.real(workspace_id_for_key(key))
    except WorkspaceNotFound as error:
        raise CommandError(str(error)) from error


def _workspace_id_of_world(world_id: int) -> int:
    try:
        return workspace_id_of_world(world_id)
    except WorldNotFound as error:
        raise CommandError(str(error)) from error


def world_scope(world_id: int) -> WorldScope:
    """ペナントの世界ひとつの範囲。ワークスペースは世界の行のもの（`World.scope` と同じ）。"""
    return WorldScope.pennant(_workspace_id_of_world(world_id), world_id)


def world_real_scope(world_id: int) -> WorldScope:
    """世界が属するワークスペースの実データの範囲。世界を消すコマンドが、その世界の台帳を組み立てるのに使う。"""
    return WorldScope.real(_workspace_id_of_world(world_id))


def legacy_real_scope() -> WorldScope:
    """一時関数のワークスペース（id が最小のもの）の実データの範囲。`measure_pages` 専用（W2 で消す）。"""
    try:
        return WorldScope.real(legacy_workspace_id())
    except WorkspaceNotFound as error:
        raise CommandError(str(error)) from error
