"""失点と自責点の責任（走者ごとの投手・失策による失点）の単体テスト。Django も DB も使わない。"""

from unittest import TestCase

from myapp.domain import services
from myapp.domain.entities import FieldingError, RunnerSubstitution
from myapp.domain.value_objects import Base, ErrorKind, FieldingPosition

from .pa_builders import RELIEVER, STARTER, P, R, _pa, _to


class RunResponsibilityTest(TestCase):
    def test_run_is_charged_to_the_pitcher_who_allowed_the_runner(self):
        entries = [
            _pa(1, 1, P.SINGLE, pitcher=STARTER),
            _pa(
                2,
                2,
                P.HOME_RUN,
                pitcher=RELIEVER,
                advances=[_to(2, Base.BATTER, Base.HOME), _to(1, Base.FIRST, Base.HOME)],
            ),
        ]

        self.assertEqual(services.runs_allowed_for(entries, STARTER), 1)
        self.assertEqual(services.runs_allowed_for(entries, RELIEVER), 1)

    def test_pinch_runner_inherits_the_responsible_pitcher(self):
        entries = [
            _pa(1, 1, P.SINGLE, pitcher=STARTER),
            _pa(
                2,
                2,
                P.HOME_RUN,
                pitcher=RELIEVER,
                substitutions=[RunnerSubstitution(Base.FIRST, 1, 99)],
                advances=[_to(2, Base.BATTER, Base.HOME), _to(99, Base.FIRST, Base.HOME)],
            ),
        ]

        self.assertEqual(services.runs_allowed_for(entries, STARTER), 1)
        self.assertEqual(services.runs_allowed_for(entries, RELIEVER), 1)

    def test_runner_who_reached_on_an_error_is_unearned(self):
        entries = [
            _pa(
                1,
                1,
                P.REACHED_ON_ERROR,
                advances=[_to(1, Base.BATTER, Base.FIRST, R.ERROR, error_index=0)],
                errors=[FieldingError(player_id=6, position=FieldingPosition.SHORTSTOP, kind=ErrorKind.FIELDING)],
            ),
            _pa(
                2,
                2,
                P.HOME_RUN,
                advances=[_to(2, Base.BATTER, Base.HOME), _to(1, Base.FIRST, Base.HOME)],
            ),
        ]

        self.assertEqual(services.runs_allowed_for(entries, STARTER), 2)
        self.assertEqual(services.earned_runs_for(entries, STARTER), 1)

    def test_run_scored_on_a_passed_ball_is_unearned(self):
        entries = [
            _pa(1, 1, P.SINGLE),
            _pa(
                2,
                2,
                P.STRIKEOUT_SWINGING,
                advances=[
                    _to(2, Base.BATTER, Base.OUT, R.PUT_OUT),
                    _to(1, Base.FIRST, Base.THIRD, R.PASSED_BALL),
                ],
            ),
            _pa(
                3,
                3,
                P.SACRIFICE_FLY,
                advances=[_to(3, Base.BATTER, Base.OUT, R.PUT_OUT), _to(1, Base.THIRD, Base.HOME, R.TAG_UP)],
            ),
        ]

        self.assertEqual(services.runs_allowed_for(entries, STARTER), 1)
        self.assertEqual(services.earned_runs_for(entries, STARTER), 0)

    def test_run_scored_on_a_wild_pitch_is_earned(self):
        entries = [
            _pa(1, 1, P.TRIPLE),
            _pa(
                2,
                2,
                P.STRIKEOUT_SWINGING,
                advances=[
                    _to(2, Base.BATTER, Base.OUT, R.PUT_OUT),
                    _to(1, Base.THIRD, Base.HOME, R.WILD_PITCH),
                ],
            ),
        ]

        self.assertEqual(services.earned_runs_for(entries, STARTER), 1)


class FieldingErrorTest(TestCase):
    def test_errors_are_counted_per_player(self):
        entries = [
            _pa(
                1,
                1,
                P.REACHED_ON_ERROR,
                advances=[_to(1, Base.BATTER, Base.FIRST, R.ERROR, error_index=0)],
                errors=[FieldingError(player_id=6, position=FieldingPosition.SHORTSTOP, kind=ErrorKind.THROWING)],
            ),
        ]

        self.assertEqual(services.errors_for(entries, player_id=6), 1)
        self.assertEqual(services.errors_for(entries, player_id=4), 0)


