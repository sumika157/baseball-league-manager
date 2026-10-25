"""ワークスペース（データの持ち主ごとの区切り）。

リーグ・球場・ペナントの世界はワークスペースに属する。ここにあるのは、名前・key・公開範囲の
検査と選択肢、メンバー（編集者・チーム担当）とその不変条件（`Workspace` 集約。W4）、招待（`Invitation`。W4）、
持てる数の上限（`WorkspaceQuota`。W4）、消してよいか（`workspace_deletion_blocker`。W4）、それに権限の判定（`WorkspaceRole`・`WorkspaceAccess`。W2）。
Django は import しない。
"""

from __future__ import annotations

import hashlib
import random
import string
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

from .exceptions import InvalidInvitation, InvalidWorkspace, MemberNotFound, WorkspaceLimitReached

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


def _clean_name(name: str) -> str:
    """名前の前後の空白を除き、空と長すぎる名前を `InvalidWorkspace` にする。"""
    cleaned = name.strip() if isinstance(name, str) else ""
    if not cleaned:
        raise InvalidWorkspace("ワークスペースの名前を入力してください。")
    if len(cleaned) > MAX_NAME_LENGTH:
        raise InvalidWorkspace(f"ワークスペースの名前は{MAX_NAME_LENGTH}文字以内にしてください。")
    return cleaned


def _is_id(value: object) -> bool:
    """1以上の整数か（真偽値は除く）。利用者・チームの id の検査に使う。"""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _team_id_set(team_ids: Iterable[int]) -> frozenset[int]:
    teams = frozenset(team_ids)
    if not all(_is_id(team_id) for team_id in teams):
        raise InvalidWorkspace("担当チームの id が正しくありません。")
    return teams


def members_from_rows(
    owner_id: int, editor_ids: Iterable[int], manager_rows: Iterable[tuple[int, int]]
) -> tuple[frozenset[int], dict[int, frozenset[int]]]:
    """表の行（編集者の行の利用者・担当の中間表の (チーム, 利用者)）から、集約のメンバーを組み立てる。

    管理画面では、持ち主に編集者や担当の行を、編集者に担当の行を付けられる（W4 より前の運用）。読むときに
    落とさず、役割の優先（持ち主 > 編集者 > チーム担当。`WorkspaceRole.of` と同じ）で上の役割だけを残す。
    重なった下の役割の行は権限に何も足さない行なので、集約を保存すると消える。
    担当の中間表の行は、呼ぶ側がこのワークスペースの実データのチームに絞って渡す。
    """
    editors = frozenset(editor_ids) - {owner_id}
    assignments: dict[int, set[int]] = {}
    for team_id, user_id in manager_rows:
        if user_id == owner_id or user_id in editors:
            continue
        assignments.setdefault(user_id, set()).add(team_id)
    return editors, {user_id: frozenset(teams) for user_id, teams in assignments.items()}


