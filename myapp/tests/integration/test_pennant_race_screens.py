"""進める単位（月末まで・上限まで）と、優勝・マジック・終了の印（#104）。

世界は球団2つ（1リーグ）で、1日に1試合・年143試合なので、「上限まで」は1シーズン全部が収まる。
マジックの計算そのものは `tests/domain/test_pennant_race.py`。ここは画面とサービスの組み立てを確かめる。
"""

from dataclasses import replace
from datetime import date, timedelta
from types import SimpleNamespace
from unittest import mock

from django.db import connection
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from myapp.application.dto import LeagueStandings, StandingRow, WorldContext
from myapp.application.pennant_home import _LABELS, PennantHomeService
from myapp.application.pennant_race import ChampionTiebreak, own_race, remaining_by_team, tiebreak_for
from myapp.domain.pennant.schedule import AdvanceTarget, Fixture
from myapp.domain.pennant.season import SCREEN_ADVANCE_TARGETS, SeasonPhase
from myapp.domain.pennant.world import WorldScope
from myapp.infrastructure import orm_models
from myapp.infrastructure.queries import DjangoGameListQuery, DjangoWorldSummaryQuery
from myapp.presentation.views import (
    build_pennant_home_service,
    build_pennant_offseason_service,
    build_pennant_season_service,
    build_pennant_world_view,
    build_world_view_service,
)

from .test_pennant_advance import SEASON_GAMES, YEAR
from .test_pennant_home import HomeCase


def _row(team_id: int, wins: int, losses: int) -> StandingRow:
    return StandingRow(
        rank=1,
        team_id=team_id,
        team_name=f"球団{team_id}",
        wins=wins,
        losses=losses,
        ties=0,
        games_played=wins + losses,
        winning_percentage="",
        games_behind="",
    )


class AdvanceUnitsTest(HomeCase):
    def test_the_owner_sees_the_month_end_and_the_limit_buttons(self):
        self.start_season()
        self.client.force_login(self.owner)

        content = self.client.get(self.home_url()).content.decode()

        self.assertIn("月末まで進める", content)
        self.assertIn("上限まで進める", content)
        self.assertLess(content.index("1日進める"), content.index("月末まで進める"), "目立つのは先頭の「1日」")

    def test_the_month_end_advances_to_the_end_of_the_month(self):
        self.start_season()
        self.client.force_login(self.owner)

        self.post_advance("month_end")

        last = build_pennant_season_service(self.world_a)._context_query.last_played_on()
        self.assertEqual((last.month, (last + timedelta(days=1)).month), (3, 4), "開幕した3月の最後の試合日まで")
        self.assertEqual(self.games_in(), 4, "3月27日〜31日（月曜は休み）")

    def test_the_limit_advances_as_far_as_the_limit_allows(self):
        self.start_season()
        self.client.force_login(self.owner)

        self.post_advance("limit")

        self.assertEqual(self.games_in(), SEASON_GAMES, "143試合は上限（150）に収まるので、1シーズン全部")

    def test_the_limit_stops_on_a_day_boundary(self):
        self.start_season()
        self.client.force_login(self.owner)

        with (
            mock.patch("myapp.domain.pennant.schedule.MAX_GAMES_PER_ADVANCE", 10),
            mock.patch("myapp.presentation.views.MAX_GAMES_PER_ADVANCE", 10),
        ):
            self.post_advance("limit")

        self.assertEqual(self.games_in(), 10, "1日1試合なので10日ぶん")

    def test_a_month_end_over_the_limit_is_not_offered_and_is_refused(self):
        self.start_season()
        self.client.force_login(self.owner)

        with mock.patch("myapp.application.pennant_home.MAX_GAMES_PER_ADVANCE", 3):
            content = self.client.get(self.home_url()).content.decode()
        with mock.patch("myapp.presentation.views.MAX_GAMES_PER_ADVANCE", 3):
            response = self.post_advance("month_end")

        self.assertNotIn("月末まで進める", content)
        self.assertEqual(self.games_in(), 0, "上限を超える範囲は何も作らずに断る")
        self.assertIn("一度に進められるのは3試合までです", self.last_message(response))

    def test_every_screen_target_is_labelled(self):
        """ボタンの文言を持たない範囲を、画面の選択肢に足してしまわない。"""
        self.assertEqual(set(SCREEN_ADVANCE_TARGETS), set(_LABELS))


