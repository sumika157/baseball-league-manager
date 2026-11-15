"""ペナントの球団の収入と年俸の総額の上限の読み口（#40 K1b）。読むだけで、書き込みはしない。

- 収入（`ClubRevenueReader`）: 終わった年の球団ごとの収入（親会社の補填を含まない）。人件費によらないので、
  年俸の契約の行が無い年（収支は「—」）でも人気の行があれば出せる。材料の作り方（`RevenueMaterials`）は収支
  （`pennant_finance.ClubFinanceService`）と共通で、収入の式は domain の `finance.club_revenue` の1か所
- 上限（`PayrollCapReader`）: ある年の上限を前年の収入から domain の `payroll_cap.payroll_cap` で導く（`cap_of`）。
  前年の人気の行が無い球団（開幕年・人気を記録する前の年の翌年）は、標準の球団の収入の見込み（`finance.expected_revenue`）から

前の年の人気は保存した行だけを読む（人気の読み口と同じ使い分け。行の無い年は「収入が無い」として見込みに回す）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ..domain.pennant.finance import ClubRevenue, club_revenue, expected_revenue
from ..domain.pennant.payroll_cap import PayrollCap, payroll_cap
from ..domain.pennant.popularity import Popularity
from ..domain.pennant.schedule import ScheduleRules
from ..domain.pennant.ticket_price import TicketPrice, resolved_ticket_price
from ..domain.value_objects import GameStage
from .pennant_popularity import PopularityReader
from .pennant_ticket_price import TicketPriceReader
from .queries import AttendanceQuery, ClubFinanceQuery


@dataclass(frozen=True)
class RevenueMaterials:
    """収入を導く材料（球団 × 年）。読むのは呼ぶ側の範囲だけ。収支（`ClubFinanceService`）と共通。"""

    popularity: Mapping[tuple[int, int], Popularity]
    manual: Mapping[tuple[int, int], TicketPrice]
    regular: Mapping[tuple[int, int], int]
    postseason: Mapping[tuple[int, int], int]

    def price(self, team_id: int, year: int) -> TicketPrice | None:
        """その年の値段（手動か、お任せ・AI の規則）。人気の無い球団 × 年は None。"""
        popularity = self.popularity.get((team_id, year))
        if popularity is None:
            return None
        return resolved_ticket_price(self.manual.get((team_id, year)), popularity.value)

    def revenue(self, team_id: int, year: int, *, share: float = 1.0) -> ClubRevenue | None:
        """球団 × 年の収入（年額の項目は `share` で按分）。人気の無い球団 × 年は None。"""
        key = (team_id, year)
        popularity = self.popularity.get(key)
        price = self.price(team_id, year)
        if popularity is None or price is None:
            return None
        return club_revenue(
            regular_attendance=self.regular.get(key, 0),
            postseason_attendance=self.postseason.get(key, 0),
            ticket_price=price,
            popularity=popularity,
            season_share=share,
        )


def home_totals(
    attendance: AttendanceQuery, stage: GameStage, *, year: int | None, team_id: int | None
) -> dict[tuple[int, int], int]:
    """ホーム試合の観客動員の合計（(球団, 年) → 人数）。"""
    rows = attendance.home_totals(stage=stage, year=year, team_id=team_id)
    return {(row.team_id, row.year): row.total for row in rows}


def cap_of(previous_revenue: int | None, capacity: int | None) -> PayrollCap:
    """前年の収入（無ければ None）と本拠地の収容人数から、その年の年俸の上限を導く。

    前年の収入が無ければ、標準の球団の収入の見込み（ホーム試合は1球団の公式戦の半分）から作る。
    """
    return payroll_cap(
        previous_revenue,
        expected_revenue=expected_revenue(capacity, home_games=ScheduleRules().games_per_team / 2),
    )


def cap_from(materials: RevenueMaterials, team_id: int, year: int, capacity: int | None) -> PayrollCap:
    """`year` 年の上限を、前年（`year - 1`）の収入の材料から導く。**前年の収入の数え方はここ1か所**
    （人気の行のある年だけ・年額の項目は按分しない・ポストシーズンを含む。無ければ見込み）。
    `PayrollCapReader.caps` と収支の年ごとの表が通る。"""
    previous = materials.revenue(team_id, year - 1)
    return cap_of(None if previous is None else previous.total, capacity)


class ClubRevenueReader:
    """終わった年の球団ごとの収入の読み口（世界ひとつ。読み口と参照クエリは世界の範囲で組み立てて渡す）。"""

    def __init__(
        self, *, popularities: PopularityReader, ticket_prices: TicketPriceReader, attendance: AttendanceQuery
    ) -> None:
        self._popularities = popularities
        self._ticket_prices = ticket_prices
        self._attendance = attendance

    def materials(self, year: int, team_ids: Sequence[int]) -> RevenueMaterials:
        """公式戦を終えた年 `year` の収入の材料（人気は保存した行だけ）。"""
        recorded = self._popularities.recorded(year)
        return RevenueMaterials(
            popularity={(team_id, year): value for team_id, value in recorded.items() if team_id in team_ids},
            manual={(team_id, year): price for team_id, price in self._ticket_prices.manual(year).items()},
            regular=home_totals(self._attendance, GameStage.REGULAR, year=year, team_id=None),
            postseason=home_totals(self._attendance, GameStage.POSTSEASON, year=year, team_id=None),
        )

    def revenues(self, year: int, team_ids: Sequence[int]) -> dict[int, int | None]:
        """公式戦を終えた年 `year` の球団ごとの収入（補填を含まない。年額の項目は按分しない）。

        人気の行の無い球団は None（収入が無い）。年の途中の年には使わない（按分しないため）。
        """
        materials = self.materials(year, team_ids)
        result: dict[int, int | None] = {}
        for team_id in team_ids:
            revenue = materials.revenue(team_id, year)
            result[team_id] = None if revenue is None else revenue.total
        return result


class PayrollCapReader:
    """球団ごとの年俸の総額の上限の読み口（世界ひとつ）。"""

    def __init__(self, *, revenues: ClubRevenueReader, facts: ClubFinanceQuery) -> None:
        self._revenues = revenues
        # 本拠地の収容人数（前年の収入が無い球団の見込み）
        self._facts = facts

    def caps(self, year: int, team_ids: Sequence[int]) -> dict[int, PayrollCap]:
        """`year` 年の球団ごとの上限（前年の収入から。無ければ標準の球団の見込みから）。"""
        materials = self._revenues.materials(year - 1, team_ids)
        capacities = self._facts.stadium_capacities(team_ids)
        return {team_id: cap_from(materials, team_id, year, capacities.get(team_id)) for team_id in team_ids}
