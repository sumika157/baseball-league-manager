"""能力の表の並べ替えと、区分の目立たせ方（ペナントの球団の画面）。Django も DB も使わない。"""

from unittest import TestCase

from myapp.domain import services
from myapp.domain.entities import Player
from myapp.domain.simulation.ratings import BatterRatings, PitcherRatings, RatingEmphasis, RatingGrade
from myapp.domain.value_objects import InningsPitched, JerseyNumber, PitchingLine, Position


def _batter(number: int, ratings: BatterRatings | None) -> services.RatedBatter:
    player = Player(name=f"野手{number}", number=JerseyNumber(number), position=Position.INFIELDER, id=number)
    return services.RatedBatter(player, ratings)


def _pitcher(number: int, ratings: PitcherRatings | None, outs: int = 27) -> services.RatedPitcher:
    player = Player(
        name=f"投手{number}",
        number=JerseyNumber(number),
        position=Position.PITCHER,
        id=number,
        pitching=PitchingLine(innings=InningsPitched(outs=outs)),
    )
    return services.RatedPitcher(player, ratings)


def _numbers(rated) -> list[int]:
    return [item.player.number.value for item in rated]


class RatingEmphasisTest(TestCase):
    def test_every_grade_has_an_emphasis(self):
        for grade in RatingGrade:
            with self.subTest(grade=grade.label):
                self.assertIsInstance(grade.emphasis, RatingEmphasis)

    def test_emphasis_by_grade(self):
        expected = {
            "S": RatingEmphasis.HIGH,
            "A": RatingEmphasis.HIGH,
            "B": RatingEmphasis.MID,
            "C": RatingEmphasis.MID,
            "D": RatingEmphasis.NONE,
            "E": RatingEmphasis.MUTED,
            "F": RatingEmphasis.MUTED,
            "G": RatingEmphasis.MUTED,
        }
        self.assertEqual({grade.label: grade.emphasis for grade in RatingGrade}, expected)

    def test_it_follows_the_value_through_the_grade(self):
        self.assertIs(RatingGrade.from_value(74).emphasis, RatingEmphasis.MID)
        self.assertIs(RatingGrade.from_value(50).emphasis, RatingEmphasis.NONE)
        self.assertIs(RatingGrade.from_value(1).emphasis, RatingEmphasis.MUTED)


class RatedBatterSortTest(TestCase):
    def setUp(self):
        self.items = [
            _batter(3, BatterRatings(contact=60, power=90)),
            _batter(1, BatterRatings(contact=80, power=40)),
            _batter(2, BatterRatings(contact=70, power=40)),
        ]

    def test_default_is_the_jersey_number_ascending(self):
        ordered, key, descending = services.sort_rated_batters(self.items)

        self.assertEqual((_numbers(ordered), key, descending), ([1, 2, 3], "number", False))

    def test_a_rating_is_descending_by_default(self):
        ordered, key, descending = services.sort_rated_batters(self.items, "power")

        self.assertEqual((_numbers(ordered), key, descending), ([3, 1, 2], "power", True))

    def test_the_direction_can_be_reversed_and_ties_keep_the_number_order(self):
        ordered, _, _ = services.sort_rated_batters(self.items, "power", False)

        self.assertEqual(_numbers(ordered), [1, 2, 3], "同値（40）は背番号の小さい順のまま")

    def test_an_unknown_key_falls_back_to_the_default(self):
        ordered, key, descending = services.sort_rated_batters(self.items, "no_such_key")

        self.assertEqual((_numbers(ordered), key, descending), ([1, 2, 3], "number", False))

    def test_every_rating_label_is_a_sort_key(self):
        """能力の列は `BatterRatings.LABELS` が出典。並べ替えのキーも同じ項目から作られる。"""
        for name in BatterRatings.LABELS:
            with self.subTest(key=name):
                self.assertIn(name, services.BATTER_RATING_SORT_KEYS)
                self.assertTrue(services.BATTER_RATING_SORT_KEYS[name][1], "能力は大きいほど良い")

    def test_players_without_ratings_go_last_in_either_direction(self):
        items = [_batter(1, None), *self.items]

        for descending in (True, False):
            with self.subTest(descending=descending):
                ordered, _, _ = services.sort_rated_batters(items, "contact", descending)
                self.assertEqual(_numbers(ordered)[-1], 1)


class RatedPitcherSortTest(TestCase):
    def setUp(self):
        self.items = [
            _pitcher(11, PitcherRatings(stuff=70)),
            _pitcher(12, PitcherRatings(stuff=85)),
            _pitcher(13, PitcherRatings(stuff=60), outs=0),
        ]

    def test_default_is_the_jersey_number_ascending(self):
        ordered, key, _ = services.sort_rated_pitchers(self.items)

        self.assertEqual((_numbers(ordered), key), ([11, 12, 13], "number"))

    def test_a_rating_is_descending_by_default(self):
        ordered, _, descending = services.sort_rated_pitchers(self.items, "stuff")

        self.assertEqual((_numbers(ordered), descending), ([12, 11, 13], True))

    def test_the_unpitched_go_last_when_sorting_by_era(self):
        ordered, _, descending = services.sort_rated_pitchers(self.items, "era")

        self.assertEqual(_numbers(ordered)[-1], 13, "未登板の防御率は 0 になるので、上位に寄せない")
        self.assertFalse(descending, "防御率は低いほど良い")

    def test_an_unknown_key_falls_back_to_the_default(self):
        ordered, key, _ = services.sort_rated_pitchers(self.items, "average")

        self.assertEqual((_numbers(ordered), key), ([11, 12, 13], "number"))
