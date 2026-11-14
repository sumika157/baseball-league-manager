"""ペナントのチケットの値段（#208 E2）。**純粋な値と規則**で、DB にも Django にも触れない。

値段は5段階（2026-11-14 ユーザー決定。#371）。段階ごとに、1人あたりの単価の倍率・ホーム試合の入場率の平均への効き・
翌年の人気への効き（年あたり）を持つ。**値段の選択肢の出典はここだけ**（画面・モデルに複製しない）。

- 上げると客単価が上がるが、入場率と人気が下がる。満員に近い人気球団ほど上げ得になる
- 人気への効きは下げより上げのほうが大きい（値上げで一時的に儲けて人気を食いつぶす手が強すぎないように）
- AI（と、GM の球団で手動の値段が無い年＝お任せ）は、その年の人気で決める（`ai_ticket_price`。乱数なし・保存しない）
- GM が手動にした値段は球団 × 年の行（`ClubTicketPrice`）として保存する。変えられるのはその年の開幕前だけで、
  翌年には引き継がない（新しい年はお任せから。2026-11-14 ユーザー決定）
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum

from ..exceptions import InvalidTicketPrice
from ..value_objects import Season

# 1人あたりの平均の単価（標準の値段。円）。NPB の水準（2026-11-14 ユーザー決定）
BASE_TICKET_PRICE = 4_000


class TicketPrice(Enum):
    """チケットの値段の段階。**値段の選択肢の唯一の出典**。値は画面に出す名前。並びは安い順。"""

    LOW = "安い"
    SLIGHTLY_LOW = "やや安い"
    STANDARD = "標準"
    SLIGHTLY_HIGH = "やや高い"
    HIGH = "高い"

    @property
    def label(self) -> str:
        return self.value

    @property
    def multiplier(self) -> float:
        """標準の単価（`BASE_TICKET_PRICE`）に掛ける倍率。"""
        return _EFFECTS[self][0]

    @property
    def fill_effect(self) -> float:
        """ホーム試合の入場率の平均への効き（足す値）。"""
        return _EFFECTS[self][1]

    @property
    def popularity_effect(self) -> float:
        """翌年の人気への効き（点。1年あたり。締める処理が足す）。"""
        return _EFFECTS[self][2]

    @property
    def unit_price(self) -> int:
        """1人あたりの平均の単価（円）。"""
        return round(BASE_TICKET_PRICE * self.multiplier)


# 段階ごとの（単価の倍率, 入場率の平均への効き, 人気への効き）。2026-11-14 ユーザー決定（#371 Q1 の A）
_EFFECTS: dict[TicketPrice, tuple[float, float, float]] = {
    TicketPrice.LOW: (0.7, 0.08, 1.0),
    TicketPrice.SLIGHTLY_LOW: (0.85, 0.04, 0.5),
    TicketPrice.STANDARD: (1.0, 0.0, 0.0),
    TicketPrice.SLIGHTLY_HIGH: (1.15, -0.05, -1.0),
    TicketPrice.HIGH: (1.3, -0.10, -2.0),
}

# --- AI の規則（その年の人気で決める。#371 Q1 の A） ---
HIGH_FROM = 80
SLIGHTLY_HIGH_FROM = 65
SLIGHTLY_LOW_TO = 35
LOW_TO = 20


def ai_ticket_price(popularity: int) -> TicketPrice:
    """AI の値段（GM の球団のお任せも同じ）。人気（1〜100）が高ければ高く、低ければ安く。乱数なし・保存しない。"""
    if popularity >= HIGH_FROM:
        return TicketPrice.HIGH
    if popularity >= SLIGHTLY_HIGH_FROM:
        return TicketPrice.SLIGHTLY_HIGH
    if popularity <= LOW_TO:
        return TicketPrice.LOW
    if popularity <= SLIGHTLY_LOW_TO:
        return TicketPrice.SLIGHTLY_LOW
    return TicketPrice.STANDARD


def resolved_ticket_price(manual: TicketPrice | None, popularity: int) -> TicketPrice:
    """その年の値段。手動の値段があればそれ、無ければ（お任せ・AI の球団）人気から決める。

    **値段の決め方の唯一の出典**。
    """
    return manual if manual is not None else ai_ticket_price(popularity)


@dataclass(frozen=True)
class ClubTicketPrice:
    """GM が手動にした、球団 × 年のチケットの値段。世界は球団のリーグで決まる。"""

    team_id: int
    year: int
    price: TicketPrice

    def __post_init__(self) -> None:
        object.__setattr__(self, "year", Season(self.year).year)
        if not isinstance(self.price, TicketPrice):
            raise InvalidTicketPrice("チケットの値段は段階から選んでください。")


def ticket_price_change_blocker(*, year: int, last_played_on: date | None) -> str | None:
    """`year` 年の値段を変えられない理由。変えられるなら None。**変えられる時期の唯一の出典**。

    変えられるのはその年の開幕前だけ（その年の試合が1つも無い。2026-11-14 ユーザー決定）。年の中で値段が1つなので、
    入場料（観客動員 × 単価）が正確に導ける。締めた年は開幕した年でもあるので、ここで一緒に止まる。
    """
    if last_played_on is not None and last_played_on.year >= year:
        return f"{year}年は開幕したので、チケットの値段は来年の開幕前まで変えられません。"
    return None
