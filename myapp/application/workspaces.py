"""ワークスペースそのものの更新を担うアプリケーションサービス（W4。#80）。

**更新だけを持つ。** メンバー・招待の一覧は参照クエリ（`application/queries.py` の `WorkspaceManagementQuery`）、
招待のリンクを開いた人に見せる中身は `InvitationPreviewQuery` が読む（更新と参照を分ける）。
サービスは2つに分ける（2026-10-25 ユーザー決定 3）:

- `WorkspaceService`: 作成・設定（名前と公開範囲）。削除は T2 で足す。
- `WorkspaceMemberService`: 招待の発行・取消・受諾、役割の変更、除名。

規則の置き場所:

- 名前・公開範囲・メンバーの不変条件（持ち主は編集者にも担当にも入らない、など）は `Workspace` 集約。
- 担当チームがこのワークスペースの実データのチームであることは、保存のときにリポジトリが集約に検査させる。
- 招待の期限と1回だけ使えることは `Invitation` と集約の `accept`。消すのはここ（受諾と同じトランザクション）。
- 持てる数の上限は `WorkspaceQuota`（持っている数はリポジトリが数える）。

権限（持ち主だけが設定・メンバーを扱える）は呼ぶ側（画面の門。`WorkspaceAccess`）が確かめる。ここでは見ない。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime

from ..domain.repositories import InvitationRepository, WorkspaceRepository
from ..domain.workspace import (
    Invitation,
    InvitedRole,
    Visibility,
    Workspace,
    WorkspaceQuota,
    invitation_digest,
)
from .dto import IssuedInvitation
from .ids import saved_id
from .pennant_season import AtomicBlock


class WorkspaceService:
    """ワークスペースの作成と設定。"""

    def __init__(
        self,
        workspaces: WorkspaceRepository,
        quota: WorkspaceQuota,
        new_key: Callable[[], str],
        atomic: AtomicBlock,
    ) -> None:
        self._workspaces = workspaces
        self._quota = quota
        self._new_key = new_key
        self._atomic = atomic

    def can_create(self, owner_id: int) -> bool:
        """その利用者が、もう1つ作れるか（作成のフォームの代わりに上限の案内を出すかの判定）。"""
        return self._quota.can_create_workspace(self._workspaces.count_owned_by(owner_id))

    @property
    def limit_message(self) -> str:
        """上限に達したときの案内（文言の出典は `WorkspaceQuota`）。"""
        return self._quota.limit_message

    def create(self, *, owner_id: int, name: str, visibility: Visibility) -> str:
        """空のワークスペースを作り、key を返す。上限に達していれば `WorkspaceLimitReached`（何も作らない）。

        数える → 上限の検査 → 保存を1つのトランザクションで行う。
        """
        with self._atomic():
            self._quota.ensure_can_create_workspace(self._workspaces.count_owned_by(owner_id))
            workspace = Workspace(name=name, key=self._new_key(), owner_id=owner_id, visibility=visibility)
            return self._workspaces.add(workspace).key

    def change_settings(self, workspace_id: int, *, name: str, visibility: Visibility) -> None:
        """名前と公開範囲を変える。"""
        with self._atomic():
            workspace = self._workspaces.find_by_id(workspace_id)
            workspace.rename(name)
            workspace.change_visibility(visibility)
            self._workspaces.save(workspace)


class WorkspaceMemberService:
    """招待とメンバー（編集者・チーム担当）の管理。"""

    def __init__(
        self,
        workspaces: WorkspaceRepository,
        invitations: InvitationRepository,
        new_token: Callable[[], str],
        clock: Callable[[], datetime],
        atomic: AtomicBlock,
    ) -> None:
        self._workspaces = workspaces
        self._invitations = invitations
        self._new_token = new_token
        self._clock = clock
        self._atomic = atomic

    def issue_invitation(self, workspace_id: int, *, role: InvitedRole, team_ids: Iterable[int]) -> IssuedInvitation:
        """招待を発行する。**トークンを返すのはここ1回だけ**（保存するのはダイジェスト）。

        発行のついでに、このワークスペースの期限切れの招待を消す。担当チームがこのワークスペースの実データの
        チームでなければ `InvalidInvitation`（何も書かない）。
        """
        now = self._clock()
        token = self._new_token()
        with self._atomic():
            workspace = self._workspaces.find_by_id(workspace_id)
            self._invitations.delete_expired(saved_id(workspace.id), now)
            invitation = Invitation.issue(
                workspace_id=saved_id(workspace.id), token=token, role=role, team_ids=team_ids, now=now
            )
            saved = self._invitations.add(invitation)
        return IssuedInvitation(
            invitation_id=saved_id(saved.id), token=token, role=saved.role, expires_at=saved.expires_at
        )

    def revoke_invitation(self, workspace_id: int, invitation_id: int) -> None:
        """未使用の招待を取り消す。無い（ほかのワークスペースの招待でも）なら `InvalidInvitation`。"""
        self._invitations.delete(workspace_id, invitation_id)

    def accept_invitation(self, token: str, user_id: int) -> str:
        """招待を受けてメンバーになり、ワークスペースの key を返す。

        無い・期限切れ・使用済みは `InvalidInvitation`（同じ文言）。メンバーの保存と招待を消すことを1つの
        トランザクションで行う（同じ招待を2回使えない。消せなければ保存も戻る）。
        """
        digest = invitation_digest(token)
        with self._atomic():
            invitation = self._invitations.find_by_digest(digest)
            workspace = self._workspaces.find_by_id(invitation.workspace_id)
            workspace.accept(invitation, user_id, now=self._clock())
            self._workspaces.save(workspace)
            self._invitations.delete(invitation.workspace_id, saved_id(invitation.id))
        return workspace.key

    def change_role(self, workspace_id: int, user_id: int, *, role: InvitedRole, team_ids: Iterable[int] = ()) -> None:
        """メンバーの役割（全体を編集 ⇔ 担当チーム）と担当チームを変える。メンバーでなければ `MemberNotFound`。"""
        with self._atomic():
            workspace = self._workspaces.find_by_id(workspace_id)
            workspace.change_role(user_id, role, team_ids)
            self._workspaces.save(workspace)

    def remove_member(self, workspace_id: int, user_id: int) -> None:
        """編集者・チーム担当を外す（担当チームもすべて外れる）。メンバーでなければ `MemberNotFound`。"""
        with self._atomic():
            workspace = self._workspaces.find_by_id(workspace_id)
            workspace.remove_member(user_id)
            self._workspaces.save(workspace)
