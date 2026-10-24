"""ペナントのポストシーズンの進行と、締める処理の検査（#39 段階 S3）。

世界は2リーグ×3球団（CS のファーストステージ・ファイナル・日本シリーズがすべて組める最小）。
公式戦は1週間だけ進め、残りの対戦を消して「公式戦を終えた世界」にする（143試合を回さずに速く）。
勝ち上がりは保存せず、順位（公式戦だけ）とポストシーズンの試合の結果から毎回導く。
"""

from __future__ import annotations

from datetime import date, timedelta
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import SimpleTestCase

from myapp.application.pennant_simulation import game_key
from myapp.domain.exceptions import (
    AlreadyAdvanced,
    AlreadyClosed,
    InvalidPostseason,
    InvalidSchedule,
    PostseasonNotFinished,
    SeasonNotFinished,
)
from myapp.domain.pennant.postseason import DAYS_BEFORE_POSTSEASON, PostseasonFormat, PostseasonStatus, SeriesStage
from myapp.domain.pennant.schedule import AdvanceTarget, Fixture
from myapp.domain.pennant.world import WorldScope
from myapp.domain.value_objects import DepartureRoute, GameStage, Position
from myapp.infrastructure import orm_models
from myapp.infrastructure.queries import (
    DjangoGameListQuery,
    DjangoPostseasonQuery,
    DjangoSeasonPlayingTimeQuery,
    DjangoSimulationContextQuery,
)
from myapp.infrastructure.repositories import DjangoFixtureRepository, DjangoWorldRepository
from myapp.presentation.views import (
    build_pennant_offseason_service,
    build_pennant_postseason_service,
    build_pennant_season_service,
    build_pennant_world_view,
    build_world_league_view_service,
)

from .world_case import PostseasonWorldCase

YEAR = 2026
DECIDE_RETIREMENTS = "myapp.domain.pennant.offseason.decide_retirements"