class RunsAllowedTest(TestCase):
    """失点と自責点。失策が絡むと差が出る。"""

    def test_runs_allowed_counts_unearned_runs_too(self):
        entries = [
            _pa(
                1,
                1,
                P.REACHED_ON_ERROR,
                advances=[_to(1, Base.BATTER, Base.FIRST, R.ERROR, error_index=0)],
                errors=[FieldingError(player_id=6, position=FieldingPosition.SHORTSTOP, kind=ErrorKind.THROWING)],
            ),
            _pa(
                2,
                2,
                P.HOME_RUN,
                advances=[_to(1, Base.FIRST, Base.HOME), _to(2, Base.BATTER, Base.HOME)],
            ),
        ]

        line = services.pitching_line_for(entries, STARTER)

        # 失策で出た走者の得点は失点だが自責点ではない
        self.assertEqual(line.runs_allowed, 2)
        self.assertEqual(line.earned_runs, 1)


class PitchingLinesForGameTest(TestCase):
    """1試合の投手全員ぶんを1回の再生で数える `pitching_lines_for`（#384）。

    1人ずつ数える `pitching_line_for` と同じ規則（失点は走者を出した投手に付く・失策で出た走者の得点は
    自責点にしない）で、投手ごとの成績を返す。
    """

    def _relief_game(self):
        """1回表。先発が失策と単打で走者を2人出して1アウトを取り、救援が本塁打を打たれて3人とも還る。"""
        return [
            _pa(
                1,
                1,
                P.REACHED_ON_ERROR,
                pitcher=STARTER,
                advances=[_to(1, Base.BATTER, Base.FIRST, R.ERROR, error_index=0)],
                errors=[FieldingError(player_id=6, position=FieldingPosition.SHORTSTOP, kind=ErrorKind.FIELDING)],
            ),
            _pa(2, 2, P.STRIKEOUT_SWINGING, pitcher=STARTER),
            _pa(
                3,
                3,
                P.SINGLE,
                pitcher=STARTER,
                advances=[_to(3, Base.BATTER, Base.FIRST), _to(1, Base.FIRST, Base.SECOND)],
            ),
            _pa(
                4,
                4,
                P.HOME_RUN,
                pitcher=RELIEVER,
                advances=[
                    _to(1, Base.SECOND, Base.HOME),
                    _to(3, Base.FIRST, Base.HOME),
                    _to(4, Base.BATTER, Base.HOME),
                ],
            ),
        ]

    def test_each_pitcher_gets_the_runs_of_the_runners_he_allowed(self):
        lines = services.pitching_lines_for(self._relief_game())

        self.assertEqual(set(lines), {STARTER, RELIEVER})
        starter, reliever = lines[STARTER], lines[RELIEVER]
        # 先発: 3人に投げて1アウト。出した走者2人が還って失点2、うち失策の走者の1点は自責点ではない
        self.assertEqual(starter.innings.outs, 1)
        self.assertEqual((starter.runs_allowed, starter.earned_runs), (2, 1))
        self.assertEqual((starter.hits_allowed, starter.strikeouts), (1, 1))
        # 救援: 本塁打の打者走者の1点だけ
        self.assertEqual(reliever.innings.outs, 0)
        self.assertEqual((reliever.runs_allowed, reliever.earned_runs), (1, 1))
        self.assertEqual((reliever.hits_allowed, reliever.home_runs_allowed), (1, 1))

    def test_agrees_with_counting_one_pitcher_at_a_time(self):
        entries = self._relief_game()
        lines = services.pitching_lines_for(entries)

        for pitcher_id in (STARTER, RELIEVER):
            with self.subTest(pitcher=pitcher_id):
                self.assertEqual(lines[pitcher_id], services.pitching_line_for(entries, pitcher_id))

    def test_the_order_of_the_given_plate_appearances_does_not_matter(self):
        """走者の経路は打席の通し番号の順に再生する。渡された順ではない。"""
        entries = self._relief_game()

        self.assertEqual(services.pitching_lines_for(list(reversed(entries))), services.pitching_lines_for(entries))

    def test_a_pitcher_who_faced_no_batter_is_not_in_the_result(self):
        entries = self._relief_game()

        self.assertNotIn(999, services.pitching_lines_for(entries))
        self.assertEqual(services.pitching_line_for(entries, 999).runs_allowed, 0)
        self.assertEqual(services.pitching_line_for(entries, 999).innings.outs, 0)
