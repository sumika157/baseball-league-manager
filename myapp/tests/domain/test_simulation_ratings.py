"""能力値・成長型・区分（S〜G）と、乱数の扱いの単体テスト。Django も DB も使わない。"""

import random
from unittest import TestCase

from myapp.domain.exceptions import InvalidRatings
from myapp.domain.simulation.randomness import game_seed, make_random, normal, weighted_index
from myapp.domain.simulation.ratings import (
    AVERAGE_RATING,
    BatterRatings,
    GrowthType,
    PitcherRatings,
    RatingGrade,
)


class RatingsTest(TestCase):
    def test_default_ratings_are_the_league_average(self):
        batter = BatterRatings()
        pitcher = PitcherRatings()
        self.assertEqual(
            (batter.contact, batter.power, batter.eye, batter.speed, batter.fielding), (AVERAGE_RATING,) * 5
        )
        self.assertEqual(
            (pitcher.stuff, pitcher.control, pitcher.home_run_avoidance, pitcher.stamina), (AVERAGE_RATING,) * 4
        )
        self.assertIs(batter.growth, GrowthType.NORMAL)

    def test_values_outside_1_to_100_are_rejected(self):
        for value in (0, 101, -5):
            with self.assertRaises(InvalidRatings):
                BatterRatings(contact=value)
            with self.assertRaises(InvalidRatings):
                PitcherRatings(stamina=value)

    def test_the_boundaries_are_accepted(self):
        BatterRatings(1, 100, 1, 100, 50)
        PitcherRatings(1, 100, 1, 100)

    def test_non_integers_are_rejected(self):
        with self.assertRaises(InvalidRatings):
            BatterRatings(contact=50.5)  # type: ignore[arg-type]
        with self.assertRaises(InvalidRatings):
            PitcherRatings(control=True)

    def test_the_error_message_names_the_item_in_japanese(self):
        with self.assertRaises(InvalidRatings) as caught:
            BatterRatings(eye=0)
        self.assertIn("選球眼", str(caught.exception))

    def test_better_ratings_raise_the_composite_values(self):
        weak, strong = BatterRatings(30, 30, 30, 30, 30), BatterRatings(70, 70, 70, 70, 70)
        self.assertLess(weak.batting_value, strong.batting_value)
        self.assertLess(weak.on_base_value, strong.on_base_value)
        self.assertLess(weak.slugging_value, strong.slugging_value)
        self.assertLess(PitcherRatings(30, 30, 30, 30).starter_value, PitcherRatings(70, 70, 70, 70).starter_value)

    def test_stamina_matters_only_for_the_starter_value(self):
        tired, fresh = PitcherRatings(stamina=20), PitcherRatings(stamina=90)
        self.assertEqual(tired.pitching_value, fresh.pitching_value)
        self.assertLess(tired.starter_value, fresh.starter_value)


class RatingGradeTest(TestCase):
    def test_grade_boundaries(self):
        expected = {
            100: "S",
            90: "S",
            89: "A",
            80: "A",
            79: "B",
            70: "B",
            69: "C",
            60: "C",
            59: "D",
            50: "D",
            49: "E",
            40: "E",
            39: "F",
            20: "F",
            19: "G",
            1: "G",
        }
        for value, grade in expected.items():
            self.assertEqual(RatingGrade.from_value(value).label, grade, f"{value}")

    def test_out_of_range_values_are_rejected(self):
        for value in (0, 101):
            with self.assertRaises(InvalidRatings):
                RatingGrade.from_value(value)

    def test_each_grade_exposes_its_lower_bound_as_the_single_source(self):
        bounds = {grade.label: grade.lower_bound for grade in RatingGrade}
        self.assertEqual(bounds, {"S": 90, "A": 80, "B": 70, "C": 60, "D": 50, "E": 40, "F": 20, "G": 1})


class RandomnessTest(TestCase):
    def test_game_seed_is_stable_across_processes(self):
        """blake2b から作るので、実行ごとに変わる `hash()` と違い値が固定される。"""
        self.assertEqual(game_seed(1, 2026, "g1"), game_seed(1, 2026, "g1"))
        self.assertEqual(game_seed(1, 2026, "g1"), 2819872429653991116)

    def test_game_seed_changes_with_each_part(self):
        base = game_seed(1, 2026, "g1")
        self.assertNotEqual(base, game_seed(2, 2026, "g1"))
        self.assertNotEqual(base, game_seed(1, 2027, "g1"))
        self.assertNotEqual(base, game_seed(1, 2026, "g2"))

    def test_the_same_seed_gives_the_same_sequence(self):
        """整数のシードは版をまたいでも同じ列になる（`random()` だけを使う理由）。"""
        self.assertEqual([make_random(7).random()], [make_random(7).random()])
        rng = make_random(7)
        self.assertEqual([rng.random(), rng.random()], [0.32383276483316237, 0.15084917392450192])
        self.assertIsInstance(make_random(7), random.Random)

    def test_normal_uses_exactly_two_draws_and_has_the_right_moments(self):
        class Counting:
            def __init__(self) -> None:
                self.inner = random.Random(3)
                self.calls = 0

            def random(self) -> float:
                self.calls += 1
                return self.inner.random()

        counting = Counting()
        normal(counting)
        self.assertEqual(counting.calls, 2)

        rng = make_random(11)
        samples = [normal(rng, 10.0, 2.0) for _ in range(20000)]
        mean = sum(samples) / len(samples)
        variance = sum((x - mean) ** 2 for x in samples) / len(samples)
        self.assertAlmostEqual(mean, 10.0, delta=0.08)
        self.assertAlmostEqual(variance**0.5, 2.0, delta=0.08)

    def test_weighted_index_follows_the_weights(self):
        rng = make_random(5)
        counts = [0, 0, 0]
        for _ in range(20000):
            counts[weighted_index(rng, [1.0, 2.0, 7.0])] += 1
        self.assertAlmostEqual(counts[0] / 20000, 0.1, delta=0.01)
        self.assertAlmostEqual(counts[2] / 20000, 0.7, delta=0.015)

    def test_weighted_index_with_zero_weights_returns_the_first(self):
        self.assertEqual(weighted_index(make_random(1), [0.0, 0.0]), 0)
