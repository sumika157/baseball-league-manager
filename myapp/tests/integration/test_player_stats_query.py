"""在籍中の選手の成績を読む参照クエリ（ランキング・タイトルの材料）。

ダッシュボードとリーグタイトルは、チームを集約として組み立てずにこのクエリで選手の成績を読む。
遅さの原因が集約の組み立てだったため（タイトル 950ms・ダッシュボード 500ms）、
**再び集約を経由する形に戻らないこと**と、集計が試合からの集計（ドメイン）と一致することを固定する。
"""

from unittest import mock

from django.db import connection
from django.test.utils import CaptureQueriesContext

from myapp.domain import services as domain_services
from myapp.domain.value_objects import BattingLine, InningsPitched, PitchingLine
from myapp.infrastructure import orm_models
from myapp.infrastructure.queries import DjangoPlayerStatsQuery
from myapp.infrastructure.repositories import DjangoGameRepository, DjangoTeamRepository

from ..helpers import give_batting, play_game
from .base import BaseCase


class PlayerStatsQueryTest(BaseCase):
    def setUp(self):
        super().setUp()
        self.query = DjangoPlayerStatsQuery()
        self.slugger = self.service.register_player(self.team.id, "大砲", 3, "内野手")
        self.ace = self.service.register_player(self.team.id, "エース", 18, "投手")

    def _by_name(self, rows):
        return {row.name: row for row in rows}

    def test_career_sums_every_season(self):
        give_batting(self.team, self.rival, self.slugger.id, BattingLine(at_bats=4, home_runs=1), year=2025)
        give_batting(self.team, self.rival, self.slugger.id, BattingLine(at_bats=5, home_runs=2), year=2026)

        row = self._by_name(self.query.list_career())["大砲"]

        self.assertEqual((row.batting.at_bats, row.batting.home_runs), (9, 3))
        self.assertEqual((row.team_id, row.team_name, row.number), (self.team.id, "テストチーム", 3))

    def test_season_counts_only_that_year(self):
        give_batting(self.team, self.rival, self.slugger.id, BattingLine(at_bats=4, home_runs=1), year=2025)
        give_batting(self.team, self.rival, self.slugger.id, BattingLine(at_bats=5, home_runs=2), year=2026)

        row = self._by_name(self.query.list_season(self.league.id, 2026))["大砲"]

        self.assertEqual((row.batting.at_bats, row.batting.home_runs), (5, 2))

    def test_season_ignores_games_across_leagues(self):
        """タイトルはリーグの中で争われる。リーグをまたぐ対戦の成績は数えない。"""
        other = orm_models.League.objects.create(name="別リーグ")
        outsider = orm_models.Team.objects.create(league=other, name="別チーム")
        give_batting(self.team, self.rival, self.slugger.id, BattingLine(at_bats=4, home_runs=1))
        give_batting(self.team, outsider, self.slugger.id, BattingLine(at_bats=6, home_runs=5), day=2)

        row = self._by_name(self.query.list_season(self.league.id, 2026))["大砲"]

        self.assertEqual(row.batting.home_runs, 1)

    def test_season_lists_only_the_league_players(self):
        other = orm_models.League.objects.create(name="別リーグ")
        outsider = orm_models.Team.objects.create(league=other, name="別チーム")
        self.service.register_player(outsider.id, "他リーグ選手", 1, "内野手")

        names = {row.name for row in self.query.list_season(self.league.id, 2026)}

        self.assertEqual(names, {"大砲", "エース"})

    def test_players_who_left_the_team_are_excluded(self):
        self.service.retire_player(self.team.id, self.slugger.id, 2026)

        names = {row.name for row in self.query.list_career()}

        self.assertEqual(names, {"エース"})

    def test_player_without_games_has_zero_stats(self):
        row = self._by_name(self.query.list_career())["エース"]

        self.assertEqual(row.batting, BattingLine())
        self.assertEqual(row.pitching, PitchingLine())

    def test_rows_follow_team_order_then_jersey_number(self):
        """同値のときの並びはこの順に決まる（ランキングの表示が変わらないように）。"""
        self.service.register_player(self.rival.id, "相手の1番", 1, "内野手")
        self.service.register_player(self.team.id, "自軍の1番", 1, "内野手")

        rows = self.query.list_career()

        self.assertEqual(
            [(row.team_name, row.number) for row in rows],
            sorted((row.team_name, row.number) for row in rows),
        )

    def test_pitching_matches_the_totals_computed_from_games(self):
        """SQL の集計は、試合（集約）から集計した値と一致する。

        投球回は 5.2 + 5.2 = 11.1（アウト数で足す）。先発登板数と救援勝利は登板順から導く。
        """
        relief = self.service.register_player(self.team.id, "救援", 41, "投手")
        play_game(
            self.team,
            self.rival,
            pitching={
                self.ace.id: PitchingLine(innings=InningsPitched.from_notation("5.2"), wins=1, strikeouts=6),
                relief.id: PitchingLine(innings=InningsPitched.from_notation("3.1"), saves=1, holds=1),
            },
            day=1,
        )
        play_game(
            self.team,
            self.rival,
            pitching={self.ace.id: PitchingLine(innings=InningsPitched.from_notation("5.2"), losses=1)},
            day=2,
        )
        games = DjangoGameRepository().find_all()

        rows = self._by_name(self.query.list_career())

        for player in (self.ace, relief):
            self.assertEqual(
                rows[player.name].pitching, domain_services.player_pitching_total(games, player.id), player.name
            )
        self.assertEqual(rows["エース"].pitching.innings, InningsPitched.from_notation("11.1"))