class OptionsTest(SimpleTestCase):
    def test_the_limit_is_offered_when_the_month_end_is_over_the_limit(self):
        """8リーグ並みの1日16試合。月末（20日 = 320試合）は隠れ、上限まで（9日 = 144試合）は出る。"""
        fixtures = [
            Fixture(date=date(2026, 4, 1) + timedelta(days=day), home_team_id=1 + n, visitor_team_id=100 + n)
            for day in range(20)
            for n in range(16)
        ]
        world = WorldContext(
            world_id=1,
            name="世界",
            phase=SeasonPhase.IN_SEASON,
            season_year=2026,
            today=date(2026, 3, 31),
            managed_team_id=1,
            managed_team_name="自軍",
            default_league_id=None,
        )

        options = {option.target: option for option in PennantHomeService._advance_options(world, fixtures)}

        self.assertNotIn(AdvanceTarget.MONTH_END.value, options)
        self.assertEqual((options["limit"].games, options["limit"].end_date), (144, date(2026, 4, 9)))


class RaceBandTest(HomeCase):
    def finish_season(self) -> int:
        """1シーズン終えて、首位の球団の id を返す（球団2つで同率なら、この世界は使えない）。"""
        self.start_season()
        build_pennant_season_service(self.world_a).advance(AdvanceTarget.SEASON_END)
        (league,) = build_world_view_service(self.world_a).get_league_standings(YEAR)
        first, second = league.rows[:2]
        self.assertNotEqual((first.wins, first.losses), (second.wins, second.losses), "同率の世界は使えない")
        return first.team_id

    def test_the_champion_sees_the_clinched_band_on_the_home_and_the_list(self):
        leader = self.finish_season()
        orm_models.PennantWorld.objects.filter(id=self.world_a).update(managed_team_id=leader)

        home = self.client.get(self.home_url()).content.decode()
        index = self.client.get(reverse("pennant_index")).content.decode()

        self.assertIn("優勝確定", home)
        self.assertIn("優勝争い", home)
        self.assertIn("優勝確定", index)

    def test_the_runner_up_sees_no_band(self):
        leader = self.finish_season()
        runner_up = orm_models.Team.objects.filter(league__world_id=self.world_a).exclude(id=leader)[0]
        orm_models.PennantWorld.objects.filter(id=self.world_a).update(managed_team_id=runner_up.id)

        home = self.client.get(self.home_url()).content.decode()
        index = self.client.get(reverse("pennant_index")).content.decode()

        self.assertNotIn("優勝争い", home)
        self.assertNotIn("優勝確定", index)

    def test_no_band_early_in_the_season_or_before_opening(self):
        before = self.client.get(self.home_url()).content.decode()
        self.start_season()
        for _ in range(3):
            build_pennant_season_service(self.world_a).advance(AdvanceTarget.DAY)
        early = self.client.get(self.home_url()).content.decode()

        self.assertNotIn("優勝争い", before)
        self.assertNotIn("優勝争い", early)

    def test_the_final_season_shows_the_end_and_the_totals(self):
        leader = self.finish_season()
        orm_models.PennantWorld.objects.filter(id=self.world_a).update(managed_team_id=leader)

        with mock.patch("myapp.application.pennant_view.is_final_season", return_value=True):
            home = self.client.get(self.home_url()).content.decode()
            index = self.client.get(reverse("pennant_index")).content.decode()

        self.assertIn("世界の終了（1シーズン）", home)
        self.assertIn("優勝</span>", home)
        self.assertIn("1回", home, "優勝回数")
        self.assertIn(f"{YEAR}年 · 終了", index)

    def test_the_yearly_standings_of_the_finale_match_the_standings_screen(self):
        """世界の終了の通算が読む年ごとの順位は、順位表と同じ規則（domain の standings）から出る。"""
        self.finish_season()
        service = build_pennant_home_service(self.world_a)

        (mine,) = service._standings_by_year([YEAR])[YEAR]
        (shown,) = build_world_view_service(self.world_a).get_league_standings(YEAR)

        def key(row):
            return (row.rank, row.team_id, row.wins, row.losses, row.ties)

        self.assertEqual([key(r) for r in mine.rows], [key(r) for r in shown.rows])
        self.assertEqual(service._standings_by_year([YEAR - 1]), {YEAR - 1: []}, "試合の無い年は空")

    def test_two_seasons_add_up_in_the_finale_and_the_second_year_reads_the_first_years_final_rank(self):
        leader = self.finish_season()
        orm_models.PennantWorld.objects.filter(id=self.world_a).update(managed_team_id=leader)
        build_pennant_offseason_service(self.world_a).close_season()
        build_pennant_season_service(self.world_a).advance(AdvanceTarget.SEASON_END)
        home_service = build_pennant_home_service(self.world_a)
        view = build_world_view_service(self.world_a)
        context = build_pennant_world_view().get_context(self.world_a)

        with mock.patch("myapp.application.pennant_view.is_final_season", return_value=True):
            context = build_pennant_world_view().get_context(self.world_a)
            home = home_service.get_home(context, include_advance=False)

        both = home_service._standings_by_year([YEAR, YEAR + 1])
        for year in (YEAR, YEAR + 1):
            (shown,) = view.get_league_standings(year)
            (mine,) = both[year]
            self.assertEqual([r.team_id for r in mine.rows], [r.team_id for r in shown.rows])
        finale = home.finale
        assert finale is not None
        own = [next(r for r in both[year][0].rows if r.team_id == leader) for year in (YEAR, YEAR + 1)]
        self.assertEqual([s.year for s in finale.seasons], [YEAR, YEAR + 1])
        self.assertEqual((finale.wins, finale.losses), (sum(r.wins for r in own), sum(r.losses for r in own)))
        self.assertEqual([s.rank for s in finale.seasons], [r.rank for r in own])
        tiebreak = tiebreak_for(YEAR + 1, games=DjangoGameListQuery(WorldScope.pennant(self.world_a)), teams=view)
        last_years = {r.team_id: r.rank for r in both[YEAR][0].rows}
        self.assertEqual(tiebreak.previous_rank(), last_years, "2年目の前年の順位は1年目の最終順位")

    def test_the_end_replaces_the_close_card_for_the_owner(self):
        leader = self.finish_season()
        orm_models.PennantWorld.objects.filter(id=self.world_a).update(managed_team_id=leader)
        self.client.force_login(self.owner)

        before = self.client.get(self.home_url()).content.decode()
        with mock.patch("myapp.application.pennant_view.is_final_season", return_value=True):
            over = self.client.get(self.home_url()).content.decode()

        self.assertIn(f"{YEAR}年のシーズンを締める", before)
        self.assertNotIn("世界の終了", before)
        self.assertIn("世界の終了", over)
        self.assertNotIn("のシーズンを締める", over, "終了の情報は1か所（締めるカードは出さない）")

    def test_the_world_list_summary_query_count_does_not_grow_with_the_number_of_worlds(self):
        self.start_season()
        with CaptureQueriesContext(connection) as before:
            DjangoWorldSummaryQuery().list_all()
        self._create_world("世界D", seed=9)
        self._create_world("世界E", seed=10)
        with CaptureQueriesContext(connection) as after:
            rows = DjangoWorldSummaryQuery().list_all()

        self.assertGreaterEqual(len(rows), 5)
        self.assertEqual(len(before), len(after))

    def test_the_finale_is_not_shown_before_the_final_season_ends(self):
        self.finish_season()

        home = self.client.get(self.home_url()).content.decode()
        index = self.client.get(reverse("pennant_index")).content.decode()

        self.assertNotIn("世界の終了", home)
        self.assertIn(f"{YEAR}年 · シーズン終了", index)


