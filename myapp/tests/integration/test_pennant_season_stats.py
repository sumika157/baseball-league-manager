"""ペナントの成績を、年と在籍の期間で引く（#95）。

シーズンが重なると、通算の成績・在籍中の選手だけを引く読み方は崩れる。
引退した選手が過去の年のタイトルから消える、交流戦がタイトルに数えられない、
ホームの主力が通算で出る、といったことが起きないように固定する。
"""

from unittest import mock

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from myapp.domain.value_objects import BattingLine
from myapp.infrastructure import orm_models
from myapp.infrastructure.repositories import DjangoGameRepository, DjangoTeamRepository
from myapp.presentation.views import build_world_view_service

from ..helpers import play_game
from .world_case import YEAR, WorldCase

NEXT_YEAR = YEAR + 1


class SeasonStatsCase(WorldCase):
    """2026年と2027年の2シーズンぶんの試合がある世界。

    本塁打は、ベテラン（打順9番）が 2026 年に 7 本・2027 年に 1 本、主力（打順8番）が 2026 年に 2 本・2027 年に 4 本。
    世界の下ごしらえの試合（本塁打は打順1〜5番だけ）とは重ならない選手を選んでいる。
    """

    def setUp(self):
        super().setUp()
        # 選手は全員、最初のシーズンから在籍している
        orm_models.PlayerStint.objects.filter(team__league__world_id=self.world_id).update(from_year=YEAR)
        self.veteran, self.regular = self.pennant_home_batters[8], self.pennant_home_batters[7]
        self.service = build_world_view_service(self.world_id)
        self._play(
            YEAR,
            5,
            {self.veteran: BattingLine(at_bats=20, home_runs=7), self.regular: BattingLine(at_bats=20, home_runs=2)},
        )
        self._play(
            NEXT_YEAR,
            5,
            {self.veteran: BattingLine(at_bats=20, home_runs=1), self.regular: BattingLine(at_bats=20, home_runs=4)},
        )

    def _play(self, year, month, batting, *, away=None, day=1):
        return play_game(
            self.pennant_team,
            away or self.pennant_rival,
            year=year,
            month=month,
            day=day,
            batting=batting,
            scope=self.scope,
        )

    def _title(self, key, year=None, league_id=None):
        titles = self.service.get_league_titles(league_id or self.pennant_league.id, year)
        return next(d for d in titles.departments if d.key == key)

    def _leader_names(self, key, year=None):
        return [entry.player_name for entry in self._title(key, year).entries or []]

    def _name_of(self, player_id):
        return orm_models.Player.objects.get(id=player_id).name

    def retire(self, player_id, last_year):
        """`last_year` を最後に在籍した年にして退団させる（`to_year` はその年を含む）。"""
        orm_models.PlayerStint.objects.filter(player_id=player_id).update(to_year=last_year)


class RetiredPlayersKeepTheirTitlesTest(SeasonStatsCase):
    """再発防止: 退団した選手が、在籍していた年のタイトルから消える。"""

    def test_a_retired_player_is_still_in_last_years_titles(self):
        self.retire(self.veteran, YEAR)

        before = self._leader_names("home_runs", YEAR)
        after = self._leader_names("home_runs", NEXT_YEAR)

        self.assertEqual(before[0], self._name_of(self.veteran), "前年の本塁打王は引退しても首位のまま")
        self.assertNotIn(self._name_of(self.veteran), after, "引退した翌年のタイトルには出ない")

    def test_the_retired_player_does_not_make_the_runner_up_look_like_the_leader(self):
        self.retire(self.veteran, YEAR)

        entries = self._title("home_runs", YEAR).entries or []

        self.assertEqual([(e.rank, e.value) for e in entries[:2]], [(1, "7"), (2, "2")])

    def test_the_title_screen_lists_the_retired_player_of_that_year(self):
        self.retire(self.veteran, YEAR)
        url = reverse("pennant_league_titles_by_year", args=[self.world_id, self.pennant_league.id, YEAR])

        self.assertContains(self.client.get(url), self._name_of(self.veteran))

    def test_the_page_of_a_retired_player_still_opens(self):
        """オフの結果から引退した選手へリンクする前提。退団しても選手ページは開ける。"""
        self.retire(self.veteran, YEAR)

        response = self.client.get(
            reverse("pennant_player_detail", args=[self.world_id, self.pennant_team.id, self.veteran])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self._name_of(self.veteran))

    def test_a_player_who_joined_later_is_not_in_the_earlier_title(self):
        orm_models.PlayerStint.objects.filter(player_id=self.regular).update(from_year=NEXT_YEAR)

        self.assertNotIn(self._name_of(self.regular), self._leader_names("home_runs", YEAR))
        self.assertIn(self._name_of(self.regular), self._leader_names("home_runs", NEXT_YEAR))


