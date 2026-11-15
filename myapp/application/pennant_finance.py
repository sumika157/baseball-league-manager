"""ペナントの球団の収支とチケットの値段の変更（#208 E2）。値段の読み口は `pennant_ticket_price`。

- 収支（`ClubFinanceService`。参照）: 保存せず、観客動員（公式戦とポストシーズン）・人気・値段・人件費（年俸の合計。
  `pennant_salary`。#40 K1a）・収容人数・年俸の上限から domain の `club_finance` で導く。人気の行の無い年（#208 E1 より
  前の年）と、契約の行の無い選手のいる前の年（#40 K1a より前に締めた年）は出さない（「—」。2026-11-14 ユーザー決定）。
  いまの年度は進める処理と同じく人気と年俸を補い、年額の項目を消化した公式戦の割合で按分する。
  年俸の上限（#40 K1b）は前年の収入から `pennant_payroll_cap` の `cap_of` で導き、超えた分を親会社の補填の1行にする
- 値段の変更（`TicketPriceService`。更新）: GM が受け持つ球団のいまの年度の値段を手動にする・お任せに戻す。
  変えられるのは開幕前だけ（検査はリポジトリがトランザクションの中で行う。domain の `ticket_price_change_blocker`）
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ..domain.exceptions import InvalidTicketPrice
from ..domain.pennant.finance import ClubFinance, FinanceInputs, club_finance, season_share
from ..domain.pennant.payroll_cap import PayrollCap
from ..domain.pennant.popularity import MEAN_POPULARITY, with_initial
from ..domain.pennant.schedule import ScheduleRules
from ..domain.pennant.season import SeasonPhase, season_year
from ..domain.pennant.ticket_price import (
    ClubTicketPrice,
    TicketPrice,
    ai_ticket_price,
    ticket_price_change_blocker,
)
from ..domain.repositories import FixtureRepository, TicketPriceRepository, WorldRepository
from ..domain.value_objects import GameStage
from .dto import (
    ClubFinanceRow,
    ClubFinanceView,
    FinanceFigures,
    Money,
    TicketPriceOption,
    WorldContext,
)
from .pennant_payroll_cap import PayrollCapReader, RevenueMaterials, cap_from, home_totals
from .pennant_popularity import PopularityReader
from .pennant_salary import SalaryContractReader
from .pennant_season import AtomicBlock
from .pennant_ticket_price import TicketPriceReader
from .queries import AttendanceQuery, ClubFinanceQuery, GameListQuery, SimulationContextQuery


@dataclass(frozen=True)
class _Materials:
    """収支を導く材料（球団 × 年）。読むのは呼ぶ側の範囲だけ。収入の材料は年俸の上限の読み口と共通。"""

    revenue: RevenueMaterials
    # 人件費（年俸の合計。#40 K1a）。分からない球団 × 年（契約の行の無い前の年）は None
    payrolls: Mapping[tuple[int, int], int | None]
    capacities: Mapping[int, int | None]
    # いまの年度の、球団ごとの消化した公式戦の数（いまの年度を含まなければ空）
    played: Mapping[int, int]
    # 年俸の上限（#40 K1b。前年の収入から）
    caps: Mapping[tuple[int, int], PayrollCap]


class ClubFinanceService:
    """球団の年の収支（参照。#208 E2）。依存は世界の範囲で組み立てて渡す。"""

    def __init__(
        self,
        *,
        popularities: PopularityReader,
        ticket_prices: TicketPriceReader,
        attendance: AttendanceQuery,
        facts: ClubFinanceQuery,
        games: GameListQuery,
        salaries: SalaryContractReader,
        caps: PayrollCapReader,
    ) -> None:
        self._popularities = popularities
        self._ticket_prices = ticket_prices
        self._attendance = attendance
        self._facts = facts
        # いまの年度の消化した公式戦の数（年額の項目の按分）
        self._games = games
        # 人件費（年俸の合計。#40 K1a）。いまの年度は契約の行の無い選手を補う
        self._salaries = salaries
        # 年俸の上限（#40 K1b）。ある年の行だけを出すときに使う（球団ひとつの年ごとの表は、手元の材料から
        # 同じ関数で導く）
        self._caps = caps

    def view(self, world: WorldContext) -> ClubFinanceView | None:
        """経営の画面の材料（受け持つ球団）。受け持つ球団が無ければ None。"""
        team_id = world.managed_team_id
        if team_id is None:
            return None
        rows = self.club_years(world, team_id)
        current = rows[-1]
        year = world.season_year
        # いまの年度の人気は初期値で補ってあるので必ずある
        auto_price = ai_ticket_price(current.popularity if current.popularity is not None else MEAN_POPULARITY)
        return ClubFinanceView(
            team_id=team_id,
            team_name=world.managed_team_name,
            year=year,
            current=current,
            history=tuple(reversed(rows[:-1])),
            manual=current.ticket_price if current.manual_price else None,
            auto_price=auto_price,
            blocker=ticket_price_change_blocker(year=year, last_played_on=world.today) or "",
            options=tuple(
                TicketPriceOption(
                    price=price,
                    unit_price=price.unit_price,
                    fill_effect_label=f"{price.fill_effect:+.2f}",
                    popularity_effect_label=f"{price.popularity_effect:+.1f}",
                )
                for price in TicketPrice
            ),
        )

    def club_years(self, world: WorldContext, team_id: int) -> list[ClubFinanceRow]:
        """ひとつの球団の、試合か人気のある年といまの年度の値段と収支（古い順。最後がいまの年度）。"""
        year = world.season_year
        recorded = self._popularities.history(team_id)
        regular = self._totals(GameStage.REGULAR, year=None, team_id=team_id)
        postseason = self._totals(GameStage.POSTSEASON, year=None, team_id=team_id)
        years = sorted({y for (_, y) in regular} | set(recorded) | {year})
        years = [y for y in years if y <= year]
        # 前の年は保存した行だけ、いまの年度は進める処理と同じく初期値で補う
        popularity = {(team_id, y): value for y, value in recorded.items() if y < year}
        found = {team_id: recorded[year]} if year in recorded else {}
        current = with_initial([team_id], found)
        popularity[(team_id, year)] = current[team_id]
        revenue = RevenueMaterials(
            popularity=popularity,
            manual={(team_id, y): price for y, price in self._ticket_prices.manual_history(team_id).items()},
            regular=regular,
            postseason=postseason,
        )
        capacities = self._facts.stadium_capacities([team_id])
        # 前年の収入は手元の全年の材料から（年ごとに読み直さない。`PayrollCapReader.caps` と同じ `cap_from`）
        caps = {(team_id, y): cap_from(revenue, team_id, y, capacities.get(team_id)) for y in years}
        materials = _Materials(
            revenue=revenue,
            payrolls=self._salaries.payrolls(years, [team_id], open_year=year),
            capacities=capacities,
            played=self._played(world),
            caps=caps,
        )
        return [self._row(world, team_id, y, materials) for y in years]

    def year_rows(self, world: WorldContext, year: int, team_ids: Sequence[int]) -> dict[int, ClubFinanceRow]:
        """ある年の球団ごとの値段と収支（オフの結果・GM ホーム）。いまの年度なら人気の無い球団を初期値で補う。"""
        recorded = self._popularities.recorded(year)
        if year == world.season_year:
            recorded = with_initial(team_ids, recorded)
        manual = self._ticket_prices.manual(year)
        materials = _Materials(
            revenue=RevenueMaterials(
                popularity={(team_id, year): value for team_id, value in recorded.items() if team_id in team_ids},
                manual={(team_id, year): price for team_id, price in manual.items()},
                regular=self._totals(GameStage.REGULAR, year=year, team_id=None),
                postseason=self._totals(GameStage.POSTSEASON, year=year, team_id=None),
            ),
            payrolls=self._salaries.payrolls([year], team_ids, open_year=year if year == world.season_year else None),
            capacities=self._facts.stadium_capacities(team_ids),
            played=self._played(world) if year == world.season_year else {},
            caps={(team_id, year): cap for team_id, cap in self._caps.caps(year, team_ids).items()},
        )
        return {team_id: self._row(world, team_id, year, materials) for team_id in team_ids}

    def _row(self, world: WorldContext, team_id: int, year: int, materials: _Materials) -> ClubFinanceRow:
        key = (team_id, year)
        regular = materials.revenue.regular.get(key, 0)
        postseason = materials.revenue.postseason.get(key, 0)
        in_progress = year == world.season_year and world.phase is not SeasonPhase.FINISHED
        popularity = materials.revenue.popularity.get(key)
        price = materials.revenue.price(team_id, year)
        payroll = materials.payrolls.get(key)
        if popularity is None or price is None or payroll is None:
            # 人気の行の無い年（#208 E1 より前の年）と、契約の行の無い選手のいる前の年（#40 K1a より前に締めた年。
            # 人件費が分からない）は、作っていない値を見せない
            return ClubFinanceRow(
                team_id=team_id,
                year=year,
                popularity=None,
                ticket_price=None,
                manual_price=False,
                figures=None,
                in_progress=in_progress,
                regular_attendance=regular,
                postseason_attendance=postseason,
            )
        share = season_share(
            games_played=materials.played.get(team_id, 0),
            games_per_team=ScheduleRules().games_per_team,
            finished=not in_progress,
        )
        finance = club_finance(
            FinanceInputs(
                regular_attendance=regular,
                postseason_attendance=postseason,
                ticket_price=price,
                popularity=popularity,
                payroll=payroll,
                capacity=materials.capacities.get(team_id),
                payroll_cap=materials.caps[key],
                season_share=share,
            )
        )
        return ClubFinanceRow(
            team_id=team_id,
            year=year,
            popularity=popularity.value,
            ticket_price=price,
            manual_price=key in materials.revenue.manual,
            figures=figures_of(finance),
            in_progress=in_progress,
            regular_attendance=regular,
            postseason_attendance=postseason,
        )

    def _totals(self, stage: GameStage, *, year: int | None, team_id: int | None) -> dict[tuple[int, int], int]:
        return home_totals(self._attendance, stage, year=year, team_id=team_id)

    def _played(self, world: WorldContext) -> dict[int, int]:
        """いまの年度の消化した公式戦の数（公式戦が終わっていれば按分しないので読まない）。"""
        if world.phase is SeasonPhase.FINISHED:
            return {}
        return self._games.count_by_team(year=world.season_year)


