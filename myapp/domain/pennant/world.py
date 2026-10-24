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
from .draft_order import DraftOrderRule
from .postseason import PostseasonFormat

MAX_NAME_LENGTH = 100
# 乱数のシード。永続化先（64ビット符号つき整数）に収まる範囲
MAX_SEED = 2**63 - 1
# 世界の元にできる実データのリーグの数（8リーグ48球団で1シーズン約3,400試合）
MAX_SOURCE_LEAGUES = 8
# 1人のオーナーが持てる世界の数。1世界10シーズンで約190万行（8リーグなら約760万行）になるため、
# 無制限にするとデータベースがオーナー1人に占められる（判断15。Wiki「ペナントモードの仕組み」の「決めたこと」）
MAX_WORLDS_PER_OWNER = 5
# 上限に達したときの文言（作成の拒否と、世界の一覧で作成フォームの代わりに出す理由の出典）。
# 上限は1人あたりなので、ほかのワークスペースの世界も数える（ワークスペースごとの上限への読み替えは W6）
WORLD_LIMIT_MESSAGE = (
    f"作れる世界は1人{MAX_WORLDS_PER_OWNER}つまでです（ほかのワークスペースの世界も数えます）。"
    "不要な世界を削除してから作ってください。"
)
# 1つの世界で遊べるシーズン数の上限（開幕年を1シーズン目と数える）。最後のシーズンは締められない
MAX_SEASONS_PER_WORLD = 10
# 開幕年の上限。最後のシーズン（開幕年 + 上限 − 1）が `Season` の範囲に収まる年。
# **世界を作るときだけ**検査する（保存済みの世界を読み戻せなくしないため）
MAX_START_YEAR = Season.MAX_YEAR - MAX_SEASONS_PER_WORLD + 1


def league_priority(league_ids: Iterable[int], *, year: int, start_year: int) -> list[int]:
    """その年のリーグの優先順。**リーグの優先順の唯一の出典**。

    日本シリーズの本拠地と種の同率と、ドラフト会議の「球団順位の逆順」の段の中の並び（#198）に使う。

    リーグを id の順に並べ、`(年 − 開幕年) mod リーグ数` だけ左に回す。2リーグなら年ごとに交互で、
    開幕年はいちばん id の小さいリーグが先。3リーグ以上は1年に1つずつ先頭が入れ替わる。
    """
    ids = sorted(set(league_ids))
    if not ids:
        return []
    shift = (year - start_year) % len(ids)
    return ids[shift:] + ids[:shift]


def postseason_format_change_blocker(*, started: bool, world_over: bool) -> str | None:
    """ポストシーズンの方式を変えられない理由。変えられるなら None。**方式を変えられる条件の唯一の出典**。

    - `world_over`: 最終シーズンを終えた世界（これ以上シーズンが無いので、変えても使われない）
    - `started`: いまの年度のポストシーズンの試合がある（途中で方式が変わると、勝ち上がりが導けなくなる）。
      締めると年度が翌年に進むので、締めた後は変えられる（締めた年の勝ち上がりは、その年の試合から導く）

    公式戦の途中・ポストシーズンの開始前は変えられる（その年のポストシーズンから新しい方式になる）。
    """
    if world_over:
        return "この世界は最終シーズンを終えているので、ポストシーズンの方式は変えられません。"
    if started:
        return "今年のポストシーズンが始まっているので、方式は変えられません。"
    return None


def draft_order_rule_change_blocker(*, world_over: bool) -> str | None:
    """ドラフトの指名順の規則を変えられない理由。変えられるなら None。**規則を変えられる条件の唯一の出典**。

    - `world_over`: 最終シーズンを終えた世界（これ以上ドラフトが無いので、変えても使われない）

    会議はシーズンを締める処理の中で全自動で行う（#198 D1）ので、締める前ならいつでも変えられる（その年のオフの会議から
    新しい規則になる）。GM が1巡ずつ進める会議（D3）を入れたら、「その年の会議が始まっている」を足す。
    """
    if world_over:
        return "この世界は最終シーズンを終えているので、ドラフトの指名順は変えられません。"
    return None


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

    持つのは作ったときに決まる事実と、オーナーが変えられる設定（ポストシーズンの方式・ドラフトの指名順の規則）だけ。
    「今日」や「シーズン中か」・ポストシーズンの勝ち上がりは持たない（日程と試合から導けるので、持つと食い違う）。
    """

    name: str
    seed: int
    start_year: int
    id: int | None = None
    # 遊ぶユーザー。ペナントの書き込みはこの人だけに許す
    owner_id: int | None = None
    # 受け持つ球団。世界を作ったあとで決めてよい
    managed_team_id: int | None = None
    # 属するワークスペース。作るときに範囲のワークスペースを渡す（`PennantWorldService`）。
    # 空のままの世界は保存できない（`DjangoWorldRepository.save` が `InvalidWorld` で拒む）
    workspace_id: int | None = None
    # ポストシーズンの方式（`PostseasonFormat` が選択肢の出典）。既定の「なし」は、方式を足す前に作った世界と同じ進み方
    postseason_format: PostseasonFormat = PostseasonFormat.NONE
    # ドラフトの指名順の規則（`DraftOrderRule` が選択肢の出典）。既定は NPB 方式
    # （規則を足す前に作った世界も NPB 方式になる）
    draft_order_rule: DraftOrderRule = DraftOrderRule.NPB

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
        if not isinstance(self.postseason_format, PostseasonFormat):
            raise InvalidWorld("ポストシーズンの方式が正しくありません。")
        if not isinstance(self.draft_order_rule, DraftOrderRule):
            raise InvalidWorld("ドラフトの指名順の規則が正しくありません。")

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