class RankingScreensDoNotBuildAggregatesTest(BaseCase):
    """ランキング・タイトルの画面でチームや試合の集約を組み立てないこと。"""

    def setUp(self):
        super().setUp()
        self.player = self.service.register_player(self.team.id, "大砲", 3, "内野手")
        give_batting(self.team, self.rival, self.player.id, BattingLine(at_bats=10, home_runs=2))

    def _forbid_aggregates(self):
        """集約を組み立てる読み方が呼ばれたら失敗させる。"""
        patches = [
            mock.patch.object(DjangoTeamRepository, name, side_effect=AssertionError(f"{name} を呼んではいけません"))
            for name in ("find_all_with_roster", "find_by_league_with_roster", "find_by_id")
        ] + [
            mock.patch.object(DjangoGameRepository, name, side_effect=AssertionError(f"{name} を呼んではいけません"))
            for name in ("find_all", "find_by_team", "find_between_teams", "find_by_id")
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_dashboard_reads_without_aggregates(self):
        self._forbid_aggregates()

        board = self.service.get_dashboard()

        self.assertEqual(board.leagues[0].rankings.home_run_leaders[0].player_name, "大砲")

    def test_league_titles_read_without_aggregates(self):
        self._forbid_aggregates()

        titles = self.service.get_league_titles(self.league.id)

        home_runs = next(d for d in titles.departments if d.key == "home_runs")
        self.assertEqual(home_runs.entries[0].player_name, "大砲")

    def test_query_count_does_not_grow_with_the_number_of_teams(self):
        """チームや選手が増えてもクエリ数は増えない（N+1 に戻らない）。"""

        def count(call):
            with CaptureQueriesContext(connection) as queries:
                call()
            return len(queries)

        dashboard_before = count(self.service.get_dashboard)
        titles_before = count(lambda: self.service.get_league_titles(self.league.id))

        for index in range(5):
            team = orm_models.Team.objects.create(league=self.league, name=f"増えたチーム{index}")
            added = self.service.register_player(team.id, f"選手{index}", 1, "投手")
            play_game(team, self.rival, pitching={added.id: PitchingLine(innings=InningsPitched.from_notation("9.0"))})

        self.assertEqual(count(self.service.get_dashboard), dashboard_before)
        self.assertEqual(count(lambda: self.service.get_league_titles(self.league.id)), titles_before)
