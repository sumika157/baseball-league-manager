"""ペナントの年俸の契約と契約更改（#40 K1a）。

計画（#40 issuecomment-6891228201 の K1a）と 2026-11-15 のユーザーの決定から:
- 年俸の契約は選手 × 期間の行として保存する。書くのは世界の作成（開幕年）・シーズンを締める処理（翌年と、行の無い
  選手の締める年の補った値）・期中の外国人の獲得（その年）だけ
- 締めた年の契約は書き換えない（`AlreadyClosed`）。同じ選手の期間は重ならない。実データの範囲には書けない
- 締める処理は、残る選手を AI の規則で更改し、新人と獲得した外国人の年俸も書く（1つのトランザクション）
- この機能より前の世界（行が無い）は読む側が補い、締めるときに締める年の補った値を書く（画面で補った値と一致する）
- 人件費は年俸の合計（参照クエリの和と domain の和が一致する）
- GM ホームの「年俸」カード・選手ページの「年俸の推移」・編成の表の「年俸」列・オフの結果の「契約更改」節
  （書く導線は無い）

世界は2リーグ×3球団（公式戦を1週間だけ進め、残りの対戦を消して「公式戦を終えた」年にする。ポストシーズンは「なし」）。
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from myapp.domain.exceptions import AlreadyClosed, InvalidSalaryContract, InvalidWorld, PlayerNotFound
from myapp.domain.pennant.contract import Salary, SalaryContract
from myapp.domain.pennant.salary import MIN_DEVELOPMENTAL_SALARY, MIN_REGISTERED_SALARY, payroll, salary_floor
from myapp.domain.pennant.schedule import AdvanceTarget
from myapp.domain.pennant.world import WorldScope
from myapp.domain.value_objects import ContractStatus
from myapp.infrastructure import orm_models
from myapp.infrastructure.queries import DjangoPayrollQuery
from myapp.infrastructure.repositories import (
    DjangoFixtureRepository,
    DjangoSalaryContractRepository,
    DjangoWorldRepository,
)
from myapp.presentation.views import (
    build_club_finance_service,
    build_offseason_view_service,
    build_pennant_home_service,
    build_pennant_offseason_service,
    build_pennant_payroll_view_service,
    build_pennant_season_service,
    build_pennant_world_service,
    build_pennant_world_view,
)

from .world_case import YEAR, _register_club

SEED = 17


class SalaryCase(TestCase):
    """2リーグ×3球団の世界。`teams` は自軍のリーグの3球団（0 が受け持つ球団）。"""

    world_id: int
    teams: list[int]
    league_id: int
    other_league_id: int
    managed_source_id: int
    owner: User

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user("gm", password="x")
        league = orm_models.League.objects.create(name="年俸リーグ")
        other = orm_models.League.objects.create(name="相手リーグ")
        cls.league_id, cls.other_league_id = league.id, other.id
        stadium = orm_models.Stadium.objects.create(name="年俸球場", city="東京", capacity=30000)
        for index in range(3):
            team = orm_models.Team.objects.create(league=league, name=f"年俸{index}球団", home_stadium=stadium)
            _register_club(team, f"年{index}")
            if index == 0:
                cls.managed_source_id = team.id
        for index in range(3):
            team = orm_models.Team.objects.create(league=other, name=f"相手{index}球団", home_stadium=stadium)
            _register_club(team, f"相{index}")
        cls.world_id = cls.create_world()
        cls.teams = list(
            orm_models.Team.objects.filter(league__world_id=cls.world_id, name__startswith="年俸")
            .order_by("id")
            .values_list("id", flat=True)
        )

    @classmethod
    def create_world(cls) -> int:
        world_id = (
            build_pennant_world_service()
            .create_world(
                name="年俸の世界",
                owner_id=None,
                source_league_ids=[cls.league_id, cls.other_league_id],
                start_year=YEAR,
                seed=SEED,
                managed_source_team_id=cls.managed_source_id,
            )
            .world.id
        )
        orm_models.PennantWorld.objects.filter(id=world_id).update(owner_id=cls.owner.id)
        return world_id

    # --- 道具 ---

    def scope(self) -> WorldScope:
        return WorldScope.pennant(self.world_id)

    def contracts(self) -> list[tuple[int, int, int, int]]:
        return list(
            orm_models.PennantSalaryContract.objects.filter(player__stints__team__league__world_id=self.world_id)
            .distinct()
            .order_by("player_id", "first_year")
            .values_list("player_id", "first_year", "last_year", "salary")
        )

    def active_player_ids(self) -> set[int]:
        return set(
            orm_models.PlayerStint.objects.filter(team__league__world_id=self.world_id, to_year=None).values_list(
                "player_id", flat=True
            )
        )

    def finish_regular_season(self) -> None:
        build_pennant_season_service(self.world_id).advance(AdvanceTarget.WEEK)
        fixtures = DjangoFixtureRepository(self.scope())
        fixtures.remove(fixtures.find_all())

    def close(self):
        return build_pennant_offseason_service(self.world_id).close_season()

    def context(self):
        return build_pennant_world_view().get_context(self.world_id)


class WorldCreationTest(SalaryCase):
    def test_creating_a_world_writes_a_one_year_contract_for_every_player(self):
        rows = self.contracts()
        self.assertEqual({row[0] for row in rows}, self.active_player_ids())
        self.assertTrue(all((first, last) == (YEAR, YEAR) for _, first, last, _ in rows))
        members = DjangoPayrollQuery(self.scope()).members(years=[YEAR], team_ids=self.teams)
        for member in members:
            minimum = (
                MIN_DEVELOPMENTAL_SALARY if member.contract is ContractStatus.DEVELOPMENTAL else MIN_REGISTERED_SALARY
            )
            with self.subTest(player=member.name):
                assert member.salary is not None
                self.assertGreaterEqual(member.salary, minimum)

    def test_deleting_the_world_deletes_its_contracts(self):
        self.assertTrue(self.contracts())
        DjangoWorldRepository().delete(self.world_id)
        self.assertFalse(orm_models.PennantSalaryContract.objects.exists())


class RepositoryTest(SalaryCase):
    def test_the_real_data_cannot_hold_contracts(self):
        with self.assertRaises(InvalidWorld):
            DjangoSalaryContractRepository(WorldScope.real()).add_all(
                [SalaryContract.for_year(1, YEAR, Salary(MIN_REGISTERED_SALARY))]
            )

    def test_overlaps_and_players_outside_the_world_are_rejected(self):
        repository = DjangoSalaryContractRepository(self.scope())
        player_id = next(iter(self.active_player_ids()))
        with self.assertRaises(InvalidSalaryContract):
            repository.add_all([SalaryContract.for_year(player_id, YEAR, Salary(MIN_REGISTERED_SALARY))])
        outsider = orm_models.Player.objects.create(name="世界の外", position="投手")
        with self.assertRaises(PlayerNotFound):
            repository.add_all([SalaryContract.for_year(outsider.id, YEAR + 1, Salary(MIN_REGISTERED_SALARY))])
        # どれかが書けなければ何も書かない
        before = self.contracts()
        with self.assertRaises(InvalidSalaryContract):
            repository.add_all(
                [
                    SalaryContract.for_year(player_id, YEAR + 1, Salary(MIN_REGISTERED_SALARY)),
                    SalaryContract.for_year(player_id, YEAR, Salary(MIN_REGISTERED_SALARY)),
                ]
            )
        self.assertEqual(self.contracts(), before)

    def test_reads_are_limited_to_the_world(self):
        other_world = self.create_world()
        mine = DjangoSalaryContractRepository(self.scope()).find_by_year(YEAR)
        theirs = DjangoSalaryContractRepository(WorldScope.pennant(other_world)).find_by_year(YEAR)
        self.assertTrue(mine and theirs)
        self.assertFalse({c.player_id for c in mine} & {c.player_id for c in theirs})
        self.assertEqual(
            DjangoSalaryContractRepository(self.scope()).for_players([c.player_id for c in theirs]),
            [],
            "他の世界は読めない",
        )

    def test_the_payroll_query_sums_to_the_domain_payroll(self):
        """参照クエリの球団 × 年の年俸と、domain の `payroll`（年俸の合計）が一致する。"""
        contracts = {c.player_id: c.salary for c in DjangoSalaryContractRepository(self.scope()).find_by_year(YEAR)}
        for team_id in self.teams:
            members = DjangoPayrollQuery(self.scope()).members(years=[YEAR], team_ids=[team_id])
            with self.subTest(team=team_id):
                self.assertEqual(
                    sum(member.salary or 0 for member in members),
                    payroll(contracts[member.player_id] for member in members),
                )


class CloseTest(SalaryCase):
    def test_closing_renews_the_survivors_and_writes_newcomers(self):
        self.finish_regular_season()
        before = {c.player_id: c.salary for c in DjangoSalaryContractRepository(self.scope()).find_by_year(YEAR)}

        result = self.close()

        after = {c.player_id: c for c in DjangoSalaryContractRepository(self.scope()).find_by_year(YEAR + 1)}
        self.assertEqual(set(after), self.active_player_ids(), "翌年に在籍する全員に翌年の契約がある")
        renewed = set(before) & set(after)
        self.assertEqual(result.renewal_count, len(renewed))
        self.assertEqual(result.renewal_change, sum(after[p].salary.yen - before[p].yen for p in renewed))
        members = {
            m.player_id: m for m in DjangoPayrollQuery(self.scope()).members(years=[YEAR + 1], team_ids=self.teams)
        }
        for player_id in renewed:
            contract = after[player_id]
            self.assertEqual((contract.first_year, contract.last_year), (YEAR + 1, YEAR + 1))
            if player_id in members:
                floor = salary_floor(before[player_id], contract=members[player_id].contract)
                self.assertGreaterEqual(contract.salary, floor, "減額の下限より下げない")
        self.assertTrue(set(after) - set(before), "新人と獲得した外国人にも翌年の契約がある")

    def test_closing_twice_does_not_add_contracts_and_the_closed_year_is_locked(self):
        self.finish_regular_season()
        self.close()
        written = self.contracts()
        with self.assertRaises(AlreadyClosed):
            self.close()
        self.assertEqual(self.contracts(), written)
        player_id = next(iter(self.active_player_ids()))
        with self.assertRaises(AlreadyClosed):
            DjangoSalaryContractRepository(self.scope()).add_all(
                [SalaryContract(player_id=player_id, first_year=YEAR - 1, last_year=YEAR, salary=Salary(500 * 10_000))]
            )

    def test_a_failure_while_writing_leaves_no_contract(self):
        self.finish_regular_season()
        before = self.contracts()
        with (
            mock.patch.object(DjangoFixtureRepository, "add_all", side_effect=RuntimeError("日程で失敗")),
            self.assertRaises(RuntimeError),
        ):
            self.close()
        self.assertEqual(self.contracts(), before)

    def test_a_world_from_before_this_feature_is_filled_and_written_when_closing(self):
        """行の無い世界も読めて締められる。締めるときに締める年の補った値を書き、それは締める前に画面が補った値と同じ。"""
        orm_models.PennantSalaryContract.objects.filter(player__stints__team__league__world_id=self.world_id).delete()
        self.finish_regular_season()
        team_id = self.teams[0]
        shown = build_pennant_payroll_view_service(self.world_id).team_salaries(team_id, YEAR)
        finance = build_club_finance_service(self.world_id).year_rows(self.context(), YEAR, [team_id])[team_id]
        assert finance.figures is not None
        self.assertEqual(finance.figures.payroll.amount, payroll(shown.values()), "いまの年度は補って人件費にする")

        self.close()

        written = {c.player_id: c.salary for c in DjangoSalaryContractRepository(self.scope()).find_by_year(YEAR)}
        self.assertEqual({p: written[p] for p in shown}, shown)
        self.assertTrue(DjangoSalaryContractRepository(self.scope()).find_by_year(YEAR + 1))

    def test_a_closed_year_without_contracts_shows_no_finance(self):
        """契約の行の無い前の年（この機能より前に締めた年）は人件費が分からないので収支を出さない。"""
        self.finish_regular_season()
        self.close()
        orm_models.PennantSalaryContract.objects.filter(
            player__stints__team__league__world_id=self.world_id, first_year=YEAR
        ).delete()
        row = build_club_finance_service(self.world_id).year_rows(self.context(), YEAR, [self.teams[0]])[self.teams[0]]
        self.assertIsNone(row.figures)
        summary = build_offseason_view_service(self.world_id).get_summary(self.context(), YEAR)
        assert summary is not None
        self.assertEqual(summary.finance, ())


class ScreenTest(SalaryCase):
    def test_the_home_shows_the_payroll_card(self):
        home = build_pennant_home_service(self.world_id).get_home(self.context(), include_advance=False)
        assert home.payroll is not None
        own = DjangoPayrollQuery(self.scope()).members(years=[YEAR], team_ids=[self.teams[0]])
        self.assertEqual(home.payroll.total.amount, sum(m.salary or 0 for m in own))
        self.assertEqual(home.payroll.player_count, len(own))
        self.assertEqual(home.payroll.top.yen, max(m.salary or 0 for m in own))
        self.assertIsNone(home.payroll.previous_total, "開幕年の前の年は無い")
        content = self.client.get(reverse("pennant_world", args=[self.world_id])).content.decode()
        self.assertIn(f"年俸（{YEAR}年）", content)
        self.assertIn(home.payroll.total.label, content)

    def test_the_player_page_and_the_club_table_show_salaries(self):
        member = DjangoPayrollQuery(self.scope()).members(years=[YEAR], team_ids=[self.teams[0]])[0]
        assert member.salary is not None
        url = reverse("pennant_player_detail", args=[self.world_id, self.teams[0], member.player_id])
        response = self.client.get(url)
        self.assertContains(response, "年俸の推移")
        self.assertContains(response, Salary(member.salary).label)

        self.client.force_login(self.owner)
        club = self.client.get(reverse("pennant_club", args=[self.world_id]))
        self.assertContains(club, '<th class="text-end">年俸</th>', count=1, html=False)
        # 年俸のセルは選手1人に1つ（列がずれない。セルを2回書いていた不具合の再発防止）
        content = club.content.decode()
        self.assertEqual(content.count('<td class="text-end text-nowrap">'), len(club.context["club_players"]))

    def test_years_without_contracts_show_a_dash_in_the_player_history(self):
        self.finish_regular_season()
        self.close()
        member = DjangoPayrollQuery(self.scope()).members(years=[YEAR], team_ids=[self.teams[0]])[0]
        orm_models.PennantSalaryContract.objects.filter(player_id=member.player_id, first_year=YEAR).delete()
        rows = build_pennant_payroll_view_service(self.world_id).history(self.context(), member.player_id)
        by_year = {row.year: row for row in rows}
        self.assertIsNone(by_year[YEAR].salary)
        self.assertEqual(by_year[YEAR].label, "—")
        self.assertIsNotNone(by_year[YEAR + 1].salary)

    def test_the_offseason_shows_the_renewals(self):
        self.finish_regular_season()
        result = self.close()
        summary = build_offseason_view_service(self.world_id).get_summary(self.context(), YEAR)
        assert summary is not None and summary.renewals is not None
        self.assertEqual(summary.renewals.total_count, result.renewal_count)
        self.assertEqual(summary.renewals.total_change, result.renewal_change)
        self.assertTrue(summary.renewals.own)
        changes = [row.change for row in summary.renewals.own]
        self.assertEqual(changes, sorted(changes, reverse=True))
        content = self.client.get(reverse("pennant_offseason", args=[self.world_id, YEAR])).content.decode()
        self.assertIn(f"契約更改（{YEAR}年 → {YEAR + 1}年）", content)
        self.assertIn(summary.renewals.own[0].after.label, content)
