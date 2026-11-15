"""ペナントの球団の年の収支（#208 E2）。**純粋な値と規則**で、DB にも Django にも触れない。

収支は**保存せず導く**（2026-11-14 ユーザー決定。#208）。材料（`FinanceInputs`）はその年のホーム試合の観客動員
（公式戦とポストシーズン）・チケットの値段・人気・年俸の合計・本拠地の収容人数・年俸の上限で、どれも試合・在籍・
契約・年ごとの状態から読める。資金の繰越は持たない（翌年の予算は前年1年の収支から。E3）。

金額は円の整数で、NPB の水準に合わせる（2026-11-14 ユーザー決定。#371 Q2 の A）:

- 収入（`club_revenue`）: 入場料（観客動員 × 単価）・放映権（基準＋人気の差）・スポンサー（基準＋人気の差）・グッズ
  （観客動員 × 1人あたり。人気で上下）・ポストシーズン（ホームの観客動員 × 単価 × 1.5）。人件費によらない
  （年俸の上限の材料。#40 K1b）
- 支出: 人件費（その年に在籍した選手の年俸の合計。`salary.payroll`。#40 K1a）・
  球場の運営費（定額＋収容人数に比例）
- 親会社の補填（`parent_subsidy`）: 年俸の合計が上限（`payroll_cap.py`。#40 K1b）を超えた分（黒字でも払う）と、
  それを足してもなお残る赤字の分の2行（繰越なし）

平均的な球団（人気50・標準の値段・標準の収容人数）で補填前の収支がおおむね0になるよう、運営費を合わせてある
（実測は Wiki「ペナントモードの仕組み」の「年俸（K1a）」の「実測」）。
年額の項目（放映権・スポンサー・人件費・運営費・上限を超えた分）は、年の途中なら消化した公式戦の割合
（`season_share`）で按分する（「今年の収支（途中経過）」。終わった年は 1）。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..simulation.game_day import WEEKDAY_FILL, WEEKEND_FILL
from .payroll_cap import PayrollCap, payroll_overrun
from .popularity import MEAN_POPULARITY, Popularity, initial_popularity, pennant_capacity
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
# 標準の球団（人気50・標準の値段）のホーム試合の入場率の見込み（`expected_revenue`。前年の収入が無い年の年俸の上限に
# 使う。#40 K1b）。試合の入場率の平均（`simulation.game_day` の平日・土日）を曜日の数で均した値（約0.83）
EXPECTED_FILL = (5 * WEEKDAY_FILL[0] + 2 * WEEKEND_FILL[0]) / 7

# --- 支出（円） ---
# 人件費は年俸の合計（`salary.payroll`。#40 K1a）で、ここには数を持たない
# 球場の運営費 = 定額 ＋ 収容人数 1席あたり。平均的な球団で補填前の収支がおおむね0になるよう合わせた（#40 K1a の実測）
OPERATIONS_BASE = 44 * OKU
OPERATIONS_PER_SEAT = 150_000


@dataclass(frozen=True)
class ParentSubsidy:
    """親会社の補填（円）の2行。`overrun` は年俸の合計が上限を超えた分（黒字でも払う）、`deficit` はそれを足しても
    なお残る赤字の分。"""

    overrun: int
    deficit: int

    @property
    def total(self) -> int:
        return self.overrun + self.deficit


def parent_subsidy(operating_balance: int, *, overrun: int) -> ParentSubsidy:
    """親会社の補填。**補填を決める唯一の出典**。

    年俸の上限を超えた分 `overrun`（`payroll_cap.payroll_overrun`）は黒字でも払い（2026-11-15 ユーザー決定。#394 Q1）、
    それを足した後の収支がまだ赤字なら、その分も払う（繰越なし）。
    """
    if overrun < 0:
        raise ValueError("上限を超えた分は0以上にしてください。")
    return ParentSubsidy(overrun=overrun, deficit=max(0, -(operating_balance + overrun)))


@dataclass(frozen=True)
class FinanceInputs:
    """球団 × 年の収支の材料。

    - `regular_attendance`・`postseason_attendance`: 公式戦・ポストシーズンのホーム試合の観客動員の合計
    - `ticket_price`: その年のチケットの値段（手動か、お任せ・AI の規則）
    - `popularity`: その年の人気
    - `payroll`: 人件費（年額。その年に在籍した選手の年俸の合計＝`salary.payroll`。円。期中に獲得した外国人も
      年額で数え、期中に2球団に在籍した選手は両方で数える。#40 K1a）
    - `capacity`: 本拠地の収容人数（分からなければ None。ペナントの標準の収容人数で補う）
    - `payroll_cap`: その年の年俸の総額の上限（年額。`payroll_cap.payroll_cap`。#40 K1b）
    - `season_share`: 年額の項目に掛ける割合（終わった年は 1、年の途中は消化した公式戦の割合。0〜1）
    """

    regular_attendance: int
    postseason_attendance: int
    ticket_price: TicketPrice
    popularity: Popularity
    payroll: int
    capacity: int | None
    payroll_cap: PayrollCap
    season_share: float = 1.0

    def __post_init__(self) -> None:
        if self.regular_attendance < 0 or self.postseason_attendance < 0:
            raise ValueError("観客動員は0以上にしてください。")
        if self.payroll < 0:
            raise ValueError("人件費は0以上にしてください。")
        if not 0.0 <= self.season_share <= 1.0:
            raise ValueError("年額の項目の割合は0〜1にしてください。")


@dataclass(frozen=True)
class ClubRevenue:
    """球団 × 年の収入（円）の内訳。`club_revenue` が導く（人件費によらない。年俸の上限の材料。#40 K1b）。"""

    gate: int
    broadcasting: int
    sponsorship: int
    merchandise: int
    postseason: int

    @property
    def total(self) -> int:
        """収入の合計（親会社の補填を含まない）。"""
        return self.gate + self.broadcasting + self.sponsorship + self.merchandise + self.postseason


