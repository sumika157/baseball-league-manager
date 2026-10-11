"""球団の勢い（直近10試合と連続）。保存せず、結果の並びから導く。"""

import unittest

from myapp.domain.pennant.form import RECENT_GAMES, Outcome, outcome_for, recent_form

W, L, T = Outcome.WIN, Outcome.LOSS, Outcome.TIE


class OutcomeTest(unittest.TestCase):
    def test_the_outcome_is_seen_from_the_team(self):
        # チーム1がホームで 3-1 勝ち。ビジターのチーム2から見れば負け
        self.assertIs(outcome_for(1, 1, 2, 3, 1), W)
        self.assertIs(outcome_for(2, 1, 2, 3, 1), L)

    def test_a_visiting_team_wins_with_more_runs(self):
        self.assertIs(outcome_for(2, 1, 2, 1, 4), W)

    def test_a_tie_is_a_tie_for_both(self):
        self.assertIs(outcome_for(1, 1, 2, 2, 2), T)
        self.assertIs(outcome_for(2, 1, 2, 2, 2), T)

    def test_the_symbols_are_what_the_screen_shows(self):
        self.assertEqual([o.value for o in (W, L, T)], ["○", "●", "△"])


class RecentFormTest(unittest.TestCase):
    def test_no_games(self):
        form = recent_form([])

        self.assertEqual((form.wins, form.losses, form.ties), (0, 0, 0))
        self.assertIsNone(form.streak)
        self.assertEqual((form.streak_length, form.streak_label), (0, ""))

    def test_the_record_counts_the_last_ten_only(self):
        newest_first = [W] * 6 + [L] * 4 + [L] * 20

        form = recent_form(newest_first)

        self.assertEqual((form.wins, form.losses, form.ties), (6, 4, 0))
        self.assertEqual(form.record, "6勝4敗")
        self.assertEqual(RECENT_GAMES, 10)

    def test_the_streak_runs_from_the_newest_game(self):
        self.assertEqual(recent_form([W, W, W, L, W]).streak_label, "3連勝")
        self.assertEqual(recent_form([L, L, W]).streak_label, "2連敗")
        self.assertEqual(recent_form([T, T, T, W]).streak_label, "3連分")

    def test_a_single_game_is_not_called_a_streak(self):
        self.assertEqual(recent_form([W, L]).streak_label, "1勝")
        self.assertEqual(recent_form([L, W]).streak_label, "1敗")
        self.assertEqual(recent_form([T, W]).streak_label, "1分")

    def test_the_streak_may_be_longer_than_the_window(self):
        """連続は直近10試合の外までさかのぼって数える。"""
        form = recent_form([W] * 14)

        self.assertEqual(form.streak_length, 14)
        self.assertEqual((form.wins, form.losses), (10, 0))

    def test_a_tie_is_shown_in_the_record_only_when_there_is_one(self):
        self.assertEqual(recent_form([W, L, T]).record, "1勝1敗1分")
        self.assertEqual(recent_form([W, L]).record, "1勝1敗")

    def test_the_window_can_be_changed(self):
        form = recent_form([W, W, L, L, L], window=2)

        self.assertEqual((form.wins, form.losses), (2, 0))
