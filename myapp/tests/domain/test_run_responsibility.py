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