@dataclass(frozen=True)
class ClubFinance:
    """球団 × 年の収支（円）。保存せず `club_finance` が導く。

    `payroll_cap` はその年の年俸の上限（年額）、`overrun` は年俸の合計が上限を超えた分（年の途中なら按分した
    途中経過）。
    """

    gate: int
    broadcasting: int
    sponsorship: int
    merchandise: int
    postseason: int
    payroll: int
    operations: int
    payroll_cap: PayrollCap
    overrun: int

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
    def subsidy(self) -> ParentSubsidy:
        """親会社の補填（`parent_subsidy`。上限超過と残る赤字の2行）。"""
        return parent_subsidy(self.operating_balance, overrun=self.overrun)

    @property
    def balance(self) -> int:
        """補填後の収支（残る赤字は補填で0になり、上限を超えた分の補填は黒字に上乗せになる）。"""
        return self.operating_balance + self.subsidy.total


def club_revenue(
    *,
    regular_attendance: int,
    postseason_attendance: int,
    ticket_price: TicketPrice,
    popularity: Popularity,
    season_share: float = 1.0,
) -> ClubRevenue:
    """球団 × 年の収入を導く。**収入の式の唯一の出典**（`club_finance` と年俸の上限の材料が通る）。

    年額の項目（放映権・スポンサー）は `season_share` で按分する。
    """
    if regular_attendance < 0 or postseason_attendance < 0:
        raise ValueError("観客動員は0以上にしてください。")
    points = popularity.value - MEAN_POPULARITY
    unit = ticket_price.unit_price
    merchandise_per_fan = MERCHANDISE_PER_FAN * max(0.0, 1.0 + MERCHANDISE_PER_POINT * points)
    return ClubRevenue(
        gate=regular_attendance * unit,
        broadcasting=round(season_share * max(0, BROADCASTING_BASE + BROADCASTING_PER_POINT * points)),
        sponsorship=round(season_share * max(0, SPONSORSHIP_BASE + SPONSORSHIP_PER_POINT * points)),
        merchandise=round(regular_attendance * merchandise_per_fan),
        postseason=round(postseason_attendance * unit * POSTSEASON_PRICE_FACTOR),
    )


def club_finance(inputs: FinanceInputs) -> ClubFinance:
    """球団 × 年の収支を導く。**収支の式の唯一の出典**。"""
    share = inputs.season_share
    revenue = club_revenue(
        regular_attendance=inputs.regular_attendance,
        postseason_attendance=inputs.postseason_attendance,
        ticket_price=inputs.ticket_price,
        popularity=inputs.popularity,
        season_share=share,
    )
    return ClubFinance(
        gate=revenue.gate,
        broadcasting=revenue.broadcasting,
        sponsorship=revenue.sponsorship,
        merchandise=revenue.merchandise,
        postseason=revenue.postseason,
        payroll=round(share * inputs.payroll),
        operations=round(share * (OPERATIONS_BASE + OPERATIONS_PER_SEAT * pennant_capacity(inputs.capacity))),
        payroll_cap=inputs.payroll_cap,
        overrun=round(share * payroll_overrun(inputs.payroll, inputs.payroll_cap)),
    )


def expected_revenue(capacity: int | None, *, home_games: float) -> int:
    """標準の球団（人気50・標準の値段・入場率 `EXPECTED_FILL`・ポストシーズンなし）の1年の収入の見込み（円）。

    前年の収入が無い年の年俸の上限（`payroll_cap.payroll_cap` の `expected_revenue`）に使う。`capacity` は本拠地の
    収容人数（分からなければ標準の収容人数で補う）、`home_games` はホーム試合の数。
    """
    attendance = round(pennant_capacity(capacity) * EXPECTED_FILL * home_games)
    return club_revenue(
        regular_attendance=attendance,
        postseason_attendance=0,
        ticket_price=TicketPrice.STANDARD,
        popularity=initial_popularity(),
    ).total


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
