"""左投げは捕・二・三・遊に就かない規則（#158）。規則の関数・選手の生成・AI のスタメンと代打。"""

from dataclasses import replace
from unittest import TestCase

from myapp.domain.simulation.manager import (
    ForeignQuota,
    PinchHitSituation,
    SimBatter,
    choose_lineup,
    choose_pinch_hitter,
    plays_position,
)
from myapp.domain.simulation.ratings import BatterRatings
from myapp.domain.value_objects import FieldingPosition, Handedness, Position
from myapp.domain.virtual_players import generator

from .test_ai_manager import full_pool
from .test_virtual_players import OnlyRandom

FP = FieldingPosition
RESTRICTED = (FP.CATCHER, FP.SECOND_BASE, FP.THIRD_BASE, FP.SHORTSTOP)


def left_handed(batter: SimBatter) -> SimBatter:
    return replace(batter, throws=Handedness.LEFT)


def right_handed(batter: SimBatter) -> SimBatter:
    return replace(batter, throws=Handedness.RIGHT)


class RuleTest(TestCase):
    def test_left_handers_cannot_take_catcher_second_third_or_short(self) -> None:
        for position in RESTRICTED:
            with self.subTest(position=position):
                self.assertFalse(position.allows_left_handed_thrower)
                self.assertFalse(position.suits_thrower(Handedness.LEFT))

    def test_left_handers_can_take_every_other_position(self) -> None:
        for position in FP:
            if position in RESTRICTED:
                continue
            with self.subTest(position=position):
                self.assertTrue(position.allows_left_handed_thrower)
                self.assertTrue(position.suits_thrower(Handedness.LEFT))

    def test_right_handers_and_unknown_throwers_are_never_restricted(self) -> None:
        for position in FP:
            self.assertTrue(position.suits_thrower(Handedness.RIGHT))
            self.assertTrue(position.suits_thrower(None))


class GenerationTest(TestCase):
    def test_no_catcher_throws_left(self) -> None:
        rng = OnlyRandom(23)
        lefts = sum(generator.handedness(rng, Position.CATCHER)[0] is Handedness.LEFT for _ in range(2000))
        self.assertEqual(lefts, 0)

    def test_few_infielders_throw_left(self) -> None:
        rng = OnlyRandom(23)
        lefts = sum(generator.handedness(rng, Position.INFIELDER)[0] is Handedness.LEFT for _ in range(2000))
        self.assertGreater(lefts, 0)
        self.assertLess(lefts / 2000, 0.12)

    def test_outfielders_and_pitchers_keep_the_old_ratio(self) -> None:
        rng = OnlyRandom(23)
        for position in (Position.OUTFIELDER, Position.PITCHER):
            lefts = sum(generator.handedness(rng, position)[0] is Handedness.LEFT for _ in range(2000))
            self.assertTrue(0.2 < lefts / 2000 < 0.3, position)


def field_of(lineup: list, position: FieldingPosition) -> SimBatter:
    return next(slot.batter for slot in lineup if slot.position is position)