@dataclass
class Workspace:
    """ワークスペース（集約ルート）。持ち主が全権を持つ、データの区切り。

    メンバーは持ち主・編集者（`editor_ids`）・チーム担当（`team_assignments`。利用者 → 担当チームの id）。
    役割は保存せず、この3つから導く（`role_of`）。1人の役割は1つだけで、次を自身で検査する:

    - 持ち主は編集者にも担当にも入らない。編集者は担当に入らない（全体を編集できるので担当は要らない）。
    - チーム担当は1つ以上のチームを持つ。
    - 担当チームがこのワークスペースの実データのチームであることは、保存のときにリポジトリがチームの id の
      集まりを渡して `ensure_assignments_within` で検査する（集約はチームを持たないため）。
    """

    name: str
    key: str
    owner_id: int
    # 既定値は持たせない（作成の画面の既定は非公開。既存データの移行は PUBLIC）
    visibility: Visibility
    id: int | None = None
    editor_ids: frozenset[int] = field(default_factory=frozenset)
    # 担当の向きは「人 → チーム」（メンバーの画面の単位が人なので。保存先の `Team.managers` は「チーム → 人」）
    team_assignments: dict[int, frozenset[int]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.name = _clean_name(self.name)
        validate_workspace_key(self.key)
        if not _is_id(self.owner_id):
            raise InvalidWorkspace("ワークスペースの持ち主が正しくありません。")
        if not isinstance(self.visibility, Visibility):
            raise InvalidWorkspace("公開範囲が正しくありません。")
        editors = frozenset(self.editor_ids)
        if not all(_is_id(user_id) for user_id in editors):
            raise InvalidWorkspace("編集者が正しくありません。")
        assignments = {user_id: _team_id_set(teams) for user_id, teams in dict(self.team_assignments).items()}
        if not all(_is_id(user_id) for user_id in assignments):
            raise InvalidWorkspace("チーム担当が正しくありません。")
        if any(not teams for teams in assignments.values()):
            raise InvalidWorkspace("チーム担当には、担当チームを1つ以上選んでください。")
        if self.owner_id in editors or self.owner_id in assignments:
            raise InvalidWorkspace("持ち主は編集者にもチーム担当にもなれません。持ち主はもともと全権を持っています。")
        if not editors.isdisjoint(assignments):
            raise InvalidWorkspace("編集者はチーム担当を兼ねられません。編集者は全チームを編集できます。")
        self.editor_ids = editors
        self.team_assignments = assignments

    def __str__(self) -> str:
        return self.name

    # --- 名前と公開範囲 ---

    def rename(self, name: str) -> None:
        self.name = _clean_name(name)

    def change_visibility(self, visibility: Visibility) -> None:
        if not isinstance(visibility, Visibility):
            raise InvalidWorkspace("公開範囲が正しくありません。")
        self.visibility = visibility

    # --- メンバー ---

    def role_of(self, user_id: int | None) -> WorkspaceRole:
        """その利用者の役割（`WorkspaceRole.of` と同じ優先）。"""
        return WorkspaceRole.of(
            owner_id=self.owner_id,
            visibility=self.visibility,
            user_id=user_id,
            is_editor=user_id in self.editor_ids,
            manages_a_team=user_id in self.team_assignments,
        )

    def is_member(self, user_id: int | None) -> bool:
        """持ち主・編集者・チーム担当のどれか。"""
        return self.role_of(user_id).is_member

    def make_editor(self, user_id: int) -> None:
        """全体を編集できるメンバーにする（担当だった人は担当から外れる）。"""
        self._ensure_not_owner(user_id)
        self.team_assignments.pop(user_id, None)
        self.editor_ids = self.editor_ids | {user_id}

    def assign_teams(self, user_id: int, team_ids: Iterable[int]) -> None:
        """渡したチームだけの担当にする（担当チームは置き換える。編集者だった人は編集者から外れる）。"""
        self._ensure_not_owner(user_id)
        teams = _team_id_set(team_ids)
        if not teams:
            raise InvalidWorkspace("チーム担当には、担当チームを1つ以上選んでください。")
        self.editor_ids = self.editor_ids - {user_id}
        self.team_assignments[user_id] = teams

    def change_role(self, user_id: int, role: InvitedRole, team_ids: Iterable[int] = ()) -> None:
        """メンバー（編集者・チーム担当）の役割を変える。担当チームの付け替えもこれで行う。

        メンバーでない人は `MemberNotFound`（新しく入れるのは招待の受諾だけ）。持ち主の役割は変えられない。
        """
        self._ensure_not_owner(user_id)
        self._ensure_member(user_id)
        self._grant(user_id, role, team_ids)

    def remove_member(self, user_id: int) -> None:
        """編集者・チーム担当を外す（担当チームもすべて外れる）。持ち主は外せない。"""
        if user_id == self.owner_id:
            raise InvalidWorkspace("持ち主は除名できません。")
        self._ensure_member(user_id)
        self.editor_ids = self.editor_ids - {user_id}
        self.team_assignments.pop(user_id, None)

    def accept(self, invitation: Invitation, user_id: int, *, now: datetime) -> None:
        """招待を受けて、その利用者をメンバーにする。

        ほかのワークスペースの招待・期限切れは `InvalidInvitation`（無いときと同じ文言）。持ち主と、既に
        メンバーの人も `InvalidInvitation`（役割を変えたいなら持ち主が変える。招待は使わずに残る）。
        招待を消す（1回だけ使える）のは呼ぶ側が同じトランザクションで行う。
        """
        if invitation.workspace_id != self.id or invitation.is_expired(now):
            raise InvalidInvitation(INVITATION_UNAVAILABLE)
        if not _is_id(user_id):
            raise InvalidInvitation("招待を受ける利用者が正しくありません。")
        if self.is_member(user_id):
            raise InvalidInvitation("すでにこのワークスペースのメンバーです。")
        self._grant(user_id, invitation.role, invitation.team_ids)

    def ensure_assignments_within(self, team_ids: Iterable[int]) -> None:
        """担当チームが、渡したチーム（このワークスペースの実データのチーム）に収まっているか。"""
        allowed = frozenset(team_ids)
        for teams in self.team_assignments.values():
            if not teams <= allowed:
                raise InvalidWorkspace("担当チームは、このワークスペースのチームから選んでください。")

    def _grant(self, user_id: int, role: InvitedRole, team_ids: Iterable[int]) -> None:
        if role is InvitedRole.EDIT_ALL:
            if frozenset(team_ids):
                raise InvalidWorkspace("全体を編集する役割には、担当チームを選びません。")
            self.make_editor(user_id)
        elif role is InvitedRole.EDIT_TEAMS:
            self.assign_teams(user_id, team_ids)
        else:
            raise InvalidWorkspace("役割が正しくありません。")

    def _ensure_not_owner(self, user_id: int) -> None:
        if not _is_id(user_id):
            raise InvalidWorkspace("利用者が正しくありません。")
        if user_id == self.owner_id:
            raise InvalidWorkspace("持ち主の役割は変えられません。持ち主はもともと全権を持っています。")

    def _ensure_member(self, user_id: int) -> None:
        if user_id not in self.editor_ids and user_id not in self.team_assignments:
            raise MemberNotFound("このワークスペースのメンバーではありません。")


# --- 招待 ---

# 招待の期限（発行から7日）
INVITATION_TTL = timedelta(days=7)
# 保存するのはトークンの SHA-256 のダイジェスト（16進64文字）だけ。トークンそのものは発行の直後に1回だけ見せる
TOKEN_DIGEST_LENGTH = 64
_DIGEST_ALPHABET = frozenset("0123456789abcdef")
# 無い・期限切れ・使用済みの招待は同じ文言（どれかを推し量らせない）
INVITATION_UNAVAILABLE = "招待が見つからないか、期限が切れています。"


class InvitedRole(Enum):
    """招待で渡す役割。選択肢の唯一の出典（フォームと ORM の choices はここから写す）。"""

    EDIT_ALL = "edit_all"
    EDIT_TEAMS = "edit_teams"

    @property
    def label(self) -> str:
        return _INVITED_ROLE_LABELS[self]

    @property
    def granted_role(self) -> WorkspaceRole:
        """受諾した人がなる役割。"""
        return WorkspaceRole.EDITOR if self is InvitedRole.EDIT_ALL else WorkspaceRole.TEAM_MANAGER

    @classmethod
    def choices(cls) -> list[tuple[str, str]]:
        """(値, 表示名) の並び。"""
        return [(member.value, member.label) for member in cls]


_INVITED_ROLE_LABELS = {
    InvitedRole.EDIT_ALL: "全体を編集",
    InvitedRole.EDIT_TEAMS: "担当チームを編集",
}


def invitation_digest(token: str) -> str:
    """招待のトークンから、保存・照合に使うダイジェスト（SHA-256 の16進）を作る。空のトークンは使えない招待。"""
    if not isinstance(token, str) or not token:
        raise InvalidInvitation(INVITATION_UNAVAILABLE)
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Invitation:
    """招待（エンティティ）。期限内に1回だけ使える（受諾したら消す。残すと、メンバーである事実が2か所になる）。

    招待したのは持ち主（招待できるのは持ち主だけ）なので、作った人は持たない（持ち主と同じ事実の出典が2つになる）。
    `EDIT_TEAMS` は担当チームを1つ以上持ち、`EDIT_ALL` はチームを持たない。
    """

    workspace_id: int
    token_digest: str
    role: InvitedRole
    team_ids: frozenset[int]
    expires_at: datetime
    id: int | None = None

    def __post_init__(self) -> None:
        if not _is_id(self.workspace_id):
            raise InvalidInvitation("招待のワークスペースが正しくありません。")
        digest = self.token_digest
        if (
            not isinstance(digest, str)
            or len(digest) != TOKEN_DIGEST_LENGTH
            or any(c not in _DIGEST_ALPHABET for c in digest)
        ):
            raise InvalidInvitation("招待のトークンが正しくありません。")
        if not isinstance(self.role, InvitedRole):
            raise InvalidInvitation("招待の役割が正しくありません。")
        if not isinstance(self.team_ids, frozenset) or not all(_is_id(team_id) for team_id in self.team_ids):
            raise InvalidInvitation("招待の担当チームが正しくありません。")
        if self.role is InvitedRole.EDIT_TEAMS and not self.team_ids:
            raise InvalidInvitation("担当チームを編集する招待には、担当チームを1つ以上選んでください。")
        if self.role is InvitedRole.EDIT_ALL and self.team_ids:
            raise InvalidInvitation("全体を編集する招待には、担当チームを選びません。")
        if not isinstance(self.expires_at, datetime):
            raise InvalidInvitation("招待の期限が正しくありません。")

    @classmethod
    def issue(
        cls, *, workspace_id: int, token: str, role: InvitedRole, team_ids: Iterable[int], now: datetime
    ) -> Invitation:
        """新しい招待。期限は `now` から `INVITATION_TTL`。トークンはダイジェストにして持つ。"""
        return cls(
            workspace_id=workspace_id,
            token_digest=invitation_digest(token),
            role=role,
            team_ids=frozenset(team_ids),
            expires_at=now + INVITATION_TTL,
        )

    def is_expired(self, now: datetime) -> bool:
        """期限を過ぎたか（期限ちょうどは過ぎた扱い）。"""
        return now >= self.expires_at

    def ensure_teams_within(self, team_ids: Iterable[int]) -> None:
        """担当チームが、渡したチーム（招待のワークスペースの実データのチーム）に収まっているか。

        リポジトリが範囲のチームの id を読んで渡す（`Workspace.ensure_assignments_within` と同じ形）。
        """
        if not self.team_ids <= frozenset(team_ids):
            raise InvalidInvitation("担当チームは、このワークスペースのチームから選んでください。")


# --- 上限 ---


@dataclass(frozen=True)
class WorkspaceQuota:
    """1人が持てるワークスペースの数の上限（値は settings から渡す。試合数・世界の数は W6 でここに足す）。

    持っている数は保存しない（アプリケーション層が数えて渡す）。
    """

    max_owned_workspaces: int

    def __post_init__(self) -> None:
        if not isinstance(self.max_owned_workspaces, int) or isinstance(self.max_owned_workspaces, bool):
            raise InvalidWorkspace("ワークスペースの数の上限が正しくありません。")
        if self.max_owned_workspaces < 0:
            raise InvalidWorkspace("ワークスペースの数の上限が正しくありません。")

    @property
    def limit_message(self) -> str:
        return f"ワークスペースは1人{self.max_owned_workspaces}つまで作れます。"

    def can_create_workspace(self, owned_count: int) -> bool:
        """今 `owned_count` 個を持っている人が、もう1つ作れるか。"""
        return owned_count < self.max_owned_workspaces

    def ensure_can_create_workspace(self, owned_count: int) -> None:
        """作れなければ `WorkspaceLimitReached`。"""
        if not self.can_create_workspace(owned_count):
            raise WorkspaceLimitReached(self.limit_message)


# --- 削除 ---


def workspace_deletion_blocker(world_count: int) -> str | None:
    """ワークスペースを消せない理由。ペナントの世界が1つも無いときだけ消せる（2026-10-25 ユーザー決定 2）。

    世界の削除は 10シーズンで約40秒かかり、ワークスペースとまとめて消すとタイムアウトを超えうる。世界は先に
    1つずつ消してもらう。数は保存せず、呼ぶ側（リポジトリ・削除の確認の参照クエリ）が数えて渡す。
    消せるなら None（前例: `setup.league_deletion_blocker`）。
    """
    if isinstance(world_count, bool) or not isinstance(world_count, int) or world_count < 0:
        raise ValueError(f"数は0以上の整数で渡してください（{world_count!r}）。")
    if world_count:
        return (
            f"このワークスペースにはペナントの世界が{world_count}つあるため削除できません。"
            "先に世界をすべて削除してください。"
        )
    return None


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
    - 設定（名前・公開範囲）の変更・メンバーの管理・ワークスペースの削除: 持ち主だけ（使うのは W4）。

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

    def can_set_up(self) -> bool:
        """リーグ・チーム・球場の登録画面を見て、作成・編集・削除ができるか。全体の編集権と同じ（W3。2026-10-25）。

        登録画面はフォームだけの画面なので、書けない人（チーム担当・閲覧）には見せない（中身は既存の画面で読める）。
        """
        return self.can_edit_all()

    def can_change_settings(self) -> bool:
        """名前と公開範囲を変えられるか（持ち主だけ。W4）。"""
        return self.role is WorkspaceRole.OWNER

    def can_manage_members(self) -> bool:
        """編集者・チーム担当の招待と除名ができるか（持ち主だけ。W4）。"""
        return self.role is WorkspaceRole.OWNER

    def can_delete(self) -> bool:
        """ワークスペースを削除できるか（持ち主だけ。W4）。"""
        return self.role is WorkspaceRole.OWNER
