"""戦力分析（球団×年度の編成）の判定規則。

デプス表の区分（先発・救援、守備位置のグループ）、左右の列、年齢の帯は
野球の編成の見方そのものなので、画面ではなくここに置く。集計結果は保存せず、
試合・在籍・生年月日から毎回導く。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum

from ..value_objects import FieldingPosition, Handedness, Profile

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


def depth_order(starts: int, games: int, number: int) -> tuple[int, int, int]:
    """区分の中の並び。先発数の多い順、出場数の多い順、背番号順。"""
    return (-starts, -games, number)


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
