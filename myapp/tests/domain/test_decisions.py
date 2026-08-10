"""勝敗・セーブ・ホールドの単体テスト（日本プロ野球の規則）。

DB も Django も使わない。イニングスコアと継投から、どの投手に何が付くかを確かめる。
"""

from datetime import date
from unittest import TestCase

from myapp.domain import services
from myapp.domain.entities import Game
from myapp.domain.value_objects import (
    InningsPitched,
    LineScore,
    PitchingLine,
    Season,
)

HOME, AWAY = 1, 2

# 選手id → チームid。試合の明細は両チームの投手が混ざるため外から渡す
HOME_PITCHERS = (10, 11, 12, 13)
AWAY_PITCHERS = (20, 21, 22, 23)
TEAM_OF = {pid: HOME for pid in HOME_PITCHERS} | {pid: AWAY for pid in AWAY_PITCHERS}


def _line(notation, *, earned_runs=0):
    return PitchingLine(innings=InningsPitched.from_notation(notation), earned_runs=earned_runs)


def _game(away_innings, home_innings, *, home_staff=(), away_staff=()) -> Game:
    """イニングスコアと継投を与えて試合を組み立てる。

    staff は (選手id, 投球回, 登板した回) の並び。登板順は並びの順。
    """
    score = LineScore(away=tuple(away_innings), home=tuple(home_innings))
    game = Game(
        season=Season(2026),
        played_on=date(2026, 4, 1),
        home_team_id=HOME,
        away_team_id=AWAY,
        home_score=score.home_total,
        away_score=score.away_total,
        line_score=score,
    )
    for staff in (home_staff, away_staff):
        for order, (player_id, notation, entered) in enumerate(staff, start=1):
            game.record_pitching(
                player_id,
                _line(notation),
                appearance_order=order,
                entered_inning=entered,
            )
    return game


class WinningPitcherTest(TestCase):
    def test_starter_who_holds_the_lead_gets_the_win(self):
        game = _game(
            [0] * 9,
            [2, 0, 0, 0, 0, 0, 0, 0, 0],
            home_staff=[(10, "9.0", 1)],
            away_staff=[(20, "9.0", 1)],
        )

        decisions = services.pitching_decisions(game, TEAM_OF)

        self.assertEqual(decisions.winner_id, 10)
        self.assertEqual(decisions.loser_id, 20)

    def test_loss_goes_to_the_pitcher_who_allowed_the_decisive_run(self):
        """決勝点を許した投手が敗戦投手。先発とは限らない。"""
        game = _game(
            [1] + [0] * 8,
            [0, 0, 0, 0, 0, 0, 2, 0, 0],
            home_staff=[(10, "9.0", 1)],
            # ビジターは7回から2番手。その回に決勝点が入る
            away_staff=[(20, "6.0", 1), (21, "3.0", 7)],
        )

        decisions = services.pitching_decisions(game, TEAM_OF)

        self.assertEqual(decisions.loser_id, 21)

    def test_starter_under_five_innings_yields_the_win_to_a_reliever(self):
        """先発は5回以上投げないと勝利投手になれない。最も内容の良い救援に回る。"""
        game = _game(
            [0] * 9,
            [3, 0, 0, 0, 0, 0, 0, 0, 0],
            home_staff=[
                (10, "4.0", 1),  # 先発は4回で降板
                (11, "3.0", 5),  # 3回を無失点。最も内容が良い
                (12, "2.0", 8),
            ],
            away_staff=[(20, "9.0", 1)],
        )

        decisions = services.pitching_decisions(game, TEAM_OF)

        self.assertEqual(decisions.winner_id, 11)

    def test_a_reliever_of_record_wins_regardless_of_length(self):
        """救援は投球回の下限が無い。逆転した時点の投手が勝利投手になる。"""
        game = _game(
            [2] + [0] * 8,
            [0, 0, 0, 0, 0, 0, 3, 0, 0],
            # ホームは7回から2番手。その裏に逆転する
            home_staff=[(10, "6.0", 1), (11, "1.0", 7), (12, "2.0", 8)],
            away_staff=[(20, "9.0", 1)],
        )

        decisions = services.pitching_decisions(game, TEAM_OF)

        self.assertEqual(decisions.winner_id, 11)

    def test_a_tie_has_no_winner_or_loser(self):
        game = _game(
            [1] + [0] * 8,
            [1] + [0] * 8,
            home_staff=[(10, "9.0", 1)],
            away_staff=[(20, "9.0", 1)],
        )

        decisions = services.pitching_decisions(game, TEAM_OF)

        self.assertIsNone(decisions.winner_id)
        self.assertIsNone(decisions.loser_id)
        self.assertIsNone(decisions.save_id)

    def test_no_line_score_means_no_decisions(self):
        """イニングスコアが無ければ判定できない。何も付けない。"""
        game = Game(
            season=Season(2026),
            played_on=date(2026, 4, 1),
            home_team_id=HOME,
            away_team_id=AWAY,
            home_score=5,
            away_score=3,
        )
        game.record_pitching(10, _line("9.0"), appearance_order=1, entered_inning=1)

        decisions = services.pitching_decisions(game, TEAM_OF)

        self.assertIsNone(decisions.winner_id)


