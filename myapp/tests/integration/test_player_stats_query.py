"""在籍中の選手の成績を読む参照クエリ（ランキング・タイトルの材料）。

ダッシュボードとリーグタイトルは、チームを集約として組み立てずにこのクエリで選手の成績を読む。
遅さの原因が集約の組み立てだったため（タイトル 950ms・ダッシュボード 500ms）、
**再び集約を経由する形に戻らないこと**と、集計が試合からの集計（ドメイン）と一致することを固定する。
"""

from unittest import mock

from django.db import connection
from django.test.utils import CaptureQueriesContext

from myapp.domain import services as domain_services
from myapp.domain.pennant.world import WorldScope
from myapp.domain.value_objects import BattingLine, InningsPitched, PitchingLine, Profile
from myapp.infrastructure import orm_models
from myapp.infrastructure.queries import DjangoPlayerStatsQuery
from myapp.infrastructure.repositories import DjangoGameRepository, DjangoTeamRepository

from ..helpers import give_batting, give_pitching, play_game
from .base import BaseCase


class PlayerStatsQueryTest(BaseCase):
    def setUp(self):
        super().setUp()
        self.query = DjangoPlayerStatsQuery(WorldScope.real())
        self.slugger = self.service.register_player(self.team.id, "大砲", 3, "内野手")
        self.ace = self.service.register_player(self.team.id, "エース", 18, "投手")

    def _by_name(self, rows):
        return {row.name: row for row in rows}

    def test_career_sums_every_season(self):
        give_batting(self.team, self.rival, self.slugger.id, BattingLine(at_bats=4, home_runs=1), year=2025)
        give_batting(self.team, self.rival, self.slugger.id, BattingLine(at_bats=5, home_runs=2), year=2026)

        row = self._by_name(self.query.list_career())["大砲"]

        self.assertEqual((row.batting.at_bats, row.batting.home_runs), (9, 3))
        self.assertEqual((row.team_id, row.team_name, row.number), (self.team.id, "テストチーム", "3"))

    def test_season_counts_only_that_year(self):
        give_batting(self.team, self.rival, self.slugger.id, BattingLine(at_bats=4, home_runs=1), year=2025)
        give_batting(self.team, self.rival, self.slugger.id, BattingLine(at_bats=5, home_runs=2), year=2026)

        row = self._by_name(self.query.list_season(self.league.id, 2026))["大砲"]

        self.assertEqual((row.batting.at_bats, row.batting.home_runs), (5, 2))

    def test_season_counts_games_across_leagues(self):
        """タイトルは交流戦も数える（NPB と同じ）。リーグをまたぐ対戦の成績も、その年の成績に入る。"""
        other = orm_models.League.objects.create(name="別リーグ")
        outsider = orm_models.Team.objects.create(league=other, name="別チーム")
        give_batting(self.team, self.rival, self.slugger.id, BattingLine(at_bats=4, home_runs=1))
        give_batting(self.team, outsider, self.slugger.id, BattingLine(at_bats=6, home_runs=5), day=2)

        row = self._by_name(self.query.list_season(self.league.id, 2026))["大砲"]

        self.assertEqual((row.batting.at_bats, row.batting.home_runs), (10, 6))

    def test_a_retired_player_stays_in_the_years_he_was_on_the_roster(self):
        """退団した選手は、在籍していた年の成績に残る（`to_year` が空の在籍だけを引くと消える）。"""
        give_batting(self.team, self.rival, self.slugger.id, BattingLine(at_bats=4, home_runs=3), year=2025)
        orm_models.PlayerStint.objects.filter(player_id=self.slugger.id).update(from_year=2024)
        self.service.retire_player(self.team.id, self.slugger.id, 2025)

        in_last_year = self._by_name(self.query.list_season(self.league.id, 2025))
        in_the_year_before = self._by_name(self.query.list_season(self.league.id, 2024))

        self.assertEqual(in_last_year["大砲"].batting.home_runs, 3)
        self.assertIn("大砲", in_the_year_before)
        self.assertNotIn("大砲", {row.name for row in self.query.list_season(self.league.id, 2026)})
        self.assertNotIn("大砲", {row.name for row in self.query.list_career()})

    def test_a_player_is_not_listed_before_he_joined(self):
        orm_models.PlayerStint.objects.filter(player_id=self.slugger.id).update(from_year=2026)

        self.assertNotIn("大砲", {row.name for row in self.query.list_season(self.league.id, 2025)})
        self.assertIn("大砲", {row.name for row in self.query.list_season(self.league.id, 2026)})

    def test_a_player_who_moved_within_the_year_is_listed_once_on_his_last_team(self):
        give_batting(self.team, self.rival, self.slugger.id, BattingLine(at_bats=4, home_runs=2), year=2026)
        self.service.transfer_player(
            self.slugger.id, from_team_id=self.team.id, to_team_id=self.rival.id, number=5, year=2026
        )

        rows = [row for row in self.query.list_season(self.league.id, 2026) if row.name == "大砲"]

        self.assertEqual([(row.team_id, row.batting.home_runs) for row in rows], [(self.rival.id, 2)])

    def test_the_roster_reads_a_team_with_profile_and_captain(self):
        self.service.appoint_captain(self.team.id, self.slugger.id)

        rows = self.query.list_roster(year=None, team_id=self.team.id, with_profile=True)
        without = self.query.list_roster(year=None, team_id=self.team.id)

        self.assertEqual({row.name for row in rows}, {"大砲", "エース"})
        self.assertEqual({row.name for row in rows if row.is_captain}, {"大砲"})
        self.assertTrue(all(row.league_id == self.league.id for row in rows))
        self.assertEqual({row.profile for row in without}, {Profile()})

    def test_the_roster_matches_what_the_team_aggregate_says(self):
        """球団の一覧は集約を組み立てずに読むが、値は集約（出典）と食い違わない。"""
        give_batting(self.team, self.rival, self.slugger.id, BattingLine(at_bats=4, singles=1, home_runs=1), year=2025)
        give_batting(self.team, self.rival, self.slugger.id, BattingLine(at_bats=5, doubles=2), year=2026)
        give_pitching(
            self.team, self.rival, self.ace.id, PitchingLine(innings=InningsPitched.from_notation("6.1"), wins=1)
        )
        self.service.appoint_captain(self.team.id, self.ace.id)
        team = DjangoTeamRepository(WorldScope.real()).find_by_id(self.team.id)

        rows = {
            row.player_id: row for row in self.query.list_roster(year=None, team_id=self.team.id, with_profile=True)
        }

        self.assertEqual(set(rows), {player.id for player in team.active_players})
        for player in team.active_players:
            row = rows[player.id]
            self.assertEqual(
                (row.batting, row.pitching, row.profile, row.number, row.position, row.is_captain),
                (
                    player.batting,
                    player.pitching,
                    player.profile,
                    player.number.value,
                    player.position,
                    team.current_captain is player,
                ),
                player.name,
            )

    def test_the_roster_of_a_year_reads_that_years_stats(self):
        give_batting(self.team, self.rival, self.slugger.id, BattingLine(at_bats=4, home_runs=1), year=2025)
        give_batting(self.team, self.rival, self.slugger.id, BattingLine(at_bats=5, home_runs=2), year=2026)
        orm_models.PlayerStint.objects.update(from_year=2025)

        season = self._by_name(self.query.list_roster(year=2025, team_id=self.team.id))
        career = self._by_name(self.query.list_roster(year=None, team_id=self.team.id))

        self.assertEqual(season["大砲"].batting.home_runs, 1)
        self.assertEqual(career["大砲"].batting.home_runs, 3)

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

        for number in ["10", "2", "0", "00"]:
            self.service.register_player(self.team.id, f"自軍の{number}番", number, "内野手")

        rows = self.query.list_career()

        # 背番号は文字列の列。SQL の文字列順（「10」が「2」より前）にならず、00 → 0 → 1 → 2 → 3 → 10 → 18 の順
        self.assertEqual(
            [(row.team_name, row.number) for row in rows],
            [("テストチーム", n) for n in ["00", "0", "1", "2", "3", "10", "18"]] + [("相手チーム", "1")],
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
        games = DjangoGameRepository(WorldScope.real()).find_all()

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

    def test_team_and_league_lists_read_without_aggregates(self):
        """球団の選手一覧・チームの合計・リーグの成績一覧も、ロスターの集約（通算を全シーズンぶん読む）を経由しない。"""
        self._forbid_aggregates()

        self.assertEqual(self.service.get_team_name(self.team.id), "テストチーム")
        batters = self.service.list_batters(self.team.id)
        pitchers = self.service.list_pitchers(self.team.id, year=2026)
        totals = self.service.get_team_totals(self.team.id)
        stats = self.service.get_league_stats(self.league.id)

        self.assertEqual([row.name for row in batters.rows], ["大砲"])
        self.assertEqual(pitchers.rows, [])
        self.assertEqual(totals.home_runs, 2)
        self.assertEqual([row.player.name for row in stats.listing.rows], ["大砲"])

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


class PeriodCoveringMatchesTheDomainTest(BaseCase):
    """SQL の期間の条件（`period_covering`）は、domain の `Stint.covers()` と境界値でも一致する。"""

    def test_every_boundary_agrees_with_stint_covers(self):
        from myapp.domain.entities import Stint
        from myapp.domain.value_objects import JerseyNumber
        from myapp.infrastructure.scoping import period_covering

        cases = [(2025, None), (2025, 2025), (2025, 2027), (2026, None), (2026, 2026), (2027, 2028)]
        for index, (from_year, to_year) in enumerate(cases):
            orm_models.PlayerStint.objects.create(
                player=orm_models.Player.objects.create(name=f"境界{index}"),
                team=self.team,
                number=str(index + 1),
                from_year=from_year,
                to_year=to_year,
            )
        for year in (2024, 2025, 2026, 2027, 2028, 2029):
            with self.subTest(year=year):
                in_sql = set(
                    orm_models.PlayerStint.objects.filter(period_covering(year)).values_list("number", flat=True)
                )
                in_domain = {
                    str(index + 1)
                    for index, (from_year, to_year) in enumerate(cases)
                    if Stint(
                        team_id=self.team.id,
                        team_name="",
                        number=JerseyNumber("1"),
                        from_year=from_year,
                        to_year=to_year,
                    ).covers(year)
                }
                self.assertEqual(in_sql, in_domain)
        current = set(orm_models.PlayerStint.objects.filter(period_covering(None)).values_list("number", flat=True))
        self.assertEqual(current, {"1", "4"})