class AdvancePostseasonTest(PostseasonWorldCase):
    def test_the_world_keeps_its_format(self):
        self.assertIs(DjangoWorldRepository().find_by_id(self.npb_world).postseason_format, PostseasonFormat.NPB)
        self.assertIs(DjangoWorldRepository().find_by_id(self.none_world).postseason_format, PostseasonFormat.NONE)
        self.assertIs(self.bracket(self.npb_world).status, PostseasonStatus.NOT_STARTED)

    def test_one_day_at_a_time_equals_to_the_end_at_once(self):
        """1日ずつ進めても、自軍の試合ごとでも、終わりまで一度に進めても同じ試合になる（再現性）。"""
        at_once = self.replay(self.npb_world, lambda: self.advance(self.npb_world, AdvanceTarget.POSTSEASON_END))
        one_by_one = self.replay(self.npb_world, lambda: self.advance_to_the_end(self.npb_world))
        by_own_games = self.replay(
            self.npb_world, lambda: self.advance_to_the_end(self.npb_world, AdvanceTarget.NEXT_MANAGED_GAME)
        )

        self.assertGreater(len(at_once), 0)
        self.assertEqual(at_once, one_by_one)
        self.assertEqual(at_once, by_own_games)
        self.assertEqual(
            at_once, self.replay(self.npb_world, lambda: self.advance(self.npb_world, AdvanceTarget.POSTSEASON_END))
        )

    def test_it_goes_through_the_bracket_to_a_champion(self):
        last_regular = DjangoPostseasonQuery(WorldScope.pennant(self.npb_world)).last_regular_date(YEAR)
        report = self.advance(self.npb_world, AdvanceTarget.POSTSEASON_END)

        self.assertIs(report.status, PostseasonStatus.FINISHED)
        self.assertTrue(report.finished)
        self.assertIsNotNone(report.champion_team_id)
        self.assertTrue(report.champion_name)
        self.assertEqual(report.played_dates[0], last_regular + timedelta(days=DAYS_BEFORE_POSTSEASON))
        bracket = self.bracket(self.npb_world)
        self.assertIs(bracket.status, PostseasonStatus.FINISHED)
        self.assertEqual(bracket.champion, report.champion_team_id)
        stages = [series.stage for series in bracket.series()]
        self.assertEqual(stages.count(SeriesStage.FIRST), 2)
        self.assertEqual(stages.count(SeriesStage.FINAL), 2)
        self.assertEqual(stages.count(SeriesStage.JAPAN_SERIES), 1)
        games = self.postseason_games(self.npb_world)
        self.assertEqual(games.count(), report.games)
        self.assertEqual(len(bracket.results), report.games)
        # 試合は世界の今日を進めるが、年はまたがない
        self.assertTrue(all(game.year == YEAR for game in games))
        self.assertEqual(
            DjangoSimulationContextQuery(WorldScope.pennant(self.npb_world)).last_played_on(), report.today
        )

    def test_the_first_stage_is_played_at_the_second_place_teams_home(self):
        report = self.advance(self.npb_world)
        bracket = self.bracket(self.npb_world)
        firsts = [series for series in bracket.series() if series.stage is SeriesStage.FIRST]

        self.assertEqual(report.games, 2, "2リーグの FS の第1戦が同じ日にある")
        for series in firsts:
            self.assertEqual(series.games[0].home_team_id, series.upper_team_id)

    def test_the_year_and_the_ratings_year_stay_in_the_same_season(self):
        self.advance(self.npb_world, AdvanceTarget.POSTSEASON_END)
        context = build_pennant_world_view().get_context(self.npb_world)

        self.assertEqual(context.season_year, YEAR)
        self.assertGreater(context.today, date(YEAR, 3, 1))

    def test_a_finished_postseason_has_nothing_to_play(self):
        self.advance(self.npb_world, AdvanceTarget.POSTSEASON_END)
        before = self.postseason_games(self.npb_world).count()

        report = self.advance(self.npb_world)

        self.assertEqual(report.played_dates, ())
        self.assertIs(report.status, PostseasonStatus.FINISHED)
        self.assertEqual(self.postseason_games(self.npb_world).count(), before)

    def test_a_world_without_a_postseason_plays_nothing(self):
        report = self.advance(self.none_world, AdvanceTarget.POSTSEASON_END)

        self.assertEqual(report.played_dates, ())
        self.assertIs(report.status, PostseasonStatus.NONE)
        self.assertFalse(self.postseason_games(self.none_world).exists())

    def test_only_the_postseason_ranges_are_accepted(self):
        for target in (AdvanceTarget.WEEK, AdvanceTarget.MONTH_END, AdvanceTarget.LIMIT, AdvanceTarget.SEASON_END):
            with self.subTest(target=target), self.assertRaises(InvalidSchedule):
                self.advance(self.npb_world, target)
        self.assertFalse(self.postseason_games(self.npb_world).exists())

    def test_the_regular_season_cannot_use_the_postseason_range(self):
        with self.assertRaises(InvalidSchedule):
            build_pennant_season_service(self.npb_world).advance(AdvanceTarget.POSTSEASON_END)

    def test_a_stale_screen_is_refused(self):
        today = DjangoSimulationContextQuery(WorldScope.pennant(self.npb_world)).last_played_on()
        with self.assertRaises(AlreadyAdvanced):
            self.advance(self.npb_world, expected_today=today - timedelta(days=1))
        self.assertFalse(self.postseason_games(self.npb_world).exists())

        self.assertEqual(self.advance(self.npb_world, expected_today=today).games, 2)

    def test_advancing_twice_at_the_same_time_makes_no_double_games(self):
        """計算している間に別のリクエストが進めたら、書かずに AlreadyAdvanced（同じ日の試合を2つ作らない）。"""
        service = build_pennant_postseason_service(self.npb_world)
        atomic = service._atomic

        def racing():
            self.advance(self.npb_world)
            return atomic()

        service._atomic = racing
        with self.assertRaises(AlreadyAdvanced):
            service.advance(AdvanceTarget.DAY)

        games = self.postseason_games(self.npb_world)
        self.assertEqual(games.count(), 2, "先に進めた1日ぶんだけ")
        self.assertEqual(len({(g.played_on, g.home_team_id) for g in games}), 2)

    def test_the_postseason_does_not_change_the_regular_season(self):
        """ポストシーズンの試合は、順位表・試合一覧・オフの出場機会（公式戦の集計）に混ざらない。"""
        scope = WorldScope.pennant(self.npb_world)

        def regular():
            return (
                build_world_league_view_service(self.npb_world).get_league_standings(YEAR),
                DjangoGameListQuery(scope).list_rows(year=YEAR),
                DjangoSeasonPlayingTimeQuery(scope).for_year(YEAR),
                DjangoPostseasonQuery(scope).last_regular_date(YEAR),
            )

        before = regular()
        self.advance(self.npb_world, AdvanceTarget.POSTSEASON_END)

        self.assertTrue(self.postseason_games(self.npb_world).exists())
        self.assertEqual(before, regular())

    def test_the_postseason_cannot_start_during_the_regular_season(self):
        teams = list(orm_models.Team.objects.filter(league__world_id=self.npb_world).values_list("id", flat=True))
        DjangoFixtureRepository(WorldScope.pennant(self.npb_world)).add_all(
            [Fixture(date(YEAR, 11, 30), teams[0], teams[1])]
        )

        with self.assertRaises(SeasonNotFinished):
            self.advance(self.npb_world)
        self.assertFalse(self.postseason_games(self.npb_world).exists())


