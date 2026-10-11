"""年齢による成長と衰え（`domain/pennant/aging.py`）。DB 不要。"""

from __future__ import annotations

import random
import unittest
from statistics import fmean

from myapp.domain.pennant import aging
from myapp.domain.pennant.aging import BATTER_CURVES, PITCHER_CURVES, age_ratings, curve_change, expected_change
from myapp.domain.simulation.ratings import BatterRatings, GrowthType, PitcherRatings


class OnlyRandom:
    """`random()` しか持たない乱数源。"""

    def __init__(self, seed: int) -> None:
        self._rng = random.Random(seed)

    def random(self) -> float:
        return self._rng.random()


class CurveTest(unittest.TestCase):
    def test_young_grow_and_old_decline(self) -> None:
        self.assertGreater(curve_change(19), curve_change(25))
        self.assertGreater(curve_change(25), 0)
        self.assertLess(curve_change(31), 0)
        self.assertLess(curve_change(39), curve_change(31))

    def test_curve_is_monotonic_non_increasing(self) -> None:
        changes = [curve_change(age) for age in range(15, 46)]
        self.assertEqual(changes, sorted(changes, reverse=True))

    def test_age_brackets(self) -> None:
        self.assertEqual(curve_change(0), curve_change(19))
        self.assertEqual(curve_change(20), curve_change(21))
        self.assertNotEqual(curve_change(21), curve_change(22))
        self.assertEqual(curve_change(38), curve_change(45))


class ExpectedChangeTest(unittest.TestCase):
    def change(self, name: str, age: int, growth: GrowthType = GrowthType.NORMAL, value: int = 50) -> float:
        curves = BATTER_CURVES if name in BATTER_CURVES else PITCHER_CURVES
        return expected_change(curves[name], age=age, growth=growth, value=value)

    def test_early_peaks_sooner_and_late_peaks_later(self) -> None:
        """早熟は曲線を2歳先で読む（若いうちだけ伸びが大きく、すぐ頭打ちになって衰えも早い）。晩成は逆。"""
        self.assertGreater(self.change("contact", 19, GrowthType.EARLY), self.change("contact", 19))
        self.assertGreater(self.change("contact", 19), self.change("contact", 19, GrowthType.LATE))
        for age in (26, 33):
            with self.subTest(age=age):
                self.assertLess(self.change("contact", age, GrowthType.EARLY), self.change("contact", age))
                self.assertGreater(self.change("contact", age, GrowthType.LATE), self.change("contact", age))

    def test_speed_declines_before_eye(self) -> None:
        for age in (30, 33, 36):
            with self.subTest(age=age):
                self.assertLess(self.change("speed", age), self.change("eye", age))

    def test_stuff_declines_before_control(self) -> None:
        self.assertLess(self.change("stuff", 33), self.change("control", 33))

    def test_high_rating_grows_slowly(self) -> None:
        slow = self.change("contact", 21, value=aging.HIGH_RATING)
        self.assertAlmostEqual(slow, self.change("contact", 21, value=50) * aging.HIGH_RATING_GAIN)
        # 衰えには効かない
        self.assertEqual(self.change("contact", 36, value=80), self.change("contact", 36, value=50))


class AgeRatingsTest(unittest.TestCase):
    batter = BatterRatings(50, 50, 50, 50, 50, GrowthType.LATE)
    pitcher = PitcherRatings(50, 50, 50, 50, GrowthType.EARLY)

    def test_uses_only_random(self) -> None:
        age_ratings(self.batter, age=25, rng=OnlyRandom(1))
        age_ratings(self.pitcher, age=25, rng=OnlyRandom(1))

    def test_same_random_gives_same_result_and_keeps_growth_type(self) -> None:
        first = age_ratings(self.batter, age=25, rng=OnlyRandom(4))
        self.assertEqual(first, age_ratings(self.batter, age=25, rng=OnlyRandom(4)))
        self.assertIs(first.growth, GrowthType.LATE)
        aged_pitcher = age_ratings(self.pitcher, age=25, rng=OnlyRandom(4))
        self.assertIsInstance(aged_pitcher, PitcherRatings)
        self.assertIs(aged_pitcher.growth, GrowthType.EARLY)

    def test_stays_within_bounds(self) -> None:
        rng = OnlyRandom(9)
        low = BatterRatings(1, 1, 1, 1, 1)
        high = BatterRatings(100, 100, 100, 100, 100)
        for _ in range(100):
            for age, ratings in ((40, low), (19, high), (19, low), (40, high)):
                aged = age_ratings(ratings, age=age, rng=rng)
                assert isinstance(aged, BatterRatings)
                for name in BatterRatings.LABELS:
                    self.assertTrue(1 <= getattr(aged, name) <= 100)

    def test_average_change_follows_the_age(self) -> None:
        def average(age: int) -> float:
            rng = OnlyRandom(21)
            moves = []
            for _ in range(400):
                aged = age_ratings(BatterRatings(), age=age, rng=rng)
                assert isinstance(aged, BatterRatings)
                moves.append(aged.contact - 50)
            return fmean(moves)

        self.assertGreater(average(20), 1.5)
        self.assertLess(average(36), -2.5)
        self.assertLess(average(36), average(30))


if __name__ == "__main__":
    unittest.main()
