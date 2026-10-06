"""能力値。シミュレーションに効く項目だけを持つ。

効かない項目を持つと、上げても何も変わらない「飾り」になる。各項目は主に1つの事象に効く。

| 区分 | 項目 | 主に効く事象 |
| --- | --- | --- |
| 野手 | ミート | 三振率↓、インプレーの安打率↑ |
| | パワー | 本塁打率、長打の割合 |
| | 選球眼 | 四球率 |
| | 走力 | 盗塁、安打での余分な進塁、併殺の回避、三塁打 |
| | 守備力 | 失策率、チームの被安打（守備位置で重みを変える） |
| 投手 | 球威 | 奪三振率 |
| | 制球 | 与四球率 |
| | 一発回避 | 被本塁打率 |
| | スタミナ | 先発・救援の受け持ち（対戦打者数の目安。連投の可否には効かない。連投は全員一律に2日続けてまで） |

値は 1〜100 の整数で、50 が1軍の平均。能力は**入力**で、成績は出力（成績から能力は作らない）。
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from enum import Enum
from typing import Any, ClassVar

from ..exceptions import InvalidRatings

RATING_MIN = 1
RATING_MAX = 100
# 1軍の平均。基準値（`LeagueBaseline`）は能力がこの値どうしの対戦に一致する。
AVERAGE_RATING = 50


class GrowthType(Enum):
    """成長型。隠し値で、翌年の能力の伸び方（オフの処理）に使う。"""

    EARLY = "早熟"
    NORMAL = "普通"
    LATE = "晩成"

    @property
    def label(self) -> str:
        return self.value


class RatingEmphasis(Enum):
    """区分の目立たせ方。**どの区分をどう目立たせるかの唯一の出典**で、画面に境目の区分や数値を書かない。"""

    HIGH = "high"  # 強調（S・A）
    MID = "mid"  # やや強調（B・C）
    NONE = "none"  # 目立たせない（D）
    MUTED = "muted"  # 控えめ（E〜G）


class RatingGrade(Enum):
    """能力の区分（S〜G）。**区分の境目の唯一の出典**で、画面に表を複製しない。"""

    S = "S"
    A = "A"
    B = "B"
    C = "C"
    D = "D"
    E = "E"
    F = "F"
    G = "G"

    @property
    def label(self) -> str:
        return self.value

    @property
    def lower_bound(self) -> int:
        """この区分に入る最小の値。G は下限なし（1）。"""
        return _GRADE_FLOORS[self]

    @property
    def emphasis(self) -> RatingEmphasis:
        """画面での目立たせ方。S・A は強調、B・C はやや強調、D は無し、E〜G は控えめ。"""
        return _GRADE_EMPHASIS[self]

    @classmethod
    def from_value(cls, value: int) -> RatingGrade:
        """能力値から区分を引く。S（90〜）A（80〜）B（70〜）C（60〜）D（50〜）E（40〜）F（20〜）G（〜19）。"""
        _require_rating("能力値", value)
        for grade in cls:
            if value >= grade.lower_bound:
                return grade
        return cls.G


_GRADE_FLOORS = {
    RatingGrade.S: 90,
    RatingGrade.A: 80,
    RatingGrade.B: 70,
    RatingGrade.C: 60,
    RatingGrade.D: 50,
    RatingGrade.E: 40,
    RatingGrade.F: 20,
    RatingGrade.G: RATING_MIN,
}


_GRADE_EMPHASIS = {
    RatingGrade.S: RatingEmphasis.HIGH,
    RatingGrade.A: RatingEmphasis.HIGH,
    RatingGrade.B: RatingEmphasis.MID,
    RatingGrade.C: RatingEmphasis.MID,
    RatingGrade.D: RatingEmphasis.NONE,
    RatingGrade.E: RatingEmphasis.MUTED,
    RatingGrade.F: RatingEmphasis.MUTED,
    RatingGrade.G: RatingEmphasis.MUTED,
}


def _require_rating(label: str, value: Any) -> int:
    """1〜100 の整数であることを確かめる。何が来ても検査するのが役目なので Any。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidRatings(f"{label}は整数で指定してください。")
    if not (RATING_MIN <= value <= RATING_MAX):
        raise InvalidRatings(f"{label}は{RATING_MIN}〜{RATING_MAX}の範囲で指定してください（{value}）。")
    return value


@dataclass(frozen=True)
class BatterRatings:
    """野手の能力。"""

    contact: int = AVERAGE_RATING  # ミート
    power: int = AVERAGE_RATING  # パワー
    eye: int = AVERAGE_RATING  # 選球眼
    speed: int = AVERAGE_RATING  # 走力
    fielding: int = AVERAGE_RATING  # 守備力
    growth: GrowthType = GrowthType.NORMAL

    LABELS: ClassVar[dict[str, str]] = {
        "contact": "ミート",
        "power": "パワー",
        "eye": "選球眼",
        "speed": "走力",
        "fielding": "守備力",
    }

    def __post_init__(self) -> None:
        for field in fields(self):
            if field.name in self.LABELS:
                _require_rating(self.LABELS[field.name], getattr(self, field.name))

    @property
    def batting_value(self) -> float:
        """打撃の総合値。控えとの比較・打順の選択に使う。四球は安打ほど得点に効かないので軽くする。"""
        return 0.34 * self.contact + 0.30 * self.power + 0.22 * self.eye + 0.14 * self.speed

    @property
    def on_base_value(self) -> float:
        """出塁の見込み。1・2番の選択に使う。"""
        return 0.45 * self.contact + 0.30 * self.eye + 0.25 * self.speed

    @property
    def slugging_value(self) -> float:
        """長打の見込み。3〜5番の選択に使う。"""
        return 0.60 * self.power + 0.40 * self.contact


@dataclass(frozen=True)
class PitcherRatings:
    """投手の能力。"""

    stuff: int = AVERAGE_RATING  # 球威
    control: int = AVERAGE_RATING  # 制球
    home_run_avoidance: int = AVERAGE_RATING  # 一発回避
    stamina: int = AVERAGE_RATING  # スタミナ
    growth: GrowthType = GrowthType.NORMAL

    LABELS: ClassVar[dict[str, str]] = {
        "stuff": "球威",
        "control": "制球",
        "home_run_avoidance": "一発回避",
        "stamina": "スタミナ",
    }

    def __post_init__(self) -> None:
        for field in fields(self):
            if field.name in self.LABELS:
                _require_rating(self.LABELS[field.name], getattr(self, field.name))

    @property
    def pitching_value(self) -> float:
        """抑える力の総合値。スタミナは含めない（先発と救援のどちらで使うかは別の話）。"""
        return 0.40 * self.stuff + 0.35 * self.control + 0.25 * self.home_run_avoidance

    @property
    def starter_value(self) -> float:
        """先発としての総合値。長いイニングを投げられることが要る。"""
        return 0.85 * self.pitching_value + 0.15 * self.stamina
