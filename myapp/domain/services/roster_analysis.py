"""戦力分析（球団×年度の編成）の判定規則。

デプス表の区分（先発・救援、守備位置のグループ）、左右の列、年齢の帯は
野球の編成の見方そのものなので、画面ではなくここに置く。集計結果は保存せず、
試合・在籍・生年月日から毎回導く。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum

from ..value_objects import FieldingPosition, Handedness, JerseyNumber, Position, Profile

# 年齢の帯の両端。範囲の外は「18歳以下」「40歳以上」にまとめる
MIN_AGE_BAND = 18
MAX_AGE_BAND = 40
UNKNOWN_AGE_LABEL = "不明"


class PitcherRole(Enum):
    """投手のその年の役割。登板に占める先発の割合で決める。"""

    STARTER = "先発"
    RELIEVER = "救援"
    NOT_APPEARED = "登板なし"

    @classmethod
    def of(cls, games: int, starts: int) -> PitcherRole:
        """登板のうち半分以上が先発なら先発、それ未満なら救援。登板が無ければ登板なし。"""
        if games <= 0:
            return cls.NOT_APPEARED
        return cls.STARTER if starts * 2 >= games else cls.RELIEVER


class FielderGroup(Enum):
    """野手の守備位置のグループ。守備位置との対応はここが唯一の出典。"""

    CATCHER = "捕手"
    MIDDLE_INFIELD = "二遊間"
    CORNER_INFIELD = "一塁・三塁"
    OUTFIELD = "外野"
    DESIGNATED_HITTER = "指名打者"
    NOT_FIELDED = "守備出場なし"

    @property
    def positions(self) -> tuple[FieldingPosition, ...]:
        """このグループに入る守備位置。守備出場なしはどの位置も持たない。"""
        return _GROUP_POSITIONS[self]

    @classmethod
    def of(cls, position: FieldingPosition | None) -> FielderGroup:
        """守備位置が属するグループ。位置が無い（投手・代打・代走だけ含む）なら守備出場なし。"""
        for group, positions in _GROUP_POSITIONS.items():
            if position in positions:
                return group
        return cls.NOT_FIELDED


_GROUP_POSITIONS: dict[FielderGroup, tuple[FieldingPosition, ...]] = {
    FielderGroup.CATCHER: (FieldingPosition.CATCHER,),
    FielderGroup.MIDDLE_INFIELD: (FieldingPosition.SECOND_BASE, FieldingPosition.SHORTSTOP),
    FielderGroup.CORNER_INFIELD: (FieldingPosition.FIRST_BASE, FieldingPosition.THIRD_BASE),
    FielderGroup.OUTFIELD: (FieldingPosition.LEFT_FIELD, FieldingPosition.CENTER_FIELD, FieldingPosition.RIGHT_FIELD),
    FielderGroup.DESIGNATED_HITTER: (FieldingPosition.DESIGNATED_HITTER,),
    FielderGroup.NOT_FIELDED: (),
}


def primary_position(
    starts: Mapping[FieldingPosition, int], appearances: Mapping[FieldingPosition, int]
) -> FieldingPosition | None:
    """主な守備位置。守備に就く位置と指名打者のうち、先発が最多の位置。

    先発が1度も無ければ出場数が最多の位置。同数なら `FieldingPosition` の宣言順。
    投手・代打・代走は対象外（野手のデプス表で見たいのは守備の居場所のため）。
    どの位置にも出ていなければ None。
    """
    # 対象の位置は区分の対応から導く（区分を増減したときに、主な守備位置がどの区分にも入らないずれを防ぐ）
    grouped = {position for group in FielderGroup for position in group.positions}
    candidates = [position for position in FieldingPosition if position in grouped]
    for counts in (starts, appearances):
        # 宣言順に見て、より多いときだけ入れ替える（同数は先に見た宣言順の早い方が残る）
        best: FieldingPosition | None = None
        for position in candidates:
            if counts.get(position, 0) > (counts.get(best, 0) if best else 0):
                best = position
        if best is not None:
            return best
    return None


def hand_of(profile: Profile, *, is_pitcher: bool) -> Handedness | None:
    """左右の判定に使う利き。投手は投げ、野手は打ち。未設定は None。"""
    return profile.throws if is_pitcher else profile.bats


def hand_label(hand: Handedness | None, *, is_pitcher: bool) -> str:
    """列の見出し。投手は「左腕・右腕」、野手は「左打・両打・右打」。"""
    if hand is None:
        return "不明"
    if is_pitcher:
        return {Handedness.LEFT: "左腕", Handedness.RIGHT: "右腕", Handedness.BOTH: "両投"}[hand]
    return f"{hand.label}打"


def hand_columns(hands: Iterable[Handedness | None], *, is_pitcher: bool) -> list[Handedness | None]:
    """デプス表の左右の列。順は左・両・右。

    投手の両投はほぼ居ないので該当者がいるときだけ、野手の両打は独立した列として常に出す。
    不明（None）は該当者がいるときだけ末尾に足す。
    """
    present = set(hands)
    always = (
        [Handedness.LEFT, Handedness.RIGHT] if is_pitcher else [Handedness.LEFT, Handedness.BOTH, Handedness.RIGHT]
    )
    columns: list[Handedness | None] = [
        hand for hand in (Handedness.LEFT, Handedness.BOTH, Handedness.RIGHT) if hand in always or hand in present
    ]
    if None in present:
        columns.append(None)
    return columns


def depth_order(starts: int, games: int, number: str) -> tuple[int, int, int]:
    """区分の中の並び。先発数の多い順、出場数の多い順、背番号順（00 → 0 → 1 …。`JerseyNumber.sort_key`）。"""
    return (-starts, -games, JerseyNumber(number).sort_key)


# 起用マップの箱1つに出す人数の上限（先発ローテーション6人が収まる数）。残りは「ほか n 名」にまとめる
MAX_PLAYERS_IN_BOX = 6


def usage_map_positions() -> tuple[FieldingPosition, ...]:
    """起用マップに箱を作る守備位置。投手と、野手の区分に入る位置（捕〜右・指名打者）。

    宣言順。野手の側は区分の対応から導く（区分を増減したときに箱がずれないように）。
    """
    fielder_positions = {position for group in FielderGroup for position in group.positions}
    return tuple(p for p in FieldingPosition if p is FieldingPosition.PITCHER or p in fielder_positions)


def usage_visible_count(total: int) -> int:
    """起用マップの箱に出す人数。上位 `MAX_PLAYERS_IN_BOX` 人まで。

    箱の中は先発数の多い順（`depth_order`）に並べてあるので、先頭から数えた人数がそのまま出す人になる。
    控えが1〜2試合ずつ先発しただけでも全員並べると、箱が長くなって主な起用が読めなくなるため上限を置く。
    """
    return min(total, MAX_PLAYERS_IN_BOX)


class ColorAxis(Enum):
    """戦力分析で選手を何で色分けするか。軸の語彙と既定（左右）はここが唯一の出典。

    色分けはページ全体（デプス表・起用マップ・入退団）で同じ規則・同じ色にする。
    """

    HAND = "hand"
    NATURAL = "natural"

    @property
    def label(self) -> str:
        return _COLOR_AXIS_LABELS[self]

    @classmethod
    def parse(cls, key: str | None) -> ColorAxis:
        """画面の `?color=` の値から軸を決める。不正・未指定は左右。"""
        for axis in cls:
            if axis.value == key:
                return axis
        return cls.HAND


_COLOR_AXIS_LABELS = {
    ColorAxis.HAND: "左右",
    ColorAxis.NATURAL: "本職",
}


@dataclass(frozen=True)
class ColorCategory:
    """色分けの区分1つ。key は CSS のクラス名の一部、mark は色に頼らないための短い印（無ければ空）。"""

    key: str
    label: str
    mark: str = ""


# 判定に使う守備位置が無いときの区分。色も印も付けない
NEUTRAL_CATEGORY = ColorCategory("neutral", "対象外")

_HAND_CATEGORIES: dict[Handedness | None, ColorCategory] = {
    Handedness.LEFT: ColorCategory("hand-left", "左", "左"),
    Handedness.BOTH: ColorCategory("hand-both", "両", "両"),
    Handedness.RIGHT: ColorCategory("hand-right", "右", "右"),
    None: ColorCategory("hand-unknown", "不明", "？"),
}
_NATURAL_CATEGORIES = (ColorCategory("natural-yes", "本職"), ColorCategory("natural-no", "本職外", "他"))


def color_categories(axis: ColorAxis) -> tuple[ColorCategory, ...]:
    """軸の区分の一覧（対象外を除く）。凡例の並び順。"""
    if axis is ColorAxis.HAND:
        return tuple(_HAND_CATEGORIES.values())
    return _NATURAL_CATEGORIES


def color_category(
    axis: ColorAxis,
    *,
    registered: Position,
    profile: Profile,
    fielding_position: FieldingPosition | None,
) -> ColorCategory:
    """選手が軸 axis のどの区分に入るか。ページのどの場所でもこの1つの関数で決める。

    場所ごとに渡すのは「どの守備位置で見るか」だけ。左右は、守備位置があればそれが投のとき、
    無ければ登録位置が投手のときに投げる手で見て、それ以外は打席で見る。
    - fielding_position: 本職かどうかを見る守備位置（起用マップは箱の位置、デプス表は主な守備位置）。
      守備位置が無い（登板なし・守備出場なし・入退団の表）ときは None で、本職の軸では対象外
    """
    if axis is ColorAxis.HAND:
        is_pitcher = (
            registered.is_pitcher if fielding_position is None else fielding_position is FieldingPosition.PITCHER
        )
        return _HAND_CATEGORIES[hand_of(profile, is_pitcher=is_pitcher)]
    if fielding_position is None:
        return NEUTRAL_CATEGORY
    return _NATURAL_CATEGORIES[0 if registered.is_natural_at(fielding_position) else 1]


def age_band(age: int | None) -> str:
    """年齢の帯。18歳以下・19〜39歳は1歳刻み・40歳以上。年齢が不明なら「不明」。"""
    if age is None:
        return UNKNOWN_AGE_LABEL
    if age <= MIN_AGE_BAND:
        return f"{MIN_AGE_BAND}歳以下"
    if age >= MAX_AGE_BAND:
        return f"{MAX_AGE_BAND}歳以上"
    return f"{age}歳"


@dataclass(frozen=True)
class AgeBandCount:
    """年齢の帯ごとの人数。"""

    band: str
    pitchers: int
    fielders: int

    @property
    def total(self) -> int:
        return self.pitchers + self.fielders


def age_distribution(pitcher_ages: Iterable[int | None], fielder_ages: Iterable[int | None]) -> list[AgeBandCount]:
    """年齢の帯ごとの投手・野手の人数。

    いちばん若い帯からいちばん年上の帯までを、人数が0の帯も含めて途切れなく並べる
    （分布の形を見るため）。年齢が不明な選手がいれば末尾に「不明」を足す。
    誰もいなければ空。
    """
    pitchers, fielders = list(pitcher_ages), list(fielder_ages)
    known = [age for age in (*pitchers, *fielders) if age is not None]
    rows: list[AgeBandCount] = []
    if known:
        # 帯の端の外は端の帯に畳むので、帯の番号も同じ範囲に収める
        low, high = (min(max(age, MIN_AGE_BAND), MAX_AGE_BAND) for age in (min(known), max(known)))
        for age in range(low, high + 1):
            label = age_band(age)
            rows.append(
                AgeBandCount(
                    band=label,
                    pitchers=sum(1 for a in pitchers if a is not None and age_band(a) == label),
                    fielders=sum(1 for a in fielders if a is not None and age_band(a) == label),
                )
            )
    unknown = AgeBandCount(
        band=UNKNOWN_AGE_LABEL,
        pitchers=sum(1 for a in pitchers if a is None),
        fielders=sum(1 for a in fielders if a is None),
    )
    if unknown.total:
        rows.append(unknown)
    return rows


def average_age(ages: Iterable[int | None]) -> float | None:
    """平均年齢。帯ではなく実際の年齢の合計から求める。年齢が分かる人がいなければ None。"""
    known = [age for age in ages if age is not None]
    return sum(known) / len(known) if known else None


class MoveKind(Enum):
    """入退団の区分。語彙と並び（宣言順）はここが唯一の出典。

    加入は新入団・移籍・再入団、退団は移籍・退団のどれか。
    """

    NEW_SIGNING = "新入団"
    TRANSFER = "移籍"
    REJOINED = "再入団"
    DEPARTED = "退団"


@dataclass(frozen=True)
class StintSpan:
    """判定に使う在籍1件。to_year は最後に在籍した年（含む）で、空なら在籍中。"""

    stint_id: int
    team_id: int
    from_year: int
    to_year: int | None

    @property
    def order_key(self) -> tuple[int, bool, int, int]:
        """在籍の前後を決める並び。始まった年。同じ年なら、その年のうちに終わった在籍を先に置く
        （シーズン途中の移籍で、移籍元を先にするため。登録した順＝id に頼ると、経歴を後から足したときに逆になる）。
        最後の決め手は id。
        """
        return (self.from_year, self.to_year is None, self.to_year or 0, self.stint_id)


@dataclass(frozen=True)
class MoveJudgement:
    """入退団の判定結果。other_team_id は移籍のときの前所属（加入）または移籍先（退団）。"""

    kind: MoveKind
    other_team_id: int | None = None


def _is_consecutive(before: StintSpan, after: StintSpan) -> bool:
    """before の後に間を空けず after が始まったか（同じ年か翌年）。加入と退団で同じ窓を使い、
    同じ動きが球団によって移籍にも退団にも見える食い違いを防ぐ。
    before が終わっていない（重なっている）ときも続いているとみなす。
    """
    return before.to_year is None or after.from_year <= before.to_year + 1


def judge_join(own: StintSpan, stints: Iterable[StintSpan]) -> MoveJudgement:
    """加入の区分。stints はその選手の全在籍（own を含んでよい）。

    直前の在籍（自分を除く）が同じチームなら再入団。別のチームで、間を空けず（前年までに終わって）
    続いていれば移籍（その球団が前所属）。それ以外（在籍が無い・間が空いた）は新入団。
    """
    earlier = [s for s in stints if s.stint_id != own.stint_id and s.order_key < own.order_key]
    if not earlier:
        return MoveJudgement(MoveKind.NEW_SIGNING)
    previous = max(earlier, key=lambda s: s.order_key)
    if previous.team_id == own.team_id:
        return MoveJudgement(MoveKind.REJOINED)
    if _is_consecutive(previous, own):
        return MoveJudgement(MoveKind.TRANSFER, previous.team_id)
    return MoveJudgement(MoveKind.NEW_SIGNING)


def judge_leave(own: StintSpan, stints: Iterable[StintSpan]) -> MoveJudgement:
    """退団の区分。own は退団年（to_year）を持つ在籍。

    この在籍の後に間を空けず（同じ年か翌年から）始まる在籍があり、それが別のチームなら移籍（移籍先）。
    それ以外は退団（引退・自由契約などは在籍からは区別できない）。
    """
    later = [
        s for s in stints if s.stint_id != own.stint_id and s.order_key > own.order_key and _is_consecutive(own, s)
    ]
    if later:
        following = min(later, key=lambda s: s.order_key)
        if following.team_id != own.team_id:
            return MoveJudgement(MoveKind.TRANSFER, following.team_id)
    return MoveJudgement(MoveKind.DEPARTED)


def move_order(kind: MoveKind, number: str) -> tuple[int, int]:
    """入退団の表の並び。区分（宣言順）、背番号順（00 → 0 → 1 …。`JerseyNumber.sort_key`）。"""
    return (list(MoveKind).index(kind), JerseyNumber(number).sort_key)