class SaveTest(TestCase):
    def test_closer_with_a_small_lead_gets_the_save(self):
        game = _game(
            [0] * 9,
            [2, 0, 0, 0, 0, 0, 0, 0, 0],
            home_staff=[(10, "8.0", 1), (11, "1.0", 9)],
            away_staff=[(20, "9.0", 1)],
        )

        decisions = services.pitching_decisions(game, TEAM_OF)

        self.assertEqual(decisions.winner_id, 10)
        self.assertEqual(decisions.save_id, 11)

    def test_no_save_when_the_lead_is_too_large(self):
        """4点差以上のリードで登板し、1回だけならセーブは付かない。"""
        game = _game(
            [0] * 9,
            [5, 0, 0, 0, 0, 0, 0, 0, 0],
            home_staff=[(10, "8.0", 1), (11, "1.0", 9)],
            away_staff=[(20, "9.0", 1)],
        )

        decisions = services.pitching_decisions(game, TEAM_OF)

        self.assertIsNone(decisions.save_id)

    def test_long_relief_earns_a_save_even_with_a_big_lead(self):
        """3回以上を投げて締めれば、点差に関わらずセーブが付く。"""
        game = _game(
            [0] * 9,
            [8, 0, 0, 0, 0, 0, 0, 0, 0],
            home_staff=[(10, "6.0", 1), (11, "3.0", 7)],
            away_staff=[(20, "9.0", 1)],
        )

        decisions = services.pitching_decisions(game, TEAM_OF)

        self.assertEqual(decisions.save_id, 11)

    def test_a_complete_game_has_no_save(self):
        """完投は勝利のみ。セーブは救援に付く記録。"""
        game = _game(
            [0] * 9,
            [1, 0, 0, 0, 0, 0, 0, 0, 0],
            home_staff=[(10, "9.0", 1)],
            away_staff=[(20, "9.0", 1)],
        )

        decisions = services.pitching_decisions(game, TEAM_OF)

        self.assertIsNone(decisions.save_id)

    def test_the_winning_pitcher_does_not_also_get_a_save(self):
        """逆転してそのまま締めた投手は勝利投手。セーブは付かない。"""
        game = _game(
            [1] + [0] * 8,
            [0, 0, 0, 0, 0, 0, 0, 0, 2],
            home_staff=[(10, "8.0", 1), (11, "1.0", 9)],
            away_staff=[(20, "9.0", 1)],
        )

        decisions = services.pitching_decisions(game, TEAM_OF)

        self.assertEqual(decisions.winner_id, 11)
        self.assertIsNone(decisions.save_id)