class OwnRaceTest(SimpleTestCase):
    def standings(self, *rows: StandingRow) -> list[LeagueStandings]:
        return [LeagueStandings(league_id=1, league_name="リーグ", rows=list(rows))]

    def test_the_magic_is_lit_near_the_end(self):
        remaining = {1: 6, 2: 6}

        race = own_race(self.standings(_row(1, 80, 57), _row(2, 79, 58)), remaining, 1)

        assert race is not None
        self.assertEqual((race.clinched, race.magic, race.label), (False, 6, "マジック6"))

    def test_nothing_while_the_magic_is_far_off(self):
        remaining = {1: 70, 2: 70}

        self.assertIsNone(own_race(self.standings(_row(1, 40, 33), _row(2, 40, 33)), remaining, 1))

    def test_only_the_own_league_is_compared(self):
        other = LeagueStandings(league_id=2, league_name="別リーグ", rows=[_row(3, 100, 40)])
        standings = [*self.standings(_row(1, 90, 50), _row(2, 70, 70)), other]

        race = own_race(standings, {}, 1)

        assert race is not None
        self.assertTrue(race.clinched)
        self.assertEqual("優勝確定", race.label)

    def test_a_team_not_in_the_standings_has_no_race(self):
        self.assertIsNone(own_race(self.standings(_row(1, 1, 1), _row(2, 1, 1)), {}, 9))

    def test_remaining_games_count_both_home_and_visitor(self):
        fixtures = [Fixture(date(2026, 4, 1), 1, 2), Fixture(date(2026, 4, 2), 3, 1)]

        self.assertEqual({1: 2, 2: 1, 3: 1}, remaining_by_team(fixtures))


