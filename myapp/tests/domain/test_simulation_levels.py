"""水準の集計（`levels`）の単体テスト。Django も DB も使わない。

能力50の球団どうしで数百試合を回し、水準が目標帯の内側に入ることを確かめる。
**数秒に収まる試合数にしてあるので、帯は目標より広めに取る**（標本のばらつきで落ちないように）。
詳しい調整は `simulate_sample`（2,000試合・858試合のシーズン）で行う。
"""

from datetime import date, timedelta
from unittest import TestCase

from myapp.domain.pennant.schedule import ScheduleRules
from myapp.domain.simulation.engine import simulate_game
from myapp.domain.simulation.levels import (
    LEVEL_TARGETS,
    SEASON_GAMES,
    TITLE_TARGETS,
    LevelRow,
    LevelTally,
    LevelTarget,
    SeasonTally,
)
from myapp.domain.simulation.manager import PitchingHistory, choose_active_roster
from myapp.domain.simulation.randomness import game_seed, make_random
from myapp.domain.simulation.samples import average_club

GAMES = 400
OPENING_DAY = date(2026, 4, 1)

# 標本のばらつき（400試合）でも落ちない広めの帯。目標帯は LEVEL_TARGETS が出典で、ここは検査用の余裕
WIDE_BANDS = {
    "runs": (3.3, 4.5),
    "batting_average": (0.230, 0.280),
    "era": (2.9, 4.2),
    "strikeouts_per_9": (6.2, 8.6),
    "walks_per_9": (2.0, 3.6),
    "home_runs_per_9": (0.6, 1.3),
    "errors": (0.8, 1.6),
    "sacrifice_bunts": (0.35, 0.85),
    "stolen_bases": (0.3, 0.9),
    "caught_stealing": (0.1, 0.4),
    "double_plays": (0.4, 1.0),
    "runs_allowed": (3.3, 4.5),
}


class AverageLeagueLevelsTest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        clubs = [choose_active_roster(average_club(team_id)) for team_id in (1, 2)]
        history = PitchingHistory()
        cls.tally = LevelTally()
        cls.games = []
        for index in range(GAMES):
            home, away = (clubs[0], clubs[1]) if index % 2 else (clubs[1], clubs[0])
            played = simulate_game(
                make_random(game_seed(20260802, 2026, f"level-{index}")),
                home,
                away,
                played_on=OPENING_DAY + timedelta(days=index),
                history=history,
            )
            cls.tally.add(played.game)
            cls.games.append(played.game)

    def test_levels_are_inside_the_wide_bands(self):
        rows = {row.key: row for row in self.tally.rows()}
        for key, (low, high) in WIDE_BANDS.items():
            self.assertTrue(low <= rows[key].value <= high, f"{rows[key].label}: {rows[key].value:.3f} ({low}-{high})")

    def test_the_wide_bands_contain_the_target_bands(self):
        """検査の帯が目標帯より狭いと、検査が目標を言い換えただけになる。"""
        for key, (low, high) in WIDE_BANDS.items():
            target = LEVEL_TARGETS[key][1]
            self.assertLessEqual(low, target.low, key)
            self.assertGreaterEqual(high, target.high, key)

    def test_every_target_has_a_row_and_a_label(self):
        rows = self.tally.rows()
        self.assertEqual([row.key for row in rows], list(LEVEL_TARGETS))
        self.assertTrue(all(row.label for row in rows))

    def test_ties_are_counted_as_a_per_season_number(self):
        tied = sum(1 for game in self.games if game.is_tie)
        row = next(row for row in self.tally.rows() if row.key == "ties")
        self.assertAlmostEqual(row.value, tied / GAMES * 143)

    def test_the_season_length_comes_from_the_schedule_rules(self):
        # 1シーズンの試合数の出典は日程の規則。ここで別に 143 を持たない
        self.assertEqual(SEASON_GAMES, ScheduleRules().games_per_team)
        self.assertEqual(SEASON_GAMES, 143)

    def test_the_tally_matches_a_hand_count(self):
        runs = sum(game.home_score + game.away_score for game in self.games)
        hits = sum(b.line.hits for game in self.games for b in game.batting)
        at_bats = sum(b.line.at_bats for game in self.games for b in game.batting)
        rows = {row.key: row for row in self.tally.rows()}
        self.assertAlmostEqual(rows["runs"].value, runs / (GAMES * 2))
        self.assertAlmostEqual(rows["batting_average"].value, hits / at_bats)
        self.assertEqual(self.tally.games, GAMES)

    def test_the_average_clubs_are_balanced(self):
        """能力が同じ2球団なら、勝敗はどちらにも偏らない。"""
        home_wins = sum(1 for game in self.games if game.home_score > game.away_score)
        away_wins = sum(1 for game in self.games if game.away_score > game.home_score)
        self.assertLess(abs(home_wins - away_wins), GAMES * 0.2)


class TargetTest(TestCase):
    def test_a_target_contains_its_own_bounds_and_center(self):
        target = LevelTarget(3.8, 4.0)
        self.assertTrue(target.contains(3.8) and target.contains(4.0) and target.contains(target.center))
        self.assertFalse(target.contains(3.79) or target.contains(4.01))

    def test_a_row_knows_whether_it_is_within_its_band(self):
        self.assertTrue(LevelRow("x", "x", 3.9, LevelTarget(3.8, 4.0)).within)
        self.assertFalse(LevelRow("x", "x", 4.1, LevelTarget(3.8, 4.0)).within)

    def test_an_empty_tally_does_not_divide_by_zero(self):
        rows = LevelTally().rows()
        self.assertEqual(len(rows), len(LEVEL_TARGETS))
        self.assertTrue(all(row.value == 0 for row in rows))
        self.assertEqual(len(SeasonTally().rows()), len(TITLE_TARGETS))


class SeasonTallyTest(TestCase):
    def test_title_values_come_from_the_players_season_totals(self):
        clubs = [choose_active_roster(average_club(team_id)) for team_id in (1, 2)]
        history = PitchingHistory()
        # 規定打席・規定投球回の判定を外して、合計から取れる値だけを確かめる
        season = SeasonTally(team_games=1)
        played_games = []
        for index in range(40):
            home, away = (clubs[0], clubs[1]) if index % 2 else (clubs[1], clubs[0])
            played = simulate_game(
                make_random(index), home, away, played_on=OPENING_DAY + timedelta(days=index), history=history
            )
            played_games.append(played.game)
        season.add_all(played_games)
        rows = {row.key: row.value for row in season.rows()}

        home_runs: dict[int, int] = {}
        saves: dict[int, int] = {}
        for game in played_games:
            for batting in game.batting:
                home_runs[batting.player_id] = home_runs.get(batting.player_id, 0) + batting.line.home_runs
            for outing in game.pitching:
                saves[outing.player_id] = saves.get(outing.player_id, 0) + outing.line.saves
        self.assertEqual(rows["home_run_title"], max(home_runs.values()))
        self.assertEqual(rows["save_title"], max(saves.values()))
        self.assertGreater(rows["batting_title"], 0.0)
        self.assertGreater(rows["innings_leader"], 0.0)
        self.assertGreater(rows["era_title"], 0.0)