class HoldTest(TestCase):
    def test_middle_reliever_preserving_a_small_lead_gets_a_hold(self):
        game = _game(
            [0] * 9,
            [2, 0, 0, 0, 0, 0, 0, 0, 0],
            home_staff=[(10, "6.0", 1), (11, "2.0", 7), (12, "1.0", 9)],
            away_staff=[(20, "9.0", 1)],
        )

        decisions = services.pitching_decisions(game, TEAM_OF)

        self.assertEqual(decisions.hold_ids, frozenset({11}))
        self.assertEqual(decisions.save_id, 12)

    def test_no_hold_for_the_starter_or_the_finisher(self):
        game = _game(
            [0] * 9,
            [2, 0, 0, 0, 0, 0, 0, 0, 0],
            home_staff=[(10, "6.0", 1), (11, "2.0", 7), (12, "1.0", 9)],
            away_staff=[(20, "9.0", 1)],
        )

        decisions = services.pitching_decisions(game, TEAM_OF)

        self.assertNotIn(10, decisions.hold_ids)
        self.assertNotIn(12, decisions.hold_ids)

    def test_no_hold_when_entering_without_a_lead(self):
        """リードしていない場面での登板はセーブ機会ではないのでホールドも付かない。"""
        game = _game(
            [3] + [0] * 8,
            [0, 0, 0, 0, 0, 0, 0, 0, 4],
            home_staff=[(10, "6.0", 1), (11, "2.0", 7), (12, "1.0", 9)],
            away_staff=[(20, "9.0", 1)],
        )

        decisions = services.pitching_decisions(game, TEAM_OF)

        self.assertEqual(decisions.hold_ids, frozenset())

    def test_no_hold_when_the_lead_is_lost(self):
        """引き継いだ時点でリードが消えていればホールドは付かない。"""
        game = _game(
            [0, 0, 0, 0, 0, 0, 3, 0, 0],
            [2, 0, 0, 0, 0, 0, 0, 0, 3],
            home_staff=[(10, "6.0", 1), (11, "2.0", 7), (12, "1.0", 9)],
            away_staff=[(20, "9.0", 1)],
        )

        decisions = services.pitching_decisions(game, TEAM_OF)

        self.assertNotIn(11, decisions.hold_ids)

    def test_holds_are_recorded_even_in_a_tie(self):
        """引分でも、リードを保って引き継いだ救援にはホールドが付く。"""
        game = _game(
            [0, 0, 0, 0, 0, 0, 0, 0, 2],
            [2, 0, 0, 0, 0, 0, 0, 0, 0],
            home_staff=[(10, "6.0", 1), (11, "2.0", 7), (12, "1.0", 9)],
            away_staff=[(20, "9.0", 1)],
        )

        decisions = services.pitching_decisions(game, TEAM_OF)

        self.assertIn(11, decisions.hold_ids)
        self.assertIsNone(decisions.winner_id)


class HoldPointTest(TestCase):
    def test_hold_points_are_holds_plus_relief_wins(self):
        line = PitchingLine(
            innings=InningsPitched.from_notation("60.0"),
            wins=5,
            relief_wins=5,
            holds=25,
        )

        self.assertEqual(line.hold_points, 30)

    def test_a_starters_win_does_not_count_toward_hold_points(self):
        line = PitchingLine(
            innings=InningsPitched.from_notation("180.0"),
            wins=15,
            relief_wins=0,
            holds=0,
            starts=28,
        )

        self.assertEqual(line.hold_points, 0)


class LineScoreTest(TestCase):
    def test_totals_come_from_the_innings(self):
        score = LineScore(away=(0, 1, 0, 2), home=(1, 0, 0, 0))

        self.assertEqual(score.away_total, 3)
        self.assertEqual(score.home_total, 1)
        self.assertEqual(score.innings, 4)

    def test_score_after_a_half_inning(self):
        score = LineScore(away=(1, 1, 0), home=(0, 2, 0))

        # 2回の表を終えた時点。ホームはまだ2回裏を攻めていない
        self.assertEqual(score.score_after(2, bottom=False), (2, 0))
        # 2回の裏を終えた時点
        self.assertEqual(score.score_after(2, bottom=True), (2, 2))

    def test_extra_innings_are_allowed(self):
        score = LineScore(away=(0,) * 12, home=(0,) * 11 + (1,))

        self.assertEqual(score.innings, 12)
        self.assertEqual(score.home_total, 1)

    def test_matches_compares_with_the_final_score(self):
        score = LineScore(away=(1, 0), home=(0, 2))

        self.assertTrue(score.matches(1, 2))
        self.assertFalse(score.matches(1, 3))
