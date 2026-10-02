"""3つ目のアウトと得点（公認野球規則 5.08）の単体テスト。Django も DB も使わない。

3つ目のアウトが「打者走者が一塁に触れる前のアウト」または「封殺」なら、そのプレイの間に
本塁に達した走者の得点は記録しない。打者が一塁に達した後の走者のアウト（タイムプレー）が
3つ目なら、その前に還った得点は認める。
"""

from unittest import TestCase

from myapp.domain.exceptions import InvalidPlateAppearance
from myapp.domain.value_objects import AdvanceReason, Base, PlateAppearanceResult

from .test_plate_appearances import _game, _pa, _to

P = PlateAppearanceResult
R = AdvanceReason


def _two_outs_with_a_runner_on_third():
    """1回表。三塁打で1番が三塁に出て、2番・3番が三振して2アウト。次の打者は4番。"""
    return [
        _pa(1, 1, P.TRIPLE),
        _pa(2, 2, P.STRIKEOUT_SWINGING),
        _pa(3, 3, P.STRIKEOUT_LOOKING),
    ]


class ThirdOutCancelsRunsTest(TestCase):
    def _assert_rejected(self, last):
        game = _game([*_two_outs_with_a_runner_on_third(), last], away_score=1)

        with self.assertRaisesRegex(InvalidPlateAppearance, "5.08"):
            game.ensure_plate_appearances_consistent()

    def test_a_run_on_a_ground_out_that_ends_the_inning_is_rejected(self):
        self._assert_rejected(
            _pa(
                4,
                4,
                P.GROUND_OUT,
                advances=[_to(4, Base.BATTER, Base.OUT, R.PUT_OUT), _to(1, Base.THIRD, Base.HOME)],
            )
        )

    def test_a_run_on_a_fly_out_that_ends_the_inning_is_rejected(self):
        self._assert_rejected(
            _pa(
                4,
                4,
                P.FLY_OUT,
                advances=[_to(4, Base.BATTER, Base.OUT, R.PUT_OUT), _to(1, Base.THIRD, Base.HOME, R.TAG_UP)],
            )
        )

    def test_a_run_on_a_force_out_that_ends_the_inning_is_rejected(self):
        """野選出塁で二塁走者が封殺され、その間に三塁走者が還っても得点にならない。"""
        game = _game(
            [
                _pa(1, 1, P.TRIPLE),
                _pa(2, 2, P.SINGLE),
                _pa(3, 3, P.STRIKEOUT_SWINGING),
                _pa(4, 4, P.STRIKEOUT_LOOKING),
                _pa(
                    5,
                    5,
                    P.FIELDERS_CHOICE,
                    advances=[
                        _to(5, Base.BATTER, Base.FIRST, R.FIELDERS_CHOICE),
                        _to(2, Base.FIRST, Base.OUT, R.FORCE_OUT),
                        _to(1, Base.THIRD, Base.HOME, R.FIELDERS_CHOICE),
                    ],
                ),
            ],
            away_score=1,
        )

        with self.assertRaisesRegex(InvalidPlateAppearance, "5.08"):
            game.ensure_plate_appearances_consistent()

    def test_a_run_on_a_double_play_that_ends_the_inning_is_rejected(self):
        """1アウトからの併殺（打者のアウトと封殺）で3つ目が取られたなら、還った走者は無効。"""
        game = _game(
            [
                _pa(1, 1, P.TRIPLE),
                _pa(2, 2, P.SINGLE),
                _pa(3, 3, P.STRIKEOUT_SWINGING),
                _pa(
                    4,
                    4,
                    P.GROUND_OUT,
                    advances=[
                        _to(4, Base.BATTER, Base.OUT, R.PUT_OUT),
                        _to(2, Base.FIRST, Base.OUT, R.FORCE_OUT),
                        _to(1, Base.THIRD, Base.HOME),
                    ],
                ),
            ],
            away_score=1,
        )

        with self.assertRaisesRegex(InvalidPlateAppearance, "5.08"):
            game.ensure_plate_appearances_consistent()