class ClosingTest(PostseasonWorldCase):
    def test_the_season_cannot_be_closed_until_the_postseason_is_over(self):
        offseason = build_pennant_offseason_service(self.npb_world)
        scope = WorldScope.pennant(self.npb_world)

        option = offseason.close_option()
        self.assertFalse(option.can_close)
        self.assertIn("ポストシーズン", option.reason)
        with self.assertRaises(PostseasonNotFinished):
            offseason.close_season()
        self.advance(self.npb_world)
        self.assertIs(self.bracket(self.npb_world).status, PostseasonStatus.IN_PROGRESS)
        with self.assertRaises(PostseasonNotFinished):
            offseason.close_season()
        # 何も書いていない（翌年の能力・日程が無い）
        self.assertFalse(orm_models.PennantPlayerRatings.objects.filter(year=YEAR + 1).exists())
        self.assertEqual(DjangoFixtureRepository(scope).find_all(), [])

        self.advance(self.npb_world, AdvanceTarget.POSTSEASON_END)
        self.assertTrue(offseason.close_option().can_close)
        closed = offseason.close_season(expected_year=YEAR)

        self.assertEqual(closed.year, YEAR)
        self.assertTrue(DjangoFixtureRepository(scope).find_all())
        # 締めた後は、ポストシーズンを進められない
        with self.assertRaises(AlreadyClosed):
            self.advance(self.npb_world)

    def test_a_world_without_a_postseason_closes_right_away(self):
        offseason = build_pennant_offseason_service(self.none_world)

        self.assertTrue(offseason.close_option().can_close)
        self.assertEqual(offseason.close_season().year, YEAR)


class ReleasesDuringThePostseasonTest(PostseasonWorldCase):
    """戦力外は公式戦を終えれば選べる（ポストシーズン中も）。指名を使うのは締めるときだけ。"""

    def test_the_release_tab_is_open_during_the_postseason(self):
        offseason = build_pennant_offseason_service(self.npb_world)
        self.advance(self.npb_world)

        self.assertTrue(offseason.can_choose_releases())
        view = offseason.release_view()
        self.assertEqual(view.year, YEAR)
        self.assertFalse(offseason.close_option().can_close, "締めるのはポストシーズンが終わってから")

    def test_releases_saved_during_the_postseason_are_used_when_the_season_is_closed(self):
        offseason = build_pennant_offseason_service(self.npb_world)
        self.advance(self.npb_world)
        # 指名打者は位置の最低人数が無いので、この小さな名簿でも外せる
        chosen = next(
            p.player_id for p in offseason.release_view().players if p.position is Position.DESIGNATED_HITTER
        )

        offseason.set_releases([chosen], expected_year=YEAR)
        with self.assertRaises(PostseasonNotFinished):
            offseason.close_season(expected_year=YEAR)
        self.assertEqual(offseason.close_option().reason, f"{YEAR}年のポストシーズンがまだ終わっていません。")

        self.advance(self.npb_world, AdvanceTarget.POSTSEASON_END)
        option = offseason.close_option()
        self.assertTrue(option.can_close)
        self.assertEqual(option.release_count, 1)
        with mock.patch(DECIDE_RETIREMENTS, return_value=()):
            offseason.close_season(expected_year=YEAR)

        stint = orm_models.PlayerStint.objects.get(player_id=chosen, team__league__world_id=self.npb_world)
        self.assertEqual((stint.to_year, stint.left_via), (YEAR, DepartureRoute.RELEASED.value))

    def test_the_release_tab_stays_closed_during_the_regular_season(self):
        teams = list(orm_models.Team.objects.filter(league__world_id=self.npb_world).values_list("id", flat=True))
        DjangoFixtureRepository(WorldScope.pennant(self.npb_world)).add_all(
            [Fixture(date(YEAR, 11, 30), teams[0], teams[1])]
        )
        offseason = build_pennant_offseason_service(self.npb_world)

        self.assertFalse(offseason.can_choose_releases())
        with self.assertRaises(SeasonNotFinished):
            offseason.set_releases([], expected_year=YEAR)

    def test_a_world_without_a_postseason_keeps_the_tab_until_it_is_closed(self):
        offseason = build_pennant_offseason_service(self.none_world)
        self.assertTrue(offseason.can_choose_releases())

        offseason.close_season(expected_year=YEAR)

        self.assertFalse(offseason.can_choose_releases())