class LineupTest(TestCase):
    def assert_rule_kept(self, lineup: list) -> None:
        self.assertEqual(len(lineup), 9)
        self.assertEqual(len({slot.position for slot in lineup}), 9)
        for slot in lineup:
            self.assertTrue(slot.position.suits_thrower(slot.batter.throws), (slot.position, slot.batter.player_id))

    def test_left_handed_infielders_only_take_first_base(self) -> None:
        """内野手は全員左投げ。一塁には就けるが、二・三・遊には置かず、右投げの別の選手が入る。"""
        batters = tuple(left_handed(b) if b.position is Position.INFIELDER else b for b in full_pool().batters)
        lineup = choose_lineup(batters, ForeignQuota())
        self.assert_rule_kept(lineup)
        for position in (FP.SECOND_BASE, FP.THIRD_BASE, FP.SHORTSTOP):
            self.assertNotEqual(field_of(lineup, position).throws, Handedness.LEFT)
        self.assertEqual(field_of(lineup, FP.FIRST_BASE).position, Position.INFIELDER)

    def test_a_single_left_handed_star_takes_first_base(self) -> None:
        pool = full_pool()
        best = pool.batters[3]  # 最も強い内野手（能力は番号が若いほど高い）
        self.assertIs(best.position, Position.INFIELDER)
        batters = tuple(left_handed(b) if b.player_id == best.player_id else b for b in pool.batters)
        lineup = choose_lineup(batters, ForeignQuota())
        self.assert_rule_kept(lineup)
        self.assertEqual(field_of(lineup, FP.FIRST_BASE).player_id, best.player_id)

    def test_left_handed_catchers_are_not_put_behind_the_plate(self) -> None:
        batters = tuple(left_handed(b) if b.position is Position.CATCHER else b for b in full_pool().batters)
        lineup = choose_lineup(batters, ForeignQuota())
        self.assert_rule_kept(lineup)
        self.assertNotEqual(field_of(lineup, FP.CATCHER).throws, Handedness.LEFT)

    def test_a_right_handed_catcher_is_chosen_over_a_stronger_left_handed_one(self) -> None:
        pool = full_pool()
        catchers = [b for b in pool.batters if b.position is Position.CATCHER]
        strongest = catchers[0]
        batters = tuple(
            left_handed(b) if b.player_id == strongest.player_id else right_handed(b) for b in pool.batters
        )
        lineup = choose_lineup(batters, ForeignQuota())
        self.assert_rule_kept(lineup)
        self.assertNotEqual(field_of(lineup, FP.CATCHER).player_id, strongest.player_id)

    def test_the_rule_bends_only_when_no_right_hander_remains(self) -> None:
        """全員が左投げの球団でも、スタメンは組める（枠が埋まらず進行が止まるよりよい）。"""
        batters = tuple(left_handed(b) for b in full_pool().batters)
        lineup = choose_lineup(batters, ForeignQuota())
        self.assertEqual(len(lineup), 9)
        self.assertEqual(len({slot.batter.player_id for slot in lineup}), 9)

    def test_unknown_throwers_are_not_restricted(self) -> None:
        lineup = choose_lineup(full_pool().batters, ForeignQuota())
        self.assertEqual(len(lineup), 9)


class PinchHitTest(TestCase):
    def situation(self, candidate: SimBatter, position: FieldingPosition) -> PinchHitSituation:
        weak = SimBatter(
            1,
            "守備の人",
            Position.INFIELDER,
            BatterRatings(contact=30, power=30, eye=30, speed=30, fielding=90),
            throws=Handedness.RIGHT,
        )
        return PinchHitSituation(
            inning=8, lead=-2, pinch_hitters_used=0, current=weak, position=position, bench=(candidate,)
        )

    def test_a_left_handed_pinch_hitter_does_not_stay_at_short(self) -> None:
        strong = SimBatter(
            2,
            "強打の左",
            Position.INFIELDER,
            BatterRatings(contact=90, power=90, eye=90, speed=90, fielding=40),
            throws=Handedness.LEFT,
        )
        self.assertFalse(plays_position(strong, FP.SHORTSTOP))
        self.assertIsNone(choose_pinch_hitter(self.situation(strong, FP.SHORTSTOP)))

    def test_a_left_handed_pinch_hitter_may_stay_at_first(self) -> None:
        strong = SimBatter(
            2,
            "強打の左",
            Position.INFIELDER,
            BatterRatings(contact=90, power=90, eye=90, speed=90, fielding=40),
            throws=Handedness.LEFT,
        )
        result = choose_pinch_hitter(self.situation(strong, FP.FIRST_BASE))
        self.assertIsNotNone(result)


class LineupQuotaTest(TestCase):
    def test_left_handers_that_cannot_be_seated_do_not_use_up_the_foreign_quota(self) -> None:
        """座れない左投げの外国人が出場枠を先に使っても、右投げの外国人を二・三・遊に回せる。"""
        pool = full_pool(foreign_ids=(4, 5, 6))
        batters = tuple(
            left_handed(b) if b.position is Position.INFIELDER and b.player_id in (4, 5) else b for b in pool.batters
        )
        lineup = choose_lineup(batters, ForeignQuota(limit=1))
        for slot in lineup:
            self.assertTrue(slot.position.suits_thrower(slot.batter.throws), (slot.position, slot.batter.player_id))
        self.assertLessEqual(sum(slot.batter.is_foreign for slot in lineup), 1)
