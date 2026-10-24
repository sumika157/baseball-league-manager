"""ワークスペース（データの持ち主ごとの区切り）の最小形。

リーグ・球場・ペナントの世界はワークスペースに属する。ここにあるのは、名前・key・公開範囲の
検査と選択肢、それに権限の判定（`WorkspaceRole`・`WorkspaceAccess`。W2）。メンバーの書き込みは W4 で足す。
Django は import しない。
"""

from __future__ import annotations

import random
import string
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum

from .exceptions import InvalidWorkspace

MAX_NAME_LENGTH = 100
# URL に入れる識別子。10文字の英小文字と数字
KEY_LENGTH = 10
KEY_ALPHABET = string.ascii_lowercase + string.digits


class Visibility(Enum):
    """公開範囲。選択肢の唯一の出典（フォームと ORM の choices はここから写す）。"""

    PUBLIC = "public"
    PRIVATE = "private"

    @property
    def label(self) -> str:
        return _VISIBILITY_LABELS[self]

    @classmethod
    def choices(cls) -> list[tuple[str, str]]:
        """(値, 表示名) の並び。"""
        return [(member.value, member.label) for member in cls]


_VISIBILITY_LABELS = {
    Visibility.PUBLIC: "公開",
    Visibility.PRIVATE: "非公開",
}


def generate_workspace_key(rng: random.Random) -> str:
    """key を作る。乱数源を引数で受け取るので、同じ乱数源なら同じ key になる。

    本番では推測されないよう `random.SystemRandom()` を渡すこと（テストだけ固定シードを使う）。
    """
    return "".join(rng.choice(KEY_ALPHABET) for _ in range(KEY_LENGTH))


def validate_workspace_key(key: str) -> str:
    """key の形（10文字の英小文字と数字）を検査して返す。"""
    if not isinstance(key, str) or len(key) != KEY_LENGTH or any(c not in KEY_ALPHABET for c in key):
        raise InvalidWorkspace(f"ワークスペースの key は{KEY_LENGTH}文字の英小文字と数字にしてください。")
    return key


@dataclass
class Workspace:
    """ワークスペース。持ち主が全権を持つ、データの区切り。"""

    name: str
    key: str
    owner_id: int
    # 既定値は持たせない（新規作成の既定は W4 で決める。既存データの移行は PUBLIC）
    visibility: Visibility
    id: int | None = None

    def __post_init__(self) -> None:
        self.name = (self.name or "").strip()
        if not self.name:
            raise InvalidWorkspace("ワークスペースの名前を入力してください。")
        if len(self.name) > MAX_NAME_LENGTH:
            raise InvalidWorkspace(f"ワークスペースの名前は{MAX_NAME_LENGTH}文字以内にしてください。")
        validate_workspace_key(self.key)
        if isinstance(self.owner_id, bool) or not isinstance(self.owner_id, int) or self.owner_id < 1:
            raise InvalidWorkspace("ワークスペースの持ち主が正しくありません。")
        if not isinstance(self.visibility, Visibility):
            raise InvalidWorkspace("公開範囲が正しくありません。")

    def __str__(self) -> str:
        return self.name


class WorkspaceRole(Enum):
    """ワークスペースの中での立場。保存しない（持ち主・編集者の行・`Team.managers` から毎回導く）。

    表示名もここが出典（ヘッダーのバッジ・`/` の一覧）。テンプレートに並べ直さない。
    """

    OWNER = "owner"
    EDITOR = "editor"
    TEAM_MANAGER = "team_manager"
    VIEWER = "viewer"
    NONE = "none"

    @property
    def label(self) -> str:
        return _ROLE_LABELS[self]

    @property
    def is_member(self) -> bool:
        """持ち主・編集者・チーム担当のどれか（閲覧者と関係なしはメンバーではない）。"""
        return self in _MEMBER_ROLES

    @classmethod
    def of(
        cls,
        *,
        owner_id: int,
        visibility: Visibility,
        user_id: int | None,
        is_editor: bool,
        manages_a_team: bool,
    ) -> WorkspaceRole:
        """表の事実から役割を導く。優先は 持ち主 > 編集者 > チーム担当 > 閲覧。

        未ログイン（`user_id` が None）とログインした部外者は、公開なら閲覧者、非公開なら関係なし。
        **サイト側の `is_staff` は引数に無い**（管理画面に入れることは、ワークスペースの権限に何も足さない）。
        """
        if not isinstance(visibility, Visibility):
            raise InvalidWorkspace("公開範囲が正しくありません。")
        if user_id is None:
            if is_editor or manages_a_team:
                raise InvalidWorkspace("ログインしていない利用者を、編集者やチーム担当にはできません。")
        elif user_id == owner_id:
            return cls.OWNER
        elif is_editor:
            return cls.EDITOR
        elif manages_a_team:
            return cls.TEAM_MANAGER
        return cls.VIEWER if visibility is Visibility.PUBLIC else cls.NONE


