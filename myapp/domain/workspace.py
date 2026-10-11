"""ワークスペース（データの持ち主ごとの区切り）の最小形。

リーグ・球場・ペナントの世界はワークスペースに属する。ここにあるのは、名前・key・公開範囲の
検査と選択肢だけで、権限（`WorkspaceAccess`）やメンバーは後の段階で足す。Django は import しない。
"""

from __future__ import annotations

import random
import string
from dataclasses import dataclass
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
