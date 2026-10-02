"""スコアボードの安打・失策の導出。Django も DB も使わない。"""

from unittest import TestCase

from myapp.domain import services
from myapp.domain.entities import FieldingError, PlateAppearance, RunnerAdvance
from myapp.domain.value_objects import (
    Base,
    ErrorKind,
    FieldingPosition,
    PlateAppearanceResult,
)

P = PlateAppearanceResult


def _pa(sequence, inning, bottom, result, *, errors=()) -> PlateAppearance:
    """打席を1つ作る。打者は打順 1 の 1 番で固定（導出には関係しない）。"""
    return PlateAppearance(
        sequence=sequence,
        inning=inning,
        is_bottom=bottom,
        batter_id=1,
        pitcher_id=100,
        batting_order=1,
        result=result,
        advances=[
            RunnerAdvance(
                runner_id=1,
                from_base=Base.BATTER,
                to_base=result.default_batter_base,
                reason=result.default_batter_reason,
            )
        ],
        errors=list(errors),
    )


def _error() -> FieldingError:
    return FieldingError(player_id=9, position=FieldingPosition.SHORTSTOP, kind=ErrorKind.THROWING)


class HitsByInningTest(TestCase):
    def test_hits_are_counted_for_the_team_at_bat(self):
        entries = [
            _pa(1, 1, False, P.SINGLE),
            _pa(2, 1, False, P.STRIKEOUT_SWINGING),
            _pa(3, 1, True, P.HOME_RUN),
            _pa(4, 2, False, P.WALK),
            _pa(5, 2, True, P.DOUBLE),
            _pa(6, 2, True, P.TRIPLE),
        ]

        self.assertEqual(services.hits_by_inning(entries, home=False), {1: 1})
        self.assertEqual(services.hits_by_inning(entries, home=True), {1: 1, 2: 2})

    def test_walks_and_errors_are_not_hits(self):
        entries = [_pa(1, 1, False, P.WALK), _pa(2, 1, False, P.REACHED_ON_ERROR, errors=[_error()])]

        self.assertEqual(services.hits_by_inning(entries, home=False), {})

    def test_extra_innings_are_included(self):
        entries = [_pa(1, 10, False, P.SINGLE), _pa(2, 11, True, P.SINGLE)]

        self.assertEqual(services.hits_by_inning(entries, home=False), {10: 1})
        self.assertEqual(services.hits_by_inning(entries, home=True), {11: 1})


class ErrorsByInningTest(TestCase):
    def test_an_error_is_charged_to_the_fielding_team(self):
        """表の打席（ビジターの攻撃）の失策はホームの失策になる。"""
        entries = [
            _pa(1, 1, False, P.REACHED_ON_ERROR, errors=[_error()]),
            _pa(2, 2, True, P.GROUND_OUT, errors=[_error(), _error()]),
        ]

        self.assertEqual(services.errors_by_inning(entries, home=True), {1: 1})
        self.assertEqual(services.errors_by_inning(entries, home=False), {2: 2})

    def test_an_error_does_not_count_as_a_hit(self):
        entries = [_pa(1, 1, False, P.REACHED_ON_ERROR, errors=[_error()])]

        self.assertEqual(services.hits_by_inning(entries, home=False), {})
        self.assertEqual(services.errors_by_inning(entries, home=True), {1: 1})

    def test_no_bottom_of_the_last_inning_leaves_the_home_batting_empty(self):
        """9回裏が行われなければ、ビジターの失策は 9 回に載らない。"""
        entries = [_pa(1, 9, False, P.SINGLE, errors=[])]

        self.assertEqual(services.errors_by_inning(entries, home=False), {})
        self.assertEqual(services.hits_by_inning(entries, home=True), {})

    def test_an_empty_record_gives_nothing(self):
        self.assertEqual(services.errors_by_inning([], home=True), {})
        self.assertEqual(services.hits_by_inning([], home=False), {})
