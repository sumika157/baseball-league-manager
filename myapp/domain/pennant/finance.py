"""ペナントの球団の年の収支（#208 E2）。**純粋な値と規則**で、DB にも Django にも触れない。

収支は**保存せず導く**（2026-11-14 ユーザー決定。#208）。材料（`FinanceInputs`）はその年のホーム試合の観客動員
（公式戦とポストシーズン）・チケットの値段・人気・契約区分ごとの人数・本拠地の収容人数で、どれも試合・在籍・
年ごとの状態から読める。資金の繰越は持たない（翌年の予算は前年1年の収支から。E3）。

金額は円の整数で、NPB の水準に合わせる（2026-11-14 ユーザー決定。#371 Q2 の A）:

- 収入: 入場料（観客動員 × 単価）・放映権（基準＋人気の差）・スポンサー（基準＋人気の差）・グッズ（観客動員 ×
  1人あたり。人気で上下）・ポストシーズン（ホームの観客動員 × 単価 × 1.5）
- 支出: 仮の人件費（`provisional_payroll`。契約区分ごとの定額 × 人数。#40 K1 で年俸の合計に置き換える）・
  球場の運営費（定額＋収容人数に比例）
- 親会社の補填（`parent_subsidy`）: 補填前の収支が赤字なら、その分（繰越なし）

平均的な球団（人気50・標準の値段・標準の収容人数）で補填前の収支がおおむね0になるよう、運営費を合わせてある
（実測は Wiki「ペナントモードの仕組み」の「実測値」の「人気と収支」）。
年額の項目（放映権・スポンサー・人件費・運営費）は、年の途中なら消化した公式戦の割合（`season_share`）で按分する
（「今年の収支（途中経過）」。終わった年は 1）。
"""

from __future__ import annotations

from dataclasses import dataclass

from .popularity import MEAN_POPULARITY, Popularity, pennant_capacity
from .ticket_price import TicketPrice

# 1億円
OKU = 100_000_000

# --- 収入（円。2026-11-14 ユーザー決定の水準） ---
# 放映権: 基準 15億円 ＋ 人気 1点あたり 0.3億円（人気80で +9億円）
BROADCASTING_BASE = 15 * OKU
BROADCASTING_PER_POINT = 30_000_000
# スポンサー: 基準 20億円 ＋ 人気 1点あたり 0.4億円
SPONSORSHIP_BASE = 20 * OKU
SPONSORSHIP_PER_POINT = 40_000_000
# グッズ: 観客1人あたり 1,000円 × (1 ＋ 人気 1点あたり 1%)
MERCHANDISE_PER_FAN = 1_000
MERCHANDISE_PER_POINT = 0.01
# ポストシーズンの入場料は単価の1.5倍
POSTSEASON_PRICE_FACTOR = 1.5

# --- 支出（円） ---
# 仮の人件費（1人あたりの年額）。#40 K1 で年俸の合計に置き換える
REGISTERED_PAYROLL = 40_000_000
DEVELOPMENTAL_PAYROLL = 5_000_000
# 球場の運営費 = 定額 ＋ 収容人数 1席あたり。平均的な球団で補填前の収支がおおむね0になるよう合わせた（E2 の実測）
OPERATIONS_BASE = 54 * OKU
OPERATIONS_PER_SEAT = 150_000


@dataclass(frozen=True)
class RosterSize:
    """球団 × 年の契約区分ごとの人数（その年に在籍した選手。仮の人件費の材料）。"""

    registered: int
    developmental: int

    def __post_init__(self) -> None:
        if self.registered < 0 or self.developmental < 0:
            raise ValueError("人数は0以上にしてください。")


def provisional_payroll(size: RosterSize) -> int:
    """仮の人件費（年額。円）。契約区分ごとの定額 × 人数（支配下・育成の2区分。外国人も支配下の定額。
    2026-11-14 ユーザー決定）。**人件費の唯一の出典**で、#40 K1 で年俸の合計に置き換える。
    """
    return REGISTERED_PAYROLL * size.registered + DEVELOPMENTAL_PAYROLL * size.developmental


