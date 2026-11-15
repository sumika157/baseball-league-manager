"""分析（対戦成績・FIP）のドメインサービスの単体テスト。

DB も Django も使わない。集計の規則そのものを確かめる。
"""

from unittest import TestCase

from myapp.domain import services
from myapp.domain.value_objects import InningsPitched, PitchingLine

from .analysis_builders import DRAGONS, SWALLOWS, TIGERS, _game, _pitcher, _team


class HeadToHeadTest(TestCase):
    def test_record_between_two_teams(self):
        games = [
            _game(TIGERS, DRAGONS, 5, 3, day=1),
            _game(TIGERS, DRAGONS, 1, 2, day=2),
            _game(DRAGONS, TIGERS, 4, 4, day=3),
        ]

        record = services.head_to_head(games, TIGERS, DRAGONS)

        self.assertEqual((record.wins, record.losses, record.ties), (1, 1, 1))

    def test_games_against_others_are_excluded(self):
        """相手が違う試合は数えない。相性を見る表なので混ぜられない。"""
        games = [
            _game(TIGERS, DRAGONS, 5, 3),
            _game(TIGERS, SWALLOWS, 9, 0, day=2),
        ]

        record = services.head_to_head(games, TIGERS, DRAGONS)

        self.assertEqual((record.wins, record.losses), (1, 0))


class MatchupTableTest(TestCase):
    def setUp(self):
        self.teams = [
            _team(TIGERS, "タイガース"),
            _team(DRAGONS, "ドラゴンズ"),
            _team(SWALLOWS, "スワローズ"),
        ]
        # タイガースはドラゴンズに2勝、スワローズに1敗
        self.games = [
            _game(TIGERS, DRAGONS, 5, 3, day=1),
            _game(TIGERS, DRAGONS, 2, 1, day=2),
            _game(SWALLOWS, TIGERS, 6, 0, day=3),
        ]

    def test_rows_follow_the_standings_order(self):
        rows = services.matchups(self.teams, self.games)
        order = services.standings(self.teams, self.games)

        self.assertEqual([row.team_id for row in rows], [row.team_id for row in order])

    def test_record_against_each_opponent(self):
        rows = services.matchups(self.teams, self.games)
        tigers = next(row for row in rows if row.team_id == TIGERS)

        against_dragons = tigers.record_against(DRAGONS)
        against_swallows = tigers.record_against(SWALLOWS)

        self.assertEqual((against_dragons.wins, against_dragons.losses), (2, 0))
        self.assertEqual((against_swallows.wins, against_swallows.losses), (0, 1))

    def test_no_record_against_itself(self):
        """自分自身との対戦は存在しない。表では対角線が空になる。"""
        rows = services.matchups(self.teams, self.games)
        tigers = next(row for row in rows if row.team_id == TIGERS)

        self.assertIsNone(tigers.record_against(TIGERS))

    def test_total_matches_the_standings_record(self):
        rows = services.matchups(self.teams, self.games)
        tigers = next(row for row in rows if row.team_id == TIGERS)

        self.assertEqual((tigers.total.wins, tigers.total.losses), (2, 1))

    def test_team_without_games_is_not_listed(self):
        """1試合も無いチームは載せない。順位表と同じ規則。"""
        teams = self.teams + [_team(4, "未参加")]

        rows = services.matchups(teams, self.games)

        self.assertNotIn(4, [row.team_id for row in rows])

    def test_no_games_gives_an_empty_table(self):
        self.assertEqual(services.matchups(self.teams, []), [])


class FipConstantTest(TestCase):
    def test_constant_aligns_fip_with_the_league_era(self):
        """定数を足した FIP は、リーグ全体では防御率と同じ値になる。"""
        league = PitchingLine(
            innings=InningsPitched.from_notation("900.0"),
            earned_runs=350,
            hits_allowed=800,
            home_runs_allowed=90,
            walks_allowed=300,
            hit_by_pitch_allowed=30,
            strikeouts=700,
        )

        constant = services.fip_constant(league)

        self.assertAlmostEqual(league.fip(constant), league.earned_run_average)

    def test_no_innings_gives_zero(self):
        """投球回が無ければ比べる相手がいない。定数は 0 とする。"""
        self.assertEqual(services.fip_constant(PitchingLine()), 0.0)

    def test_constant_comes_from_the_league_totals(self):
        pitchers = [
            _pitcher(1, "9.0", hits_allowed=5, home_runs_allowed=1, strikeouts=10, earned_runs=2),
            _pitcher(2, "9.0", hits_allowed=9, home_runs_allowed=2, walks_allowed=4, strikeouts=3, earned_runs=5),
        ]

        totals = services.team_pitching(pitchers)
        constant = services.fip_constant(totals)

        self.assertAlmostEqual(constant, totals.earned_run_average - totals.fip_base)


class PitcherSortByFipTest(TestCase):
    def test_sorted_by_fip_ascending_by_default(self):
        """FIP は低いほど良い。素点で並べても定数を足した順序と変わらない。"""
        good = _pitcher(1, "9.0", strikeouts=12, hits_allowed=3)
        bad = _pitcher(2, "9.0", hits_allowed=4, home_runs_allowed=3, walks_allowed=5)

        ordered, key, descending = services.sort_pitchers([bad, good], "fip")

        self.assertEqual(key, "fip")
        self.assertFalse(descending)
        self.assertEqual([p.id for p in ordered], [1, 2])

    def test_unused_pitchers_go_last(self):
        used = _pitcher(1, "9.0", hits_allowed=4, home_runs_allowed=2, walks_allowed=5)
        unused = _pitcher(2, "0.0")

        ordered, _, _ = services.sort_pitchers([unused, used], "fip")

        self.assertEqual([p.id for p in ordered], [1, 2])
