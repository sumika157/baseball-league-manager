"""試合と、そこからの集計の単体テスト。Django も DB も使わない。"""

from datetime import date
from unittest import TestCase

from myapp.domain import services
from myapp.domain.entities import Game, Team
from myapp.domain.exceptions import InvalidGame
from myapp.domain.value_objects import (
    BattingLine,
    InningsPitched,
    PitchingLine,
    Season,
)

HOME, AWAY = 1, 2


def _game(home_score, away_score, day=1, season=2026, home=HOME, away=AWAY) -> Game:
    return Game(
        season=Season(season),
        played_on=date(season, 4, day),
        home_team_id=home,
        away_team_id=away,
        home_score=home_score,
        away_score=away_score,
    )


class GameRuleTest(TestCase):
    def test_same_team_is_rejected(self):
        with self.assertRaises(InvalidGame):
            Game(
                season=Season(2026), played_on=date(2026, 4, 1),
                home_team_id=1, away_team_id=1,
            )

    def test_negative_score_is_rejected(self):
        with self.assertRaises(InvalidGame):
            _game(-1, 0)

    def test_result_seen_from_each_team(self):
        game = _game(5, 3)

        self.assertEqual(game.result_for(HOME), 'win')
        self.assertEqual(game.result_for(AWAY), 'loss')
        self.assertEqual(game.winner_team_id, HOME)

    def test_tie(self):
        game = _game(2, 2)

        self.assertTrue(game.is_tie)
        self.assertIsNone(game.winner_team_id)
        self.assertEqual(game.result_for(HOME), 'tie')
        self.assertEqual(game.result_for(AWAY), 'tie')

    def test_score_seen_from_each_team(self):
        game = _game(5, 3)

        self.assertEqual(game.score_for(HOME), (5, 3))
        self.assertEqual(game.score_for(AWAY), (3, 5))

    def test_uninvolved_team_is_rejected(self):
        game = _game(5, 3)
        with self.assertRaises(InvalidGame):
            game.result_for(999)

    def test_recording_the_same_player_twice_overwrites(self):
        game = _game(5, 3)
        game.record_batting(10, BattingLine(at_bats=4, singles=1))
        game.record_batting(10, BattingLine(at_bats=4, home_runs=2))

        self.assertEqual(len(game.batting), 1)
        self.assertEqual(game.batting[0].line.home_runs, 2)


class TeamRecordFromGamesTest(TestCase):
    def setUp(self):
        self.games = [_game(5, 3, 1), _game(2, 2, 2), _game(1, 4, 3)]

    def test_wins_losses_and_ties(self):
        home = services.team_record(self.games, HOME)
        away = services.team_record(self.games, AWAY)

        self.assertEqual((home.wins, home.losses, home.ties), (1, 1, 1))
        self.assertEqual((away.wins, away.losses, away.ties), (1, 1, 1))

    def test_games_of_other_teams_are_ignored(self):
        games = self.games + [_game(9, 0, 4, home=3, away=4)]
        record = services.team_record(games, HOME)

        self.assertEqual(record.games_played, 3)

    def test_no_games_means_empty_record(self):
        record = services.team_record([], HOME)
        self.assertEqual(record.games_played, 0)


class PlayerTotalsTest(TestCase):
    def test_batting_totals_are_summed(self):
        g1, g2 = _game(5, 3, 1), _game(1, 4, 2)
        g1.record_batting(10, BattingLine(at_bats=4, singles=2, home_runs=1))
        g2.record_batting(10, BattingLine(at_bats=3, singles=1, walks=1))

        total = services.player_batting_total([g1, g2], 10)

        self.assertEqual(total.at_bats, 7)
        self.assertEqual(total.hits, 4)
        self.assertEqual(total.walks, 1)

    def test_rates_are_recomputed_not_averaged(self):
        """率は試合ごとの率の平均ではなく、合算した実数から計算し直す。"""
        g1, g2 = _game(5, 3, 1), _game(1, 4, 2)
        g1.record_batting(10, BattingLine(at_bats=1, singles=1))   # 10割
        g2.record_batting(10, BattingLine(at_bats=9, singles=0))   # 0割

        total = services.player_batting_total([g1, g2], 10)

        # 率の平均なら .500 だが、正しくは 1/10 = .100
        self.assertAlmostEqual(total.batting_average, 0.1)

    def test_pitching_totals_add_innings_correctly(self):
        """5.2 + 5.2 は 10.4 ではなく 11.1（アウト数で足す）。"""
        g1, g2 = _game(5, 3, 1), _game(1, 4, 2)
        line = PitchingLine(innings=InningsPitched.from_notation('5.2'), earned_runs=1)
        g1.record_pitching(18, line)
        g2.record_pitching(18, line)

        total = services.player_pitching_total([g1, g2], 18)

        self.assertEqual(total.innings.outs, 34)
        self.assertEqual(str(total.innings), '11.1')
        self.assertEqual(total.earned_runs, 2)

    def test_other_players_are_ignored(self):
        game = _game(5, 3)
        game.record_batting(10, BattingLine(at_bats=4, singles=2))
        game.record_batting(11, BattingLine(at_bats=4, home_runs=1))

        total = services.player_batting_total([game], 10)

        self.assertEqual(total.at_bats, 4)
        self.assertEqual(total.home_runs, 0)

    def test_player_without_records_totals_zero(self):
        total = services.player_batting_total([_game(5, 3)], 99)
        self.assertEqual(total.at_bats, 0)


class StandingsFromGamesTest(TestCase):
    def setUp(self):
        self.teams = [
            Team(name='ホーム', id=HOME, league_id=1),
            Team(name='ビジター', id=AWAY, league_id=1),
            Team(name='未実施', id=3, league_id=1),
        ]

    def test_ranked_by_winning_percentage(self):
        games = [_game(5, 3, 1), _game(6, 2, 2), _game(1, 4, 3)]

        rows = services.standings(self.teams, games)

        self.assertEqual([r.team_name for r in rows], ['ホーム', 'ビジター'])
        self.assertEqual(rows[0].record.wins, 2)

    def test_teams_without_games_are_excluded(self):
        """未実施を0勝0敗として並べると、全敗と区別できなくなる。"""
        rows = services.standings(self.teams, [_game(5, 3)])

        self.assertEqual(len(rows), 2)
        self.assertNotIn('未実施', [r.team_name for r in rows])

    def test_ties_share_the_rank(self):
        games = [_game(5, 3, 1), _game(3, 5, 2)]

        rows = services.standings(self.teams, games)

        self.assertEqual([r.rank for r in rows], [1, 1])

    def test_no_games_returns_empty(self):
        self.assertEqual(services.standings(self.teams, []), [])

    def test_seasons_are_listed_newest_first(self):
        games = [_game(1, 0, season=2024), _game(1, 0, season=2026), _game(1, 0, season=2025)]

        self.assertEqual([s.year for s in services.seasons_of(games)], [2026, 2025, 2024])