class TiebreakRaceTest(SimpleTestCase):
    """終了時に同率首位が並んだら、規定（直接対決 → 前年の順位 → 並び順）で1球団に決める。"""

    def tied(self):
        rank_one = [
            StandingRow(1, 1, "球団1", 80, 63, 0, 143, "", ""),
            StandingRow(1, 2, "球団2", 80, 63, 0, 143, "", ""),
            StandingRow(3, 3, "球団3", 60, 83, 0, 143, "", ""),
        ]
        return [LeagueStandings(league_id=1, league_name="リーグ", rows=rank_one)]

    def tiebreak(self, results, previous=None, order=(1, 2, 3)):
        return ChampionTiebreak(
            results=lambda: results, previous_rank=lambda: previous or {}, order=lambda: list(order)
        )

    def test_only_the_rule_winner_is_the_champion(self):
        tiebreak = self.tiebreak([(2, 1), (2, 1), (1, 2)])

        first = own_race(self.tied(), {}, 1, tiebreak)
        second = own_race(self.tied(), {}, 2, tiebreak)

        self.assertIsNone(first, "直接対決で負けた側は優勝ではない")
        assert second is not None
        self.assertTrue(second.clinched)

    def test_without_the_tiebreak_a_dead_heat_has_no_champion(self):
        """途中の確定は安全側のまま（決着の材料を渡さなければ、同率は確定にしない）。"""
        self.assertIsNone(own_race(self.tied(), {}, 1))

    def test_the_tiebreak_is_not_used_while_games_remain(self):
        tiebreak = self.tiebreak([(2, 1)])

        self.assertIsNone(own_race(self.tied(), {1: 1}, 2, tiebreak))

    def test_the_data_is_read_only_when_needed(self):
        """同率が無ければ、決着の材料（試合・前年の順位）は読まない。"""

        def boom():
            raise AssertionError("読んではいけない")

        tiebreak = ChampionTiebreak(results=boom, previous_rank=boom, order=boom)
        rows = [StandingRow(1, 1, "球団1", 90, 53, 0, 143, "", ""), StandingRow(2, 2, "球団2", 70, 73, 0, 143, "", "")]

        race = own_race([LeagueStandings(1, "リーグ", rows)], {}, 1, tiebreak)

        assert race is not None
        self.assertTrue(race.clinched)


class WorldContextTest(SimpleTestCase):
    def context(self, *, phase, is_final):
        return WorldContext(
            world_id=1,
            name="世界",
            phase=phase,
            season_year=2035,
            today=None,
            managed_team_id=None,
            managed_team_name="",
            default_league_id=None,
            is_final_season=is_final,
        )

    def test_a_world_is_over_only_when_the_final_season_is_finished(self):
        for phase in SeasonPhase:
            for is_final in (True, False):
                with self.subTest(phase=phase, is_final=is_final):
                    expected = is_final and phase is SeasonPhase.FINISHED
                    self.assertEqual(self.context(phase=phase, is_final=is_final).is_over, expected)