def figures_of(finance: ClubFinance) -> FinanceFigures:
    """domain の収支を画面に渡す形に写す。"""
    subsidy = finance.subsidy
    return FinanceFigures(
        gate=Money(finance.gate),
        broadcasting=Money(finance.broadcasting),
        sponsorship=Money(finance.sponsorship),
        merchandise=Money(finance.merchandise),
        postseason=Money(finance.postseason),
        revenue=Money(finance.revenue),
        payroll=Money(finance.payroll),
        operations=Money(finance.operations),
        expenses=Money(finance.expenses),
        operating_balance=Money(finance.operating_balance),
        subsidy=Money(subsidy.total),
        overrun_subsidy=Money(subsidy.overrun),
        deficit_subsidy=Money(subsidy.deficit),
        balance=Money(finance.balance),
        payroll_cap=Money(finance.payroll_cap.yen),
        cap_estimated=finance.payroll_cap.estimated,
    )


class TicketPriceService:
    """GM が受け持つ球団のいまの年度のチケットの値段を変える（更新。#208 E2）。依存は世界の範囲で組み立てて渡す。"""

    def __init__(
        self,
        *,
        world_id: int,
        worlds: WorldRepository,
        prices: TicketPriceRepository,
        fixtures: FixtureRepository,
        context_query: SimulationContextQuery,
        atomic: AtomicBlock,
    ) -> None:
        self._world_id = world_id
        self._worlds = worlds
        self._prices = prices
        self._fixtures = fixtures
        self._context_query = context_query
        self._atomic = atomic

    def change(self, price: TicketPrice | None) -> bool:
        """いまの年度の値段を手動にする（`price`）か、お任せに戻す（None）。変えたら True、同じなら何もせず False。

        受け持つ球団が無ければ InvalidTicketPrice。開幕した年・締めた年はリポジトリが TicketPriceLocked・
        AlreadyClosed で断る（トランザクションの中で確かめる）。年は手動の値段を書くトランザクションの中で導く。
        """
        with self._atomic():
            world = self._worlds.find_by_id(self._world_id)
            team_id = world.managed_team_id
            if team_id is None:
                raise InvalidTicketPrice("受け持つ球団が無いので、チケットの値段は変えられません。")
            year = season_year(
                next_fixture_on=self._fixtures.first_date(),
                last_played_on=self._context_query.last_played_on(),
                start_year=world.start_year,
            )
            current = {row.team_id: row.price for row in self._prices.find_by_year(year)}.get(team_id)
            if current is price:
                return False
            if price is None:
                self._prices.delete(team_id, year)
            else:
                self._prices.save(ClubTicketPrice(team_id=team_id, year=year, price=price))
            return True