class InterleagueGamesCountTest(SeasonStatsCase):
    """タイトルは交流戦も数える。規定の基準になる試合数も、交流戦を含む。"""

    def setUp(self):
        super().setUp()
        other_league = orm_models.League.objects.create(name="目印の別リーグ", world_id=self.world_id)
        self.outsider = orm_models.Team.objects.create(league=other_league, name="目印の交流戦相手")
        self._play(NEXT_YEAR, 6, {self.veteran: BattingLine(at_bats=4, home_runs=3)}, away=self.outsider, day=2)

    def test_home_runs_in_interleague_games_count_for_the_title(self):
        entries = {e.player_id: e.value for e in self._title("home_runs", NEXT_YEAR).entries or []}

        self.assertEqual(entries[self.veteran], "4", "交流戦の3本 + リーグ内の1本")
        self.assertEqual(entries[self.regular], "4")

    def test_the_regulation_uses_every_game_of_the_team(self):
        """規定打席は、その球団の全試合（交流戦を含む）の数で決まる。"""
        totals = self.service.get_team_totals(self.pennant_team.id, NEXT_YEAR)

        self.assertEqual(totals.games, 2, "リーグ内の1試合 + 交流戦の1試合")

    def test_the_other_leagues_year_choices_include_the_interleague_game(self):
        titles = self.service.get_league_titles(self.outsider.league_id)

        self.assertEqual(titles.available_years, [NEXT_YEAR])


class PeriodToggleTest(SeasonStatsCase):
    """成績一覧は、ペナントの世界では今季が既定で、通算に切り替えられる。"""

    def _stats(self, period=None):
        url = reverse("pennant_league_stats", args=[self.world_id, self.pennant_league.id])
        response = self.client.get(url + (f"?period={period}" if period else ""))
        self.assertEqual(response.status_code, 200)
        return response, {row.player.id: row.player for row in response.context["players"]}

    def test_this_season_is_the_default_and_career_adds_up_the_seasons(self):
        response, season = self._stats()
        _, career = self._stats("career")

        self.assertEqual(response.context["stats_year"], NEXT_YEAR)
        self.assertEqual(season[self.veteran].home_runs, 1)
        self.assertEqual(career[self.veteran].home_runs, 8, "7 + 1")
        self.assertEqual(season[self.regular].home_runs, 4)
        self.assertEqual(career[self.regular].home_runs, 6, "2 + 4")

    def test_the_toggle_links_switch_between_this_season_and_career(self):
        content = self._stats()[0].content.decode()

        self.assertIn("今季", content)
        self.assertIn("period=career", content)

    def test_the_team_page_follows_the_same_period(self):
        url = reverse("pennant_player_list", args=[self.world_id, self.pennant_team.id])

        season = self.client.get(url)
        career = self.client.get(url + "?period=career")

        def by_id(response):
            return {row.id: row for row in response.context["players"]}

        self.assertEqual(by_id(season)[self.veteran].home_runs, 1)
        self.assertEqual(by_id(career)[self.veteran].home_runs, 8)
        self.assertGreater(career.context["totals"].home_runs, season.context["totals"].home_runs)
        self.assertEqual(season.context["stats_year"], NEXT_YEAR)
        self.assertIsNone(career.context["stats_year"])

    def test_the_ratings_table_shows_the_batting_of_the_same_period(self):
        url = reverse("pennant_player_list", args=[self.world_id, self.pennant_team.id]) + "?view=ratings"

        season = self.client.get(url)
        career = self.client.get(url + "&period=career")

        def average(response):
            return next(r for r in response.context["ratings_table"].rows if r.id == self.veteran).batting_average

        self.assertAlmostEqual(average(season), 1 / 20, places=3, msg="2027年: 20打数1安打")
        self.assertAlmostEqual(average(career), 8 / 40, places=3, msg="通算: 40打数8安打")

    def test_a_retired_player_is_not_on_this_seasons_roster(self):
        self.retire(self.veteran, YEAR)

        _, season = self._stats()
        _, career = self._stats("career")

        self.assertNotIn(self.veteran, season)
        self.assertNotIn(self.veteran, career, "通算の一覧は在籍中の選手だけ（実データと同じ）")


