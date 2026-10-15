"""世界（ペナントモードのセーブデータ）と、その範囲。

実データと世界は同じテーブルを使い、**リーグが属する世界で分ける**（`League.world`。
空なら実データ）。球団・選手・試合はリーグをたどって世界が決まるので、世界の出典は
リーグの1か所だけになる。

読み書きの入口（リポジトリ・参照クエリ）は `WorldScope` を**必須で**受け取る。
「渡さなければ全世界を読む」という経路を作らないための値オブジェクトで、
渡し忘れは型検査で落ちる。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from ..exceptions import InvalidWorkspace, InvalidWorld
from ..value_objects import Season

MAX_NAME_LENGTH = 100
# 乱数のシード。永続化先（64ビット符号つき整数）に収まる範囲
MAX_SEED = 2**63 - 1
# 世界の元にできる実データのリーグの数（8リーグ48球団で1シーズン約3,400試合）
MAX_SOURCE_LEAGUES = 8
# 1人のオーナーが持てる世界の数。1世界10シーズンで約190万行（8リーグなら約760万行）になるため、
# 無制限にするとデータベースがオーナー1人に占められる（判断15。Wiki「ペナントモードの仕組み」の「決めたこと」）
MAX_WORLDS_PER_OWNER = 5
# 1つの世界で遊べるシーズン数の上限（開幕年を1シーズン目と数える）。最後のシーズンは締められない
MAX_SEASONS_PER_WORLD = 10
# 開幕年の上限。最後のシーズン（開幕年 + 上限 − 1）が `Season` の範囲に収まる年。
# **世界を作るときだけ**検査する（保存済みの世界を読み戻せなくしないため）
MAX_START_YEAR = Season.MAX_YEAR - MAX_SEASONS_PER_WORLD + 1


def earliest_start_year(birth_dates: Iterable[date | None]) -> int:
    """分岐元の選手の生年月日と矛盾しない、いちばん早い開幕年。

    開幕年に生まれた選手は開幕日にまだ年齢を数えられないので、最も遅い生年の翌年を下限にする
    （開幕日が年のいつでも成り立つ保守的な値）。生年月日が無い選手は数えない。
    """
    years = [birth.year + 1 for birth in birth_dates if birth is not None]
    return max([Season.MIN_YEAR, *years])


def _is_valid_id(value: object) -> bool:
    """行の id として成り立つか（1以上の int。bool は int の派生だが id ではないので弾く）。"""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


@dataclass(frozen=True)
class WorldScope:
    """どのワークスペースの、どの世界を読み書きするかの範囲。

    `world_id` が None なら**そのワークスペースの実データ**（ほかのワークスペースの実データは含まない）。
    ペナントの世界は、属するワークスペースの id も持つ（球場のようにワークスペースで引くものがあるため）。
    **既定値は持たせない**（「指定しなければ実データ」「指定しなければ全ワークスペース」と
    読めるコードが、ほかの範囲の行を画面に出す事故のもとになるため）。作り方は `real()`・`pennant()` の2つ。
    """

    workspace_id: int
    world_id: int | None

    def __post_init__(self) -> None:
        if not _is_valid_id(self.workspace_id):
            raise InvalidWorkspace("ワークスペースの id が正しくありません。")
        if self.world_id is not None and not _is_valid_id(self.world_id):
            raise InvalidWorld("世界の id が正しくありません。")

    @classmethod
    def real(cls, workspace_id: int) -> WorldScope:
        """そのワークスペースの実データ（どの世界にも属さないリーグ）。"""
        return cls(workspace_id=workspace_id, world_id=None)

    @classmethod
    def pennant(cls, workspace_id: int, world_id: int) -> WorldScope:
        """そのワークスペースに属するペナントの世界ひとつ。"""
        if world_id is None:
            raise InvalidWorld("世界の id が正しくありません。")
        return cls(workspace_id=workspace_id, world_id=world_id)

    @property
    def is_real(self) -> bool:
        return self.world_id is None

    @property
    def is_pennant(self) -> bool:
        return self.world_id is not None

    def __str__(self) -> str:
        if self.world_id is None:
            return f"ワークスペース {self.workspace_id} の実データ"
        return f"ワークスペース {self.workspace_id} の世界 {self.world_id}"


@dataclass
class World:
    """世界。ペナントモードのセーブデータひとつぶん。

    持つのは作ったときに決まる事実だけ。「今日」や「シーズン中か」は持たない
    （日程と試合から導けるので、持つと食い違う）。
    """

    name: str
    seed: int
    start_year: int
    id: int | None = None
    # 遊ぶユーザー。ペナントの書き込みはこの人だけに許す
    owner_id: int | None = None
    # 受け持つ球団。世界を作ったあとで決めてよい
    managed_team_id: int | None = None
    # 属するワークスペース。W1a では保存時に埋まる（未設定なら暫定のワークスペース）。
    # W1b で範囲から渡す形に替える
    workspace_id: int | None = None

    def __post_init__(self) -> None:
        self.name = (self.name or "").strip()
        if not self.name:
            raise InvalidWorld("世界の名前を入力してください。")
        if len(self.name) > MAX_NAME_LENGTH:
            raise InvalidWorld(f"世界の名前は{MAX_NAME_LENGTH}文字以内にしてください。")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or not (0 <= self.seed <= MAX_SEED):
            raise InvalidWorld(f"乱数のシードは 0〜{MAX_SEED} の整数にしてください。")
        # 開幕年は在籍の加入年になる。年として成り立たない値はここで弾く
        self.start_year = Season(self.start_year).year

    def __str__(self) -> str:
        return self.name

    @property
    def scope(self) -> WorldScope:
        """この世界を読み書きする範囲（属するワークスペースを運ぶ）。

        保存前（id が無い）の世界と、ワークスペースが決まっていない世界には無い。
        """
        if self.id is None:
            raise InvalidWorld("保存していない世界には範囲がありません。")
        if self.workspace_id is None:
            raise InvalidWorld("ワークスペースに属していない世界には範囲がありません。")
        return WorldScope.pennant(self.workspace_id, self.id)
