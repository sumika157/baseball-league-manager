"""引退の確率（`domain/pennant/retirement.py`）。DB 不要。"""

from __future__ import annotations

import unittest

from myapp.domain.pennant import retirement
from myapp.domain.pennant.retirement import MAX_PLAYING_AGE, PlayingTime, retirement_chance

REGULAR_BATTER = PlayingTime(plate_appearances=100)


def chance(
    age: int = 30,
    value: float = 45.0,
    playing_time: PlayingTime = REGULAR_BATTER,
    is_foreign: bool = False,
    seasons_as_pro: int = 10,
) -> float:
    return retirement_chance(
        age=age, value=value, playing_time=playing_time, is_foreign=is_foreign, seasons_as_pro=seasons_as_pro
    )


class RetirementChanceTest(unittest.TestCase):
    def test_max_age_always_retires(self) -> None:
        self.assertEqual(MAX_PLAYING_AGE, 41)
        for age in (41, 42, 50):
            self.assertEqual(chance(age=age, value=99.0, playing_time=PlayingTime(plate_appearances=600)), 1.0)
        self.assertLess(chance(age=40, value=99.0), 1.0)

    def test_older_retire_more_often(self) -> None:
        chances = [chance(age=age) for age in range(20, 41)]
        self.assertEqual(chances, sorted(chances))

    def test_weaker_retire_more_often(self) -> None:
        values = [chance(age=33, value=value) for value in range(30, 61, 5)]
        self.assertEqual(values, sorted(values, reverse=True))

    def test_value_multiplier_is_clamped(self) -> None:
        self.assertEqual(chance(age=30, value=0.0), chance(age=30, value=-50.0))
        self.assertEqual(chance(age=30, value=100.0), chance(age=30, value=200.0))

    def test_foreign_players_leave_twice_as_often(self) -> None:
        self.assertAlmostEqual(chance(age=30, is_foreign=True), 2 * chance(age=30))

    def test_regulars_stay_and_idle_veterans_leave(self) -> None:
        base = chance(age=30)
        regular_batter = chance(age=30, playing_time=PlayingTime(plate_appearances=550))
        regular_pitcher = chance(age=30, playing_time=PlayingTime(outs=300))
        idle = chance(age=30, playing_time=PlayingTime())
        self.assertAlmostEqual(regular_batter, base * retirement.REGULAR_FACTOR)
        self.assertAlmostEqual(regular_pitcher, base * retirement.REGULAR_FACTOR)
        self.assertAlmostEqual(idle, base * retirement.IDLE_FACTOR)

    def test_idle_factor_does_not_apply_to_the_young(self) -> None:
        age = retirement.IDLE_MIN_AGE - 1
        self.assertEqual(chance(age=age, playing_time=PlayingTime()), chance(age=age))

    def test_recent_young_players_are_protected(self) -> None:
        self.assertAlmostEqual(chance(age=22, seasons_as_pro=1), chance(age=22) * retirement.ROOKIE_FACTOR)
        self.assertAlmostEqual(chance(age=22, seasons_as_pro=2), chance(age=22) * retirement.ROOKIE_FACTOR)
        self.assertEqual(chance(age=22, seasons_as_pro=3), chance(age=22))
        # 25歳を過ぎれば入団直後でも守られない
        self.assertEqual(chance(age=25, seasons_as_pro=1), chance(age=25))

    def test_capped_below_certainty(self) -> None:
        worst = chance(age=40, value=0.0, playing_time=PlayingTime(), is_foreign=True)
        self.assertEqual(worst, retirement.MAX_CHANCE)

    def test_playing_time_flags(self) -> None:
        self.assertTrue(PlayingTime(plate_appearances=300).is_regular)
        self.assertFalse(PlayingTime(plate_appearances=299).is_regular)
        self.assertTrue(PlayingTime(outs=240).is_regular)
        self.assertTrue(PlayingTime().is_idle)
        self.assertFalse(PlayingTime(outs=1).is_idle)


if __name__ == "__main__":
    unittest.main()