class HomeReadsThisSeasonTest(SeasonStatsCase):
    def setUp(self):
        super().setUp()
        orm_models.PennantWorld.objects.filter(id=self.world_id).update(managed_team_id=self.pennant_team.id)

    def _home(self):
        response = self.client.get(reverse("pennant_world", args=[self.world_id]))
        self.assertEqual(response.status_code, 200)
        return response.context["home"]

    def test_the_key_batters_are_this_seasons(self):
        rows = {row.player_id: row for row in self._home().batters}

        self.assertEqual(rows[self.veteran].home_runs, 1)
        self.assertEqual(rows[self.regular].home_runs, 4)

    def test_the_home_does_not_assemble_team_or_game_aggregates(self):
        """ホームが通算を読む `Team` 集約や全試合を組み立てると、シーズンが増えるほど遅くなる。"""
        for repository, names in (
            (DjangoTeamRepository, ("find_all_with_roster", "find_by_league_with_roster", "find_by_id")),
            (DjangoGameRepository, ("find_all", "find_by_team", "find_between_teams")),
        ):
            for name in names:
                patch = mock.patch.object(repository, name, side_effect=AssertionError(f"{name} を呼んではいけません"))
                patch.start()
                self.addCleanup(patch.stop)

        self.assertTrue(self._home().batters)


class OnlyThatYearIsReadTest(SeasonStatsCase):
    def test_the_standings_do_not_assemble_every_game(self):
        """年の選択肢は SQL で調べ、試合は対象の年だけ読む。"""
        self.assertEqual(self.service.get_standings().available_years, [NEXT_YEAR, YEAR])

        with mock.patch.object(DjangoGameRepository, "find_all", side_effect=AssertionError("find_all")):
            board = self.service.get_standings(YEAR)

        self.assertEqual(board.year, YEAR)

    def test_query_count_does_not_grow_with_the_number_of_seasons(self):
        def count(call):
            with CaptureQueriesContext(connection) as queries:
                call()
            return len(queries)

        titles_before = count(lambda: self.service.get_league_titles(self.pennant_league.id, NEXT_YEAR))
        standings_before = count(lambda: self.service.get_standings(NEXT_YEAR))
        roster_before = count(
            lambda: build_world_view_service(self.world_id).list_batters(self.pennant_team.id, year=NEXT_YEAR)
        )
        for index in range(3):
            self._play(NEXT_YEAR + 1 + index, 5, {self.veteran: BattingLine(at_bats=3)})

        self.assertEqual(
            count(lambda: self.service.get_league_titles(self.pennant_league.id, NEXT_YEAR)), titles_before
        )
        self.assertEqual(count(lambda: self.service.get_standings(NEXT_YEAR)), standings_before)
        self.assertEqual(
            count(lambda: build_world_view_service(self.world_id).list_batters(self.pennant_team.id, year=NEXT_YEAR)),
            roster_before,
        )
