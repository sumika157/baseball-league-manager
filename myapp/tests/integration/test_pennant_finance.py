"""ペナントのチケットの値段と球団の収支（#208 E2）。

決定（2026-11-14。#371 のコメント「ユーザーの決定」）と計画から:
- GM が手動にした値段は球団 × 年の行として保存する（上書き・お任せに戻す）。変えられるのはその年の開幕前だけ
  （開幕した年は `TicketPriceLocked`、締めた年は `AlreadyClosed`）で、翌年には引き継がない
- 値段はホーム試合の観客数に効く（得点と打席は変わらない）。締めるとき翌年の人気に効く
- 収支は保存せず導く。経営の画面（GET は誰でも・POST はオーナーだけ・変えられない時期はフォームを出さない）、
  GM ホームのカードの要約、オフの結果の「収支」節。人気の行の無い年は「—」
- 実データの範囲には書けない。世界を消せば値段も消える

世界は2リーグ×3球団（公式戦を1週間だけ進め、残りの対戦を消して「公式戦を終えた」年にする。ポストシーズンは「なし」）。
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.db import transaction
from django.test import TestCase
from django.urls import reverse

from myapp.application.pennant_simulation import DaySimulator
from myapp.domain.exceptions import (
    AlreadyAdvanced,
    AlreadyClosed,
    InvalidTicketPrice,
    InvalidWorld,
    TicketPriceLocked,
)
from myapp.domain.pennant.finance import money_label
from myapp.domain.pennant.schedule import AdvanceTarget
from myapp.domain.pennant.ticket_price import ClubTicketPrice, TicketPrice
from myapp.domain.pennant.world import WorldScope
from myapp.domain.value_objects import ContractStatus
from myapp.infrastructure import orm_models
from myapp.infrastructure.queries import DjangoClubFinanceQuery
from myapp.infrastructure.repositories import DjangoFixtureRepository, DjangoTicketPriceRepository
from myapp.presentation.views import (
    build_club_finance_service,
    build_offseason_view_service,
    build_pennant_home_service,
    build_pennant_offseason_service,
    build_pennant_season_service,
    build_pennant_world_service,
    build_pennant_world_view,
    build_ticket_price_service,
)

from .world_case import YEAR, _register_club

SEED = 13
CAPACITY = 30000


class FinanceCase(TestCase):
    """2リーグ×3球団の世界。`teams` は自軍のリーグの3球団（0 が受け持つ球団）、`all_teams` は世界の全球団。"""

    world_id: int
    teams: list[int]
    all_teams: list[int]
    league_id: int
    other_league_id: int
    managed_source_id: int
    owner: User

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user("gm", password="x")
        league = orm_models.League.objects.create(name="経営リーグ")
        other = orm_models.League.objects.create(name="相手リーグ")
        cls.league_id, cls.other_league_id = league.id, other.id
        stadium = orm_models.Stadium.objects.create(name="経営球場", city="東京", capacity=CAPACITY)
        for index in range(3):
            team = orm_models.Team.objects.create(league=league, name=f"経営{index}球団", home_stadium=stadium)
            _register_club(team, f"経{index}")
            if index == 0:
                cls.managed_source_id = team.id
        for index in range(3):
            team = orm_models.Team.objects.create(league=other, name=f"相手{index}球団", home_stadium=stadium)
            _register_club(team, f"相{index}")
        cls.world_id = cls.create_world()
        cls.teams = list(
            orm_models.Team.objects.filter(league__world_id=cls.world_id, name__startswith="経営")
            .order_by("id")
            .values_list("id", flat=True)
        )
        cls.all_teams = list(
            orm_models.Team.objects.filter(league__world_id=cls.world_id).order_by("id").values_list("id", flat=True)
        )

    @classmethod
    def create_world(cls) -> int:
        world_id = (
            build_pennant_world_service()
            .create_world(
                name="経営の世界",
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

    def repository(self, scope=None) -> DjangoTicketPriceRepository:
        return DjangoTicketPriceRepository(scope or WorldScope.pennant(self.world_id))

    def prices(self, world_id=None) -> list[tuple[int, int, str]]:
        world_id = self.world_id if world_id is None else world_id
        return list(
            orm_models.PennantTicketPrice.objects.filter(team__league__world_id=world_id)
            .order_by("year", "team_id")
            .values_list("year", "team_id", "price")
        )

    def advance_week(self) -> None:
        build_pennant_season_service(self.world_id).advance(AdvanceTarget.WEEK)

    def finish_regular_season(self) -> None:
        """公式戦を1週間だけ進め、残りの対戦を消して「公式戦を終えた」年にする。"""
        self.advance_week()
        fixtures = DjangoFixtureRepository(WorldScope.pennant(self.world_id))
        fixtures.remove(fixtures.find_all())

    def close(self) -> None:
        build_pennant_offseason_service(self.world_id).close_season()

    def games(self):
        return list(
            orm_models.Game.objects.filter(home_team__league__world_id=self.world_id)
            .order_by("played_on", "home_team_id")
            .values_list("played_on", "home_team_id", "away_team_id", "home_score", "away_score", "attendance")
        )

    def plate_appearances(self):
        return list(
            orm_models.GamePlateAppearance.objects.filter(game__home_team__league__world_id=self.world_id)
            .order_by("game__played_on", "game__home_team_id", "sequence")
            .values_list("game__played_on", "sequence", "batter_id", "pitcher_id", "result")
        )

    def popularity(self, team_id: int, year: int) -> int:
        return orm_models.PennantClubPopularity.objects.get(team_id=team_id, year=year).value

    def context(self):
        return build_pennant_world_view().get_context(self.world_id)

    def url(self) -> str:
        return reverse("pennant_finance", args=[self.world_id])


class RepositoryTest(FinanceCase):
    def test_saving_overwrites_and_deleting_goes_back_to_auto(self):
        repository = self.repository()
        repository.save(ClubTicketPrice(self.teams[0], YEAR, TicketPrice.HIGH))
        repository.save(ClubTicketPrice(self.teams[0], YEAR, TicketPrice.LOW))
        self.assertEqual(self.prices(), [(YEAR, self.teams[0], "安い")])
        self.assertEqual(repository.find_by_year(YEAR), [ClubTicketPrice(self.teams[0], YEAR, TicketPrice.LOW)])
        self.assertEqual(repository.find_by_team(self.teams[0]), repository.find_by_year(YEAR))

        repository.delete(self.teams[0], YEAR)
        self.assertEqual(self.prices(), [])
        repository.delete(self.teams[0], YEAR)  # 行が無くても何もしない

    def test_the_real_data_and_other_worlds_cannot_hold_prices(self):
        real_team = orm_models.Team.objects.filter(league_id=self.league_id).values_list("id", flat=True).first()
        with self.assertRaises(InvalidWorld):
            self.repository(WorldScope.real()).save(ClubTicketPrice(real_team, YEAR, TicketPrice.HIGH))
        with self.assertRaises(InvalidTicketPrice):
            self.repository().save(ClubTicketPrice(real_team, YEAR, TicketPrice.HIGH))
        other = self.create_world()
        other_team = orm_models.Team.objects.filter(league__world_id=other).values_list("id", flat=True).first()
        self.repository(WorldScope.pennant(other)).save(ClubTicketPrice(other_team, YEAR, TicketPrice.HIGH))
        self.assertEqual(self.repository().find_by_year(YEAR), [], "他の世界の値段は読まない")
        self.assertEqual(self.repository().find_by_team(other_team), [])

    def test_an_opened_year_is_locked_and_a_closed_year_is_closed(self):
        """2026-11-14 ユーザー決定: 変えられるのはその年の開幕前だけ。"""
        self.advance_week()
        with self.assertRaises(TicketPriceLocked):
            self.repository().save(ClubTicketPrice(self.teams[0], YEAR, TicketPrice.HIGH))
        with self.assertRaises(TicketPriceLocked):
            self.repository().delete(self.teams[0], YEAR)
        fixtures = DjangoFixtureRepository(WorldScope.pennant(self.world_id))
        fixtures.remove(fixtures.find_all())
        self.close()
        with self.assertRaises(AlreadyClosed):
            self.repository().save(ClubTicketPrice(self.teams[0], YEAR, TicketPrice.HIGH))
        # 締めた後の翌年は、まだ開幕していないので書ける
        self.repository().save(ClubTicketPrice(self.teams[0], YEAR + 1, TicketPrice.HIGH))
        self.assertEqual(self.prices(), [(YEAR + 1, self.teams[0], "高い")])

    def test_deleting_the_world_deletes_its_prices(self):
        other = self.create_world()
        other_team = orm_models.Team.objects.filter(league__world_id=other).values_list("id", flat=True).first()
        self.repository(WorldScope.pennant(other)).save(ClubTicketPrice(other_team, YEAR, TicketPrice.HIGH))
        self.repository().save(ClubTicketPrice(self.teams[0], YEAR, TicketPrice.HIGH))

        build_pennant_world_service().delete_world(other)

        self.assertEqual(self.prices(other), [])
        self.assertEqual(len(self.prices()), 1, "他の世界の値段は残る")


class TicketPriceServiceTest(FinanceCase):
    def test_the_gm_sets_and_resets_the_price_of_the_managed_club(self):
        service = build_ticket_price_service(self.world_id)
        self.assertTrue(service.change(TicketPrice.SLIGHTLY_HIGH))
        self.assertFalse(service.change(TicketPrice.SLIGHTLY_HIGH), "同じなら何もしない")
        self.assertEqual(self.prices(), [(YEAR, self.teams[0], "やや高い")])
        self.assertTrue(service.change(None))
        self.assertEqual(self.prices(), [])
        self.assertFalse(service.change(None))

    def test_a_manual_price_is_not_carried_over_to_the_next_year(self):
        """2026-11-14 ユーザー決定: 手動の値段は翌年に引き継がない（新しい年はお任せから）。"""
        build_ticket_price_service(self.world_id).change(TicketPrice.HIGH)
        self.finish_regular_season()
        with self.assertRaises(TicketPriceLocked):
            build_ticket_price_service(self.world_id).change(TicketPrice.LOW)
        self.close()

        self.assertEqual(self.prices(), [(YEAR, self.teams[0], "高い")], "締めた年の手動の値段は残る")
        view = build_club_finance_service(self.world_id).view(self.context())
        assert view is not None
        self.assertEqual(view.year, YEAR + 1)
        self.assertIsNone(view.manual)
        self.assertEqual(view.blocker, "")
        # 翌年の値段は翌年の開幕前に変えられる
        self.assertTrue(build_ticket_price_service(self.world_id).change(TicketPrice.LOW))
        self.assertIn((YEAR + 1, self.teams[0], "安い"), self.prices())


class SimulationTest(FinanceCase):
    def test_the_price_changes_attendance_but_not_the_games(self):
        """値段は入場率に効く（高いほど観客が減る）。得点と打席は値段で変わらない（別の乱数源）。"""
        savepoint = transaction.savepoint()
        self.finish_regular_season()
        plain_games, plain_plays = self.games(), self.plate_appearances()
        transaction.savepoint_rollback(savepoint)

        repository = self.repository()
        for team_id in self.all_teams:
            repository.save(ClubTicketPrice(team_id, YEAR, TicketPrice.HIGH))
        self.finish_regular_season()
        expensive_games, expensive_plays = self.games(), self.plate_appearances()

        self.assertEqual([game[:5] for game in expensive_games], [game[:5] for game in plain_games])
        self.assertEqual(expensive_plays, plain_plays)
        self.assertLess(sum(game[5] for game in expensive_games), sum(game[5] for game in plain_games))
        for plain, expensive in zip(plain_games, expensive_games, strict=True):
            self.assertLessEqual(expensive[5], plain[5])

    def test_a_price_changed_while_advancing_is_not_written_with_the_old_attendance(self):
        """進める処理が値段を読んだ後に、最初の試合を書く前に値段が変わったら、古い値段で試合を書かない。"""
        real_play = DaySimulator.play
        changed = []

        def play_and_change(simulator, *args, **kwargs):
            if not changed:
                orm_models.PennantTicketPrice.objects.create(team_id=self.teams[0], year=YEAR, price="高い")
                changed.append(True)
            return real_play(simulator, *args, **kwargs)

        with mock.patch.object(DaySimulator, "play", play_and_change), self.assertRaises(AlreadyAdvanced):
            self.advance_week()
        self.assertEqual(self.games(), [])

    def test_closing_applies_the_price_to_next_years_popularity(self):
        """高い値段の球団は、同じ成績・同じぶれでも翌年の人気が 2 下がる（世界の平均は引かない）。"""
        savepoint = transaction.savepoint()
        self.finish_regular_season()
        self.close()
        plain = {team_id: self.popularity(team_id, YEAR + 1) for team_id in self.all_teams}
        transaction.savepoint_rollback(savepoint)

        self.repository().save(ClubTicketPrice(self.teams[0], YEAR, TicketPrice.HIGH))
        self.finish_regular_season()
        self.close()
        expensive = {team_id: self.popularity(team_id, YEAR + 1) for team_id in self.all_teams}

        self.assertEqual(expensive[self.teams[0]], plain[self.teams[0]] - 2)
        self.assertEqual(
            {k: v for k, v in expensive.items() if k != self.teams[0]},
            {k: v for k, v in plain.items() if k != self.teams[0]},
        )


class FinanceViewTest(FinanceCase):
    def test_the_finance_of_a_closed_year_is_derived_from_the_games(self):
        self.repository().save(ClubTicketPrice(self.teams[0], YEAR, TicketPrice.SLIGHTLY_LOW))
        self.finish_regular_season()
        self.close()

        rows = build_club_finance_service(self.world_id).year_rows(self.context(), YEAR, self.teams)
        own = rows[self.teams[0]]
        home_total = sum(game[5] for game in self.games() if game[1] == self.teams[0])
        self.assertEqual(own.regular_attendance, home_total)
        self.assertEqual((own.ticket_price, own.manual_price), (TicketPrice.SLIGHTLY_LOW, True))
        assert own.figures is not None
        self.assertEqual(own.figures.gate.amount, home_total * TicketPrice.SLIGHTLY_LOW.unit_price)
        self.assertFalse(own.in_progress)
        # 人件費はその年に在籍した人数（全員支配下）× 4,000万円
        (size,) = DjangoClubFinanceQuery(WorldScope.pennant(self.world_id)).roster_sizes(
            years=[YEAR], team_ids=[self.teams[0]]
        )
        self.assertGreater(size.registered, 0)
        self.assertEqual(own.figures.payroll.amount, 40_000_000 * size.registered + 5_000_000 * size.developmental)
        rival = rows[self.teams[1]]
        self.assertEqual((rival.ticket_price, rival.manual_price), (TicketPrice.STANDARD, False))

    def test_years_without_popularity_show_no_finance(self):
        """人気の行の無い年（この機能より前の年）は収支を出さない（2026-11-14 ユーザー決定）。"""
        self.finish_regular_season()
        self.close()
        orm_models.PennantClubPopularity.objects.filter(team__league__world_id=self.world_id, year=YEAR).delete()

        view = build_club_finance_service(self.world_id).view(self.context())
        assert view is not None
        (past,) = view.history
        self.assertEqual(past.year, YEAR)
        self.assertIsNone(past.figures)
        self.assertIsNone(past.popularity)
        summary = build_offseason_view_service(self.world_id).get_summary(self.context(), YEAR)
        assert summary is not None
        self.assertEqual(summary.finance, ())

    def test_the_current_year_is_prorated_and_is_zero_before_the_opening(self):
        view = build_club_finance_service(self.world_id).view(self.context())
        assert view is not None
        self.assertTrue(view.current.in_progress)
        assert view.current.figures is not None
        self.assertEqual((view.current.figures.revenue.amount, view.current.figures.expenses.amount), (0, 0))
        self.assertEqual(view.history, ())

        self.advance_week()
        view = build_club_finance_service(self.world_id).view(self.context())
        assert view is not None and view.current.figures is not None
        self.assertGreater(view.current.figures.payroll.amount, 0)
        self.assertGreater(view.current.figures.gate.amount, 0)
        self.assertNotEqual(view.blocker, "")


class ScreenTest(FinanceCase):
    def test_anyone_can_read_but_only_the_owner_gets_the_form(self):
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["form"])
        self.assertNotContains(response, "変更する")

        self.client.force_login(User.objects.create_user("other", password="x"))
        self.assertIsNone(self.client.get(self.url()).context["form"])

        self.client.force_login(self.owner)
        response = self.client.get(self.url())
        self.assertIsNotNone(response.context["form"])
        self.assertContains(response, "変更する")
        self.assertContains(response, "お任せ（人気で決める）")

    def test_only_the_owner_can_post(self):
        anonymous = self.client.post(self.url(), {"ticket_price": "HIGH"})
        self.assertEqual(anonymous.status_code, 302)
        self.assertIn(reverse("login"), anonymous["Location"])
        self.client.force_login(User.objects.create_user("other", password="x"))
        self.assertEqual(self.client.post(self.url(), {"ticket_price": "HIGH"}).status_code, 403)
        self.assertEqual(self.prices(), [])

        self.client.force_login(self.owner)
        response = self.client.post(self.url(), {"ticket_price": "HIGH"}, follow=True)
        self.assertContains(response, f"{YEAR}年のチケットの値段を「高い」にしました。")
        self.assertEqual(self.prices(), [(YEAR, self.teams[0], "高い")])
        response = self.client.post(self.url(), {"ticket_price": "AUTO"}, follow=True)
        self.assertContains(response, "「お任せ」にしました。")
        self.assertEqual(self.prices(), [])
        self.assertEqual(self.client.post(self.url(), {"ticket_price": "NOPE"}).status_code, 200)

    def test_after_the_opening_the_form_is_replaced_by_the_reason(self):
        """変えられない時期は、フォームを出さずに理由だけを出す（両立しない操作を並べない）。"""
        self.advance_week()
        self.client.force_login(self.owner)
        response = self.client.get(self.url())
        self.assertIsNone(response.context["form"])
        self.assertContains(response, "開幕したので")
        response = self.client.post(self.url(), {"ticket_price": "HIGH"}, follow=True)
        self.assertContains(response, "開幕したので")
        self.assertEqual(self.prices(), [])

    def test_the_home_card_and_the_offseason_show_the_finance(self):
        self.finish_regular_season()

        home = build_pennant_home_service(self.world_id).get_home(self.context(), include_advance=False)
        assert home.finance is not None and home.finance.figures is not None
        self.assertEqual(home.finance.year, YEAR)
        content = self.client.get(reverse("pennant_world", args=[self.world_id])).content.decode()
        self.assertIn("経営を見る", content)
        self.assertIn(home.finance.figures.operating_balance.label, content)

        self.close()
        summary = build_offseason_view_service(self.world_id).get_summary(self.context(), YEAR)
        assert summary is not None
        self.assertEqual({row.team_id for row in summary.finance}, set(self.teams))
        balances = [row.finance.figures.operating_balance.amount for row in summary.finance if row.finance.figures]
        self.assertEqual(balances, sorted(balances, reverse=True))
        content = self.client.get(reverse("pennant_offseason", args=[self.world_id, YEAR])).content.decode()
        self.assertIn(f"{YEAR}年の収支", content)
        self.assertIn(money_label(balances[0]), content)

        finance = self.client.get(self.url())
        self.assertContains(finance, "過去の年の収支")
        self.assertContains(finance, f"{YEAR}年")


class RosterSizeQueryTest(FinanceCase):
    def test_registered_and_developmental_players_are_counted_per_year(self):
        """その年の区分で数える（育成で入って昇格した選手は昇格の年から支配下）。同じ年に2回在籍しても1人。"""
        team_id = self.teams[0]
        query = DjangoClubFinanceQuery(WorldScope.pennant(self.world_id))
        (before,) = query.roster_sizes(years=[YEAR], team_ids=[team_id])
        player = orm_models.Player.objects.create(name="育成から昇格", position="投手")
        orm_models.PlayerStint.objects.create(
            player=player,
            team_id=team_id,
            number="120",
            from_year=YEAR - 1,
            to_year=YEAR,
            signed_as=ContractStatus.DEVELOPMENTAL.value,
        )
        orm_models.PlayerStint.objects.create(
            player=player,
            team_id=team_id,
            number="70",
            from_year=YEAR,
            signed_as=ContractStatus.DEVELOPMENTAL.value,
            promoted_year=YEAR + 1,
            number_before_promotion="121",
        )

        sizes = {row.year: row for row in query.roster_sizes(years=[YEAR, YEAR + 1], team_ids=[team_id])}

        self.assertEqual(sizes[YEAR].developmental, before.developmental + 1)
        self.assertEqual(sizes[YEAR].registered, before.registered)
        self.assertEqual(sizes[YEAR + 1].registered, before.registered + 1)
        self.assertEqual(query.roster_sizes(years=[], team_ids=[team_id]), [])
        self.assertEqual(
            query.stadium_capacities([team_id, self.all_teams[-1]]),
            {
                team_id: CAPACITY,
                self.all_teams[-1]: CAPACITY,
            },
        )