class FinaleTest(SimpleTestCase):
    """終了した世界の通算。年ごとの順位表から、自軍の成績と優勝を足し上げる。"""

    class Teams:
        def __init__(self, by_year):
            self.by_year = by_year

        def get_league_standings(self, year):
            return self.by_year.get(year, [])

        def list_teams(self):
            return SimpleNamespace(rows=[SimpleNamespace(id=1), SimpleNamespace(id=2)])

    class Games:
        def __init__(self, years, results=()):
            self.years = years
            self.results = results

        def list_seasons(self, **kwargs):
            return sorted(self.years, reverse=True)

        def list_rows(self, **kwargs):
            return [
                SimpleNamespace(is_recorded=True, winner_team_id=winner, home_team_id=winner, away_team_id=loser)
                for winner, loser in self.results
            ]

    def service(self, teams, games):
        return PennantHomeService(
            teams=teams,
            games=games,
            game_records=None,
            fixtures=None,
            activity=None,
            team_records=None,
            leagues=None,
        )

    def league(self, *rows):
        return [LeagueStandings(league_id=1, league_name="リーグ", rows=list(rows))]

    def world(self):
        return WorldContext(
            world_id=1,
            name="世界",
            phase=SeasonPhase.FINISHED,
            season_year=2028,
            today=date(2028, 10, 1),
            managed_team_id=1,
            managed_team_name="自軍",
            default_league_id=None,
            is_final_season=True,
        )

    def test_the_totals_add_up_and_a_year_without_the_team_is_skipped(self):
        by_year = {
            2026: self.league(
                StandingRow(1, 1, "球団1", 90, 50, 3, 143, "", ""), StandingRow(2, 2, "球団2", 70, 70, 3, 143, "", "")
            ),
            # 2027 は自軍の試合が無い（順位表に載らない）
            2027: self.league(StandingRow(1, 2, "球団2", 80, 63, 0, 143, "", "")),
            # 2028 は同率首位。直接対決（自軍の2勝1敗）で自軍が優勝
            2028: self.league(
                StandingRow(1, 1, "球団1", 80, 63, 0, 143, "", ""), StandingRow(1, 2, "球団2", 80, 63, 0, 143, "", "")
            ),
        }
        teams = self.Teams(by_year)
        games = self.Games([2026, 2027, 2028], results=[(1, 2), (1, 2), (2, 1)])
        service = self.service(teams, games)
        asked = []

        def standings_by_year(years):
            asked.append(list(years))
            return {year: by_year[year] for year in years}

        with mock.patch.object(service, "_standings_by_year", standings_by_year):
            finale = service._finale(self.world(), 2028, by_year[2028])

        self.assertEqual([s.year for s in finale.seasons], [2026, 2028])
        self.assertEqual([s.is_champion for s in finale.seasons], [True, True])
        self.assertEqual((finale.wins, finale.losses, finale.ties, finale.titles), (170, 113, 3, 2))
        self.assertEqual([s.record for s in finale.seasons], ["90勝50敗3分", "80勝63敗"])
        self.assertEqual(asked, [[2026, 2027]], "最後の年はホームが読んだ順位を使い、二重に読まない")

    def test_a_world_that_is_not_over_has_no_finale(self):
        service = self.service(self.Teams({}), self.Games([]))
        world = replace(self.world(), is_final_season=False)

        self.assertIsNone(service._finale(world, 2028, []))


class PreviousRankTest(SimpleTestCase):
    """前年の順位は最終順位。前年に同率首位があれば、規定で決めた優勝を1位とする。"""

    def test_a_tied_leader_of_last_year_is_ranked_by_the_rule(self):
        last_year = [
            LeagueStandings(
                league_id=1,
                league_name="リーグ",
                rows=[
                    StandingRow(1, 1, "球団1", 80, 63, 0, 143, "", ""),
                    StandingRow(1, 2, "球団2", 80, 63, 0, 143, "", ""),
                    StandingRow(3, 3, "球団3", 60, 83, 0, 143, "", ""),
                ],
            )
        ]
        teams = SimpleNamespace(
            get_league_standings=lambda year: last_year if year == 2026 else [],
            list_teams=lambda: SimpleNamespace(
                rows=[SimpleNamespace(id=1), SimpleNamespace(id=2), SimpleNamespace(id=3)]
            ),
        )
        games = SimpleNamespace(
            list_rows=lambda year=None: [
                SimpleNamespace(is_recorded=True, winner_team_id=2, home_team_id=2, away_team_id=1),
                SimpleNamespace(is_recorded=True, winner_team_id=2, home_team_id=2, away_team_id=1),
                SimpleNamespace(is_recorded=True, winner_team_id=1, home_team_id=1, away_team_id=2),
            ]
        )

        ranks = tiebreak_for(2027, games=games, teams=teams).previous_rank()

        self.assertEqual({1: 2, 2: 1, 3: 3}, ranks, "直接対決で勝った球団2が1位。ほかの同率首位は2位")