class CommandTest(PostseasonWorldCase):
    def test_the_command_plays_to_the_end(self):
        out = StringIO()
        call_command("pennant_advance", world=self.npb_world, target="postseason-end", stdout=out)

        self.assertIn("ポストシーズンが終わりました", out.getvalue())
        self.assertIs(self.bracket(self.npb_world).status, PostseasonStatus.FINISHED)

    def test_a_day_after_the_regular_season_advances_the_postseason(self):
        out = StringIO()
        call_command("pennant_advance", world=self.npb_world, target="day", stdout=out)

        self.assertIn("ポストシーズン", out.getvalue())
        self.assertEqual(self.postseason_games(self.npb_world).count(), 2)

    def test_the_regular_season_ranges_point_to_the_postseason_when_it_is_left(self):
        """公式戦の後に week / month / season-end を打つと、ポストシーズンの進め方を案内する。

        方式「なし」の世界と、終わったポストシーズンは案内しない。
        """
        for target in ("week", "month", "season-end"):
            with self.subTest(target=target):
                out = StringIO()
                call_command("pennant_advance", world=self.npb_world, target=target, stdout=out)
                self.assertIn("--to postseason-end", out.getvalue())

                out = StringIO()
                call_command("pennant_advance", world=self.none_world, target=target, stdout=out)
                self.assertNotIn("ポストシーズン", out.getvalue())
        self.assertFalse(self.postseason_games(self.npb_world).exists(), "案内するだけで進めない")

        self.advance(self.npb_world, AdvanceTarget.POSTSEASON_END)
        out = StringIO()
        call_command("pennant_advance", world=self.npb_world, target="week", stdout=out)
        self.assertNotIn("--to postseason-end", out.getvalue(), "終わった後は案内しない")

    def test_a_broken_postseason_only_warns_after_the_regular_season(self):
        """案内のための読み取りが失敗しても、公式戦の結果の報告は落とさず、警告を1行だけ出す。"""
        out, err = StringIO(), StringIO()
        with mock.patch(
            "myapp.application.pennant_postseason.PennantPostseasonService.has_postseason_left",
            side_effect=InvalidPostseason("食い違い"),
        ):
            call_command("pennant_advance", world=self.npb_world, target="week", stdout=out, stderr=err)

        self.assertIn("シーズンは終了しています", out.getvalue())
        self.assertNotIn("--to postseason-end", out.getvalue())
        self.assertIn("ポストシーズンの状況を確かめられませんでした: 食い違い", err.getvalue())

    def test_a_world_without_a_postseason_says_so(self):
        out = StringIO()
        call_command("pennant_advance", world=self.none_world, target="postseason-end", stdout=out)

        self.assertIn("進めるポストシーズンの試合がありません", out.getvalue())


class GameKeyTest(SimpleTestCase):
    """試合ごとの乱数の識別。公式戦は切り出す前と同じ形のまま、ポストシーズンは別の接頭辞。"""

    def test_the_regular_season_keeps_its_key(self):
        self.assertEqual(game_key(GameStage.REGULAR, date(YEAR, 4, 1), 3, 5), "2026-04-01:3:5")

    def test_the_postseason_does_not_share_the_regular_seasons_random_numbers(self):
        self.assertEqual(game_key(GameStage.POSTSEASON, date(YEAR, 10, 1), 3, 5), "post:2026-10-01:3:5")


class CreateCommandFormatsTest(SimpleTestCase):
    def test_every_format_can_be_chosen_from_the_command(self):
        """方式を足したときに、コマンドから選べないままにならない（綴りの対応が Enum の全部を覆う）。"""
        from myapp.management.commands.pennant_create import POSTSEASON_FORMATS

        self.assertEqual(set(POSTSEASON_FORMATS.values()), set(PostseasonFormat))
