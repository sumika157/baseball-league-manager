"""ペナントの選手の年俸（#40 K1a）の読み口と、年俸の参照の画面の材料。

- 読み口（`SalaryContractReader`）: 契約の行の無い選手（この機能より前の世界）の年俸を domain の
  `salary.initial_salary` で補う。**補う読み方はここ1つ**（`fill_salaries`）。補う／行だけ読むの使い分けは人気
  （`pennant_popularity`）に揃える: いまの年度（進める処理が人件費に使う年）と締める年は補い、前の年は保存した行だけを
  読む（行の無い年は「—」。作っていない値を見せない）
- 参照（`PennantPayrollViewService`）: GM ホームの「年俸」のカード（上限と残り・超過。#40 K1b）・選手ページの
  「年俸の推移」・オフの結果の「契約更改」の節。読むだけで、書き込みはしない

年俸の契約は決めた事実として保存する（相場・総額・減額の下限は保存せず導く）。書くのは世界の作成・シーズンを締める
処理・期中の外国人の獲得だけ。締める処理は、行の無い選手の締める年の補った値を、翌年の能力より先に書く。
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

from ..domain.pennant.contract import Salary
from ..domain.pennant.payroll_cap import payroll_overrun
from ..domain.pennant.salary import SalaryBasis, entered_year_of, initial_salary, payroll, salary_basis
from ..domain.repositories import RatingsRepository, SalaryContractRepository
from .dto import (
    Money,
    PayrollMember,
    PayrollSummary,
    RenewalRow,
    RenewalSummary,
    SalaryHistoryRow,
    WorldContext,
)
from .pennant_payroll_cap import PayrollCapReader
from .queries import PayrollQuery


def fill_salaries(recorded: Mapping[int, Salary], bases: Mapping[int, SalaryBasis]) -> dict[int, Salary]:
    """`bases` の選手の年俸。契約の行（`recorded`）があればその値、無ければ `initial_salary` で補う。
    **補いの唯一の入口**（締める処理・画面・人件費が通る）。"""
    return {
        player_id: recorded[player_id] if player_id in recorded else initial_salary(basis)
        for player_id, basis in bases.items()
    }


@dataclass(frozen=True)
class MemberSalary:
    """球団 × 年に在籍した選手ひとりと、その年の年俸（補った値なら `supplemented`。補わない読み方では None
    もある）。
    """

    member: PayrollMember
    salary: Salary | None
    supplemented: bool = False


class SalaryContractReader:
    """年俸の読み口（世界ひとつ。リポジトリと参照クエリは世界の範囲で組み立てて渡す）。"""

    def __init__(
        self, *, contracts: SalaryContractRepository, payroll: PayrollQuery, ratings: RatingsRepository
    ) -> None:
        self._contracts = contracts
        self._payroll = payroll
        # 補いの材料（その年の能力）
        self._ratings = ratings

    def recorded(self, year: int) -> dict[int, Salary]:
        """その年を期間に含む、保存した契約の年俸だけ（選手 id → 年俸。補わない）。"""
        return {contract.player_id: contract.salary for contract in self._contracts.find_by_year(year)}

    def members(self, year: int, team_ids: Collection[int], *, supplement: bool) -> list[MemberSalary]:
        """球団 × `year` 年に在籍した選手と年俸。`supplement` なら行の無い選手を補う（いまの年度）。"""
        rows = self._payroll.members(years=[year], team_ids=team_ids)
        filled = self.supplement(year, [row for row in rows if row.salary is None]) if supplement else {}
        return [_member_salary(row, filled) for row in rows]

    def payrolls(
        self, years: Collection[int], team_ids: Collection[int], *, open_year: int | None
    ) -> dict[tuple[int, int], int | None]:
        """球団 × 年の人件費（年俸の合計。domain の `salary.payroll`）。

        `open_year`（いまの年度）は行の無い選手を補って数える。ほかの年は、行の無い選手がいれば None（分からない）。
        在籍した選手のいない球団 × 年は含まない。
        """
        rows = self._payroll.members(years=years, team_ids=team_ids)
        missing = [row for row in rows if row.salary is None and row.year == open_year]
        filled = self.supplement(open_year, missing) if open_year is not None and missing else {}
        grouped: dict[tuple[int, int], list[Salary | None]] = {}
        for row in rows:
            grouped.setdefault((row.team_id, row.year), []).append(_member_salary(row, filled).salary)
        return {
            key: None if any(salary is None for salary in salaries) else payroll(s for s in salaries if s is not None)
            for key, salaries in grouped.items()
        }

    def supplement(self, year: int, rows: Sequence[PayrollMember]) -> dict[int, Salary]:
        """行の無い選手（`rows`）の `year` 年の補った年俸（選手 id → 年俸）。能力はその年の行を1回で読む。"""
        if not rows:
            return {}
        ratings = {
            item.player_id: item.ratings for item in self._ratings.find_by_players([r.player_id for r in rows], year)
        }
        bases = {
            row.player_id: salary_basis(
                position=row.position,
                ratings=ratings.get(row.player_id),
                contract=row.contract,
                is_foreign=row.is_foreign,
                entered_year=entered_year_of(draft_year=row.draft_year, joined_year=row.joined_year),
                year=year,
            )
            for row in rows
        }
        return fill_salaries({}, bases)

    def history(self, player_id: int, *, through_year: int) -> list[MemberSalary]:
        """選手ひとりの球団 × 年の年俸（`through_year` 年まで。古い順）。`through_year` 年だけ行が無ければ補う。"""
        rows = self._payroll.for_player(player_id, through_year=through_year)
        filled = self.supplement(
            through_year, [row for row in rows if row.salary is None and row.year == through_year]
        )
        return [_member_salary(row, filled) for row in rows]


def _member_salary(row: PayrollMember, filled: Mapping[int, Salary]) -> MemberSalary:
    if row.salary is not None:
        return MemberSalary(row, Salary(row.salary))
    if row.player_id in filled:
        return MemberSalary(row, filled[row.player_id], supplemented=True)
    return MemberSalary(row, None)


class PennantPayrollViewService:
    """年俸の参照の画面の材料（#40 K1a）。依存は世界の範囲で組み立てて渡す。"""

    def __init__(self, *, salaries: SalaryContractReader, caps: PayrollCapReader) -> None:
        self._salaries = salaries
        # 年俸の総額の上限（#40 K1b。GM ホームのカード）
        self._caps = caps

    def summary(self, world: WorldContext) -> PayrollSummary | None:
        """GM ホームの「年俸」のカード（自軍のいまの年度。上限と残り・超過も）。受け持つ球団が無い・在籍する選手が
        いなければ None。"""
        team_id = world.managed_team_id
        if team_id is None:
            return None
        year = world.season_year
        paid = [
            (item.member, item.salary)
            for item in self._salaries.members(year, [team_id], supplement=True)
            if item.salary is not None
        ]
        if not paid:
            return None
        top_member, top = max(paid, key=lambda pair: (pair[1], -pair[0].player_id))
        total = payroll(salary for _, salary in paid)
        previous = self._salaries.payrolls([year - 1], [team_id], open_year=None).get((team_id, year - 1))
        cap = self._caps.caps(year, [team_id])[team_id]
        return PayrollSummary(
            year=year,
            total=Money(total),
            player_count=len(paid),
            average=round(total / len(paid)),
            top_name=top_member.name,
            top=top,
            previous_total=None if previous is None else Money(previous),
            cap=Money(cap.yen),
            cap_estimated=cap.estimated,
            room=Money(max(0, cap.room(total))),
            overrun=Money(payroll_overrun(total, cap)),
        )

    def team_salaries(self, team_id: int, year: int) -> dict[int, Salary]:
        """球団の `year` 年の選手ごとの年俸（行の無い選手は補う。編成の表の「年俸」列）。"""
        return {
            item.member.player_id: item.salary
            for item in self._salaries.members(year, [team_id], supplement=True)
            if item.salary is not None
        }

    def history(self, world: WorldContext, player_id: int) -> tuple[SalaryHistoryRow, ...]:
        """選手ページの「年俸の推移」（古い順。いまの年度まで）。在籍の無い選手（範囲の外）は空。"""
        return tuple(
            SalaryHistoryRow(
                year=item.member.year,
                team_name=item.member.team_name,
                salary=item.salary,
                supplemented=item.supplemented,
            )
            for item in self._salaries.history(player_id, through_year=world.season_year)
        )

    def renewals(self, world: WorldContext, year: int) -> RenewalSummary | None:
        """締めた年 `year` の契約更改（自軍の残った選手と、世界全体の人数と総額の増減）。

        更改は `year` 年と翌年の両方に契約のある選手（新人と獲得した外国人は翌年だけなので含まない）。
        翌年の契約が1つも無い年（契約を記録する前に締めた年）は None。
        """
        after = self._salaries.recorded(year + 1)
        if not after:
            return None
        before = self._salaries.recorded(year)
        renewed = sorted(set(before) & set(after))
        own: list[RenewalRow] = []
        if world.managed_team_id is not None:
            for item in self._salaries.members(year + 1, [world.managed_team_id], supplement=False):
                player_id = item.member.player_id
                if player_id in before and player_id in after:
                    own.append(
                        RenewalRow(
                            player_id=player_id,
                            name=item.member.name,
                            position=item.member.position,
                            before=before[player_id],
                            after=after[player_id],
                        )
                    )
        return RenewalSummary(
            own=tuple(sorted(own, key=lambda row: (-row.change, row.player_id))),
            total_count=len(renewed),
            total_change=sum(after[pid].yen - before[pid].yen for pid in renewed),
        )