class ThirdOutAllowsRunsTest(TestCase):
    def test_a_run_before_the_third_out_is_counted_when_the_batter_was_thrown_out_on_the_bases(self):
        """単打を打った打者が二塁を狙って刺されたのは、打者が一塁に達した後のアウト。

        その前に三塁走者が本塁に達していれば得点は認める（タイムプレー）。
        """
        game = _game(
            [
                *_two_outs_with_a_runner_on_third(),
                _pa(
                    4,
                    4,
                    P.SINGLE,
                    advances=[_to(4, Base.BATTER, Base.OUT, R.THROWN_OUT), _to(1, Base.THIRD, Base.HOME)],
                ),
            ],
            away_score=1,
        )

        game.ensure_plate_appearances_consistent()

    def test_a_run_is_counted_when_a_runner_is_thrown_out_after_the_batter_reached_first(self):
        game = _game(
            [
                _pa(1, 1, P.TRIPLE),
                _pa(2, 2, P.SINGLE),
                _pa(3, 3, P.STRIKEOUT_SWINGING),
                _pa(4, 4, P.STRIKEOUT_LOOKING),
                _pa(
                    5,
                    5,
                    P.SINGLE,
                    advances=[
                        _to(5, Base.BATTER, Base.FIRST),
                        _to(2, Base.FIRST, Base.OUT, R.THROWN_OUT),
                        _to(1, Base.THIRD, Base.HOME),
                    ],
                ),
            ],
            away_score=1,
        )

        game.ensure_plate_appearances_consistent()

    def test_a_run_on_a_wild_pitch_before_the_strikeout_that_ends_the_inning_is_counted(self):
        """暴投で還ったのは三振とは別のプレイ。その後の三振が3つ目のアウトでも得点は認める。"""
        for reason in (R.WILD_PITCH, R.PASSED_BALL, R.BALK, R.STOLEN_BASE):
            with self.subTest(reason=reason.label):
                game = _game(
                    [
                        *_two_outs_with_a_runner_on_third(),
                        _pa(
                            4,
                            4,
                            P.STRIKEOUT_SWINGING,
                            advances=[_to(4, Base.BATTER, Base.OUT, R.PUT_OUT), _to(1, Base.THIRD, Base.HOME, reason)],
                        ),
                    ],
                    away_score=1,
                )

                game.ensure_plate_appearances_consistent()

    def test_a_run_on_a_ground_out_with_fewer_than_three_outs_is_counted(self):
        game = _game(
            [
                _pa(1, 1, P.TRIPLE),
                _pa(2, 2, P.STRIKEOUT_SWINGING),
                _pa(
                    3,
                    3,
                    P.GROUND_OUT,
                    advances=[_to(3, Base.BATTER, Base.OUT, R.PUT_OUT), _to(1, Base.THIRD, Base.HOME)],
                ),
            ],
            away_score=1,
        )

        game.ensure_plate_appearances_consistent()

    def test_the_rule_does_not_carry_over_to_the_next_half_inning(self):
        """前の半回の3つ目のアウトは、次の半回の得点に影響しない。"""
        game = _game(
            [
                _pa(1, 1, P.STRIKEOUT_SWINGING),
                _pa(2, 2, P.STRIKEOUT_SWINGING),
                _pa(3, 3, P.STRIKEOUT_SWINGING),
                _pa(4, 1, P.HOME_RUN, bottom=True),
            ],
            home_score=1,
        )

        game.ensure_plate_appearances_consistent()


class ThirdOutReasonTest(TestCase):
    def test_only_the_batters_put_out_and_force_outs_cancel_runs(self):
        cancelling = {reason for reason in R if reason.cancels_runs_when_third_out}

        self.assertEqual(cancelling, {R.PUT_OUT, R.FORCE_OUT})

    def test_runs_between_pitches_are_a_different_play(self):
        between = {reason for reason in R if reason.happens_between_pitches}

        self.assertEqual(between, {R.STOLEN_BASE, R.WILD_PITCH, R.PASSED_BALL, R.BALK})