_ROLE_LABELS = {
    WorkspaceRole.OWNER: "持ち主",
    WorkspaceRole.EDITOR: "編集者",
    WorkspaceRole.TEAM_MANAGER: "チーム担当",
    WorkspaceRole.VIEWER: "閲覧",
    WorkspaceRole.NONE: "関係なし",
}
_MEMBER_ROLES = frozenset({WorkspaceRole.OWNER, WorkspaceRole.EDITOR, WorkspaceRole.TEAM_MANAGER})
_EDIT_ALL_ROLES = frozenset({WorkspaceRole.OWNER, WorkspaceRole.EDITOR})


@dataclass(frozen=True)
class WorkspaceAccess:
    """1回の要求で、その人がそのワークスペースで何をできるか（値オブジェクト）。

    - 閲覧: 公開なら誰でも、非公開ならメンバー（持ち主・編集者・チーム担当）だけ。
    - 全体の編集（チームを問わない書き込み。ペナントの世界の作成も）: 持ち主と編集者。
    - チームの編集: 全体を編集できる人と、渡したチームのどれか1つの担当（試合は2チームにまたがる）。
    - メンバーの管理・ワークスペースの削除: 持ち主だけ（使うのは W4）。

    `managed_team_ids` はチーム担当のときだけ持つ（持ち主・編集者はチームを問わず編集できるので読まない）。
    渡されたチームがこのワークスペースの実データのチームかどうかは見ない（範囲で絞ったリポジトリが止める）。
    作り方は `of()`。直接作るときも、役割と公開範囲・担当チームの組み合わせを検査する。
    """

    role: WorkspaceRole
    visibility: Visibility
    managed_team_ids: frozenset[int] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not isinstance(self.role, WorkspaceRole):
            raise InvalidWorkspace("ワークスペースの役割が正しくありません。")
        if not isinstance(self.visibility, Visibility):
            raise InvalidWorkspace("公開範囲が正しくありません。")
        if not isinstance(self.managed_team_ids, frozenset) or any(
            isinstance(team_id, bool) or not isinstance(team_id, int) for team_id in self.managed_team_ids
        ):
            raise InvalidWorkspace("担当チームの id が正しくありません。")
        if (self.role is WorkspaceRole.TEAM_MANAGER) != bool(self.managed_team_ids):
            raise InvalidWorkspace("担当チームを持つのはチーム担当だけで、チーム担当は1つ以上のチームを持ちます。")
        if self.role is WorkspaceRole.VIEWER and self.visibility is not Visibility.PUBLIC:
            raise InvalidWorkspace("非公開のワークスペースに、メンバーでない閲覧者はいません。")
        if self.role is WorkspaceRole.NONE and self.visibility is not Visibility.PRIVATE:
            raise InvalidWorkspace("公開のワークスペースは、誰でも閲覧できます。")

    @classmethod
    def of(
        cls,
        *,
        owner_id: int,
        visibility: Visibility,
        user_id: int | None,
        is_editor: bool,
        managed_team_ids: Iterable[int],
    ) -> WorkspaceAccess:
        """表の事実（持ち主・編集者の行の有無・担当チームの id）から組み立てる。役割の決め方は `WorkspaceRole.of`。

        持ち主・編集者が担当を兼ねていても、担当チームは持たない（全体を編集できるので要らない）。
        """
        team_ids = frozenset(managed_team_ids)
        role = WorkspaceRole.of(
            owner_id=owner_id,
            visibility=visibility,
            user_id=user_id,
            is_editor=is_editor,
            manages_a_team=bool(team_ids),
        )
        return cls(
            role=role,
            visibility=visibility,
            managed_team_ids=team_ids if role is WorkspaceRole.TEAM_MANAGER else frozenset(),
        )

    @property
    def is_member(self) -> bool:
        return self.role.is_member

    def can_view(self) -> bool:
        """このワークスペースの画面を読めるか。"""
        return self.role is not WorkspaceRole.NONE

    def can_edit_all(self) -> bool:
        """チームを問わず、このワークスペースのデータを編集できるか（持ち主・編集者）。"""
        return self.role in _EDIT_ALL_ROLES

    def can_edit_teams(self, team_ids: Iterable[int]) -> bool:
        """渡したチームのどれか1つでも編集できるか（試合はどちらかのチームの担当なら編集できる）。

        持ち主・編集者は常に可。チーム担当は、渡したチームに担当のチームがあるときだけ。
        """
        if self.can_edit_all():
            return True
        return not self.managed_team_ids.isdisjoint(team_ids)

    def can_create_world(self) -> bool:
        """ペナントの世界を作れるか。全体の編集権と同じ（2026-10-24 決定）。

        世界の作成はワークスペースへの書き込みなので、公開ワークスペースを見に来ただけの人には許さない。
        進める・締める・編成・削除は世界を作った人が決める（ここでは判定しない）。
        """
        return self.can_edit_all()

    def can_manage_members(self) -> bool:
        """編集者・チーム担当の招待と除名ができるか（持ち主だけ。W4）。"""
        return self.role is WorkspaceRole.OWNER

    def can_delete(self) -> bool:
        """ワークスペースを削除できるか（持ち主だけ。W4）。"""
        return self.role is WorkspaceRole.OWNER
