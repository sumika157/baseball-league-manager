"""ワークスペースの解決（管理コマンドの入口用）と、識別子の生成。画面は URL の key を `workspace_view` が解決する。

- `workspace_id_of_world()`（W1b T3）: 管理コマンドが `--world` から世界のワークスペースを導く。
- `workspace_id_for_key()`（W1b T5）: 管理コマンドが `--workspace <key>` からワークスペースを引く。
- `new_workspace_key()`（W4。管理画面の `_new_workspace_key` から移した）: まだ使われていない key を作る。
  作成のサービス（`WorkspaceService`）と管理画面が共有する。
- `new_invitation_token()`（W4）: 招待のトークンを作る。
"""

import random
import secrets

from ..domain.exceptions import WorkspaceNotFound, WorldNotFound
from ..domain.workspace import generate_workspace_key
from . import orm_models

# 招待のトークンの乱数のバイト数（URL に入る文字列は約43文字）
INVITATION_TOKEN_BYTES = 32


def new_workspace_key() -> str:
    """まだ使われていない key を作る。推測されないよう OS の乱数源（`SystemRandom`）で引く。"""
    rng = random.SystemRandom()
    while True:
        key = generate_workspace_key(rng)
        if not orm_models.Workspace.objects.filter(key=key).exists():
            return key


def new_invitation_token() -> str:
    """招待のトークン（URL に入れる、推測されない文字列）。保存するのはダイジェストだけ（`invitation_digest`）。"""
    return secrets.token_urlsafe(INVITATION_TOKEN_BYTES)


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