def parent_subsidy(operating_balance: int) -> int:
    """親会社の補填（円）。補填前の収支が赤字なら、その分（繰越なし）。**補填を決める唯一の出典**。

    #40 K1 は年俸の総額に予算の上限を置き、超えた分を親会社の補填とオーナーの評価で効かせる（#40 Q3）。
    その項はこの関数に足す。
    """
    return max(0, -operating_balance)


@dataclass(frozen=True)
class FinanceInputs:
    """球団 × 年の収支の材料。

    - `regular_attendance`・`postseason_attendance`: 公式戦・ポストシーズンのホーム試合の観客動員の合計
    - `ticket_price`: その年のチケットの値段（手動か、お任せ・AI の規則）
    - `popularity`: その年の人気
    - `roster`: 契約区分ごとの人数
    - `capacity`: 本拠地の収容人数（分からなければ None。ペナントの標準の収容人数で補う）
    - `season_share`: 年額の項目に掛ける割合（終わった年は 1、年の途中は消化した公式戦の割合。0〜1）
    """

    regular_attendance: int
    postseason_attendance: int
    ticket_price: TicketPrice
    popularity: Popularity
    roster: RosterSize
    capacity: int | None
    season_share: float = 1.0

    def __post_init__(self) -> None:
        if self.regular_attendance < 0 or self.postseason_attendance < 0:
            raise ValueError("観客動員は0以上にしてください。")
        if not 0.0 <= self.season_share <= 1.0:
            raise ValueError("年額の項目の割合は0〜1にしてください。")


@dataclass(frozen=True)
class ClubFinance:
    """球団 × 年の収支（円）。保存せず `club_finance` が導く。"""

    gate: int
    broadcasting: int
    sponsorship: int
    merchandise: int
    postseason: int
    payroll: int
    operations: int

    @property
    def revenue(self) -> int:
        """収入の合計（親会社の補填を含まない）。"""
        return self.gate + self.broadcasting + self.sponsorship + self.merchandise + self.postseason

    @property
    def expenses(self) -> int:
        return self.payroll + self.operations

    @property
    def operating_balance(self) -> int:
        """補填前の収支。"""
        return self.revenue - self.expenses

    @property
    def subsidy(self) -> int:
        """親会社の補填（`parent_subsidy`）。"""
        return parent_subsidy(self.operating_balance)

    @property
    def balance(self) -> int:
        """補填後の収支（赤字は補填で0になる）。"""
        return self.operating_balance + self.subsidy


def club_finance(inputs: FinanceInputs) -> ClubFinance:
    """球団 × 年の収支を導く。**収支の式の唯一の出典**。"""
    points = inputs.popularity.value - MEAN_POPULARITY
    unit = inputs.ticket_price.unit_price
    share = inputs.season_share
    merchandise_per_fan = MERCHANDISE_PER_FAN * max(0.0, 1.0 + MERCHANDISE_PER_POINT * points)
    return ClubFinance(
        gate=inputs.regular_attendance * unit,
        broadcasting=round(share * max(0, BROADCASTING_BASE + BROADCASTING_PER_POINT * points)),
        sponsorship=round(share * max(0, SPONSORSHIP_BASE + SPONSORSHIP_PER_POINT * points)),
        merchandise=round(inputs.regular_attendance * merchandise_per_fan),
        postseason=round(inputs.postseason_attendance * unit * POSTSEASON_PRICE_FACTOR),
        payroll=round(share * provisional_payroll(inputs.roster)),
        operations=round(share * (OPERATIONS_BASE + OPERATIONS_PER_SEAT * pennant_capacity(inputs.capacity))),
    )


def season_share(*, games_played: int, games_per_team: int, finished: bool) -> float:
    """年額の項目に掛ける割合。終わった年（公式戦を終えた）は 1、年の途中は消化した公式戦の割合（0〜1）。"""
    if finished or games_per_team <= 0:
        return 1.0
    return min(max(games_played / games_per_team, 0.0), 1.0)


def money_label(yen: int) -> str:
    """金額の表示（億円・小数1桁。例 `70.3億円`・`-2.5億円`）。**金額の表記の唯一の出典**。"""
    value = round(yen / OKU, 1)
    if value == 0:
        value = 0.0  # -0.0 を出さない
    return f"{value:,.1f}億円"
