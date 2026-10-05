"""自動ドラフト（`domain/pennant/draft.py`）。DB 不要。"""

from __future__ import annotations

import random
import unittest
from collections import Counter
from datetime import date

from myapp.domain.pennant import draft
from myapp.domain.pennant.draft import DraftRoute, DraftTeam, draft_class, draftee_count
from myapp.domain.simulation.ratings import BatterRatings, PitcherRatings
from myapp.domain.value_objects import Position
from myapp.domain.virtual_players import generator
from myapp.domain.virtual_players.generator import AmateurPath

YEAR = 2027


class OnlyRandom:
    """`random()` しか持たない乱数源。"""

    def __init__(self, seed: int) -> None:
        self._rng = random.Random(seed)

    def random(self) -> float:
        return self._rng.random()


def team(
    counts: dict[Position, int] | None = None,
    *,
    foreign: int = 0,
    limit: int | None = 4,
    numbers: frozenset[int] = frozenset(),
    surnames: dict[str, int] | None = None,
) -> DraftTeam:
    default = generator.largest_remainder(draft.ROSTER_TARGET - 4, generator.POSITION_RATIOS)
    return DraftTeam(
        team_id=1,
        position_counts=default if counts is None else counts,
        foreign_count=foreign,
        foreign_limit=limit,
        used_numbers=numbers,
        surname_counts=surnames or {},
    )


def run(target: DraftTeam, seed: int = 1, used: set[str] | None = None) -> tuple[draft.Draftee, ...]:
    return draft_class(OnlyRandom(seed), target, year=YEAR, used_names=set() if used is None else used)


class CountTest(unittest.TestCase):
    def test_refills_toward_the_target_between_two_and_eight(self) -> None:
        self.assertEqual(draftee_count(34), draft.MIN_DRAFTEES)
        self.assertEqual(draftee_count(31), 3)
        self.assertEqual(draftee_count(20), draft.MAX_DRAFTEES)

    def test_never_exceeds_the_roster_limit(self) -> None:
        self.assertEqual(draftee_count(39), 1)
        self.assertEqual(draftee_count(generator.MAX_ROSTER), 0)
        self.assertEqual(draftee_count(45), 0)

    def test_draft_matches_the_count(self) -> None:
        self.assertEqual(len(run(team())), 4)


class PositionTest(unittest.TestCase):
    def test_minimums_are_filled_first(self) -> None:
        counts = {Position.PITCHER: 12, Position.CATCHER: 1, Position.INFIELDER: 6, Position.OUTFIELDER: 5}
        chosen = Counter(rookie.position for rookie in run(team(counts)))
        self.assertGreaterEqual(
            counts[Position.PITCHER] + chosen[Position.PITCHER], draft.MIN_BY_POSITION[Position.PITCHER]
        )
        self.assertGreaterEqual(counts[Position.CATCHER] + chosen[Position.CATCHER], 2)

    def test_surplus_positions_are_not_chosen(self) -> None:
        counts = {Position.PITCHER: 20, Position.CATCHER: 4, Position.INFIELDER: 3, Position.OUTFIELDER: 3}
        chosen = Counter(rookie.position for rookie in run(team(counts)))
        self.assertEqual(chosen[Position.PITCHER], 0)

    def test_pitchers_get_pitcher_ratings(self) -> None:
        for rookie in run(team({Position.INFIELDER: 20})):
            expected = PitcherRatings if rookie.position.is_pitcher else BatterRatings
            self.assertIsInstance(rookie.ratings, expected)


class ForeignTest(unittest.TestCase):
    def foreign_count(self, target: DraftTeam) -> int:
        return sum(rookie.is_foreign for rookie in run(target))

    def test_adds_up_to_two_foreign_players_per_year(self) -> None:
        self.assertEqual(self.foreign_count(team(foreign=0, limit=None)), draft.MAX_FOREIGN_PER_DRAFT)
        self.assertEqual(self.foreign_count(team(foreign=3, limit=None)), 1)
        self.assertEqual(self.foreign_count(team(foreign=4, limit=None)), 0)

    def test_does_not_add_to_a_full_roster_quota(self) -> None:
        self.assertEqual(self.foreign_count(team(foreign=2, limit=2)), 0)
        # 分岐元が枠を超えていても、さらには足さない
        self.assertEqual(self.foreign_count(team(foreign=6, limit=3)), 0)

    def test_small_quota_caps_the_target(self) -> None:
        self.assertEqual(self.foreign_count(team(foreign=0, limit=1)), 1)

    def test_foreign_players_are_never_catchers_and_carry_nationality(self) -> None:
        counts = {Position.PITCHER: 15, Position.CATCHER: 0, Position.INFIELDER: 6, Position.OUTFIELDER: 5}
        for seed in range(20):
            for rookie in run(team(counts, limit=None), seed):
                if rookie.is_foreign:
                    self.assertIsNot(rookie.position, Position.CATCHER)
                    self.assertTrue(rookie.profile.is_foreign_player)
                    self.assertEqual(rookie.profile.nationality, rookie.profile.birthplace)
                    self.assertEqual((rookie.profile.high_school, rookie.profile.university), ("", ""))
                else:
                    self.assertFalse(rookie.profile.is_foreign_player)
                    self.assertEqual(rookie.profile.nationality, "")


class NumberTest(unittest.TestCase):
    def test_numbers_avoid_current_roster_and_each_other(self) -> None:
        taken = frozenset(range(1, 99))
        for rookie in run(team(numbers=taken)):
            self.assertNotIn(rookie.number, taken)
        numbers = [rookie.number for rookie in run(team(), seed=5)]
        self.assertEqual(len(numbers), len(set(numbers)))

    def test_falls_back_to_three_digits_when_one_to_ninety_nine_are_full(self) -> None:
        for rookie in run(team(numbers=frozenset(range(1, 100)))):
            self.assertTrue(100 <= rookie.number <= 999)


class ProfileTest(unittest.TestCase):
    def test_age_follows_the_route(self) -> None:
        as_of = date(YEAR + 1, 4, 1)
        seen: set[DraftRoute] = set()
        for seed in range(60):
            for rookie in run(team(), seed):
                age = rookie.profile.age(as_of)
                seen.add(rookie.route)
                self.assertEqual(rookie.profile.debut_year, YEAR + 1)
                if rookie.route is DraftRoute.HIGH_SCHOOL:
                    self.assertEqual(age, 18)
                elif rookie.route is DraftRoute.UNIVERSITY:
                    self.assertEqual(age, 22)
                elif rookie.route is DraftRoute.CORPORATE:
                    self.assertTrue(age is not None and 23 <= age <= 26)
                else:
                    self.assertTrue(age is not None and 25 <= age <= 31)
        self.assertEqual(seen, set(DraftRoute))

    def test_domestic_routes_follow_the_weights(self) -> None:
        counts = Counter(rookie.route for seed in range(150) for rookie in run(team(), seed))
        domestic = counts[DraftRoute.HIGH_SCHOOL] + counts[DraftRoute.UNIVERSITY] + counts[DraftRoute.CORPORATE]
        self.assertAlmostEqual(counts[DraftRoute.UNIVERSITY] / domestic, 0.5, delta=0.08)
        self.assertAlmostEqual(counts[DraftRoute.HIGH_SCHOOL] / domestic, 0.3, delta=0.08)

    def test_rookies_are_weaker_than_the_league_average_and_foreign_stronger(self) -> None:
        by_route: dict[DraftRoute, list[float]] = {route: [] for route in DraftRoute}
        for seed in range(80):
            for rookie in run(team(), seed):
                ratings = rookie.ratings
                value = ratings.pitching_value if isinstance(ratings, PitcherRatings) else ratings.batting_value
                by_route[rookie.route].append(value)
        means = {route: sum(values) / len(values) for route, values in by_route.items() if values}
        self.assertLess(means[DraftRoute.HIGH_SCHOOL], means[DraftRoute.UNIVERSITY])
        self.assertLess(means[DraftRoute.UNIVERSITY], means[DraftRoute.CORPORATE])
        self.assertLess(means[DraftRoute.CORPORATE], means[DraftRoute.FOREIGN])

    def test_route_labels_are_the_screen_names(self) -> None:
        self.assertEqual([route.label for route in DraftRoute], ["高校", "大学", "社会人", "外国人"])


class RouteMappingTest(unittest.TestCase):
    def test_every_amateur_path_has_a_route_with_the_same_label(self) -> None:
        """アマチュアの経路を足して、新人の経路を足し忘れると KeyError になる。ここで見張る。"""
        for path in AmateurPath:
            route = DraftRoute.from_amateur_path(path)
            self.assertEqual(route.label, path.value)
        self.assertIs(DraftRoute.from_amateur_path(None), DraftRoute.FOREIGN)


class NameTest(unittest.TestCase):
    def test_names_avoid_taken_ones_and_are_recorded(self) -> None:
        used: set[str] = set()
        first = run(team(), used=used)
        self.assertTrue({rookie.name for rookie in first} <= used)
        second = run(team(), used=used)
        self.assertFalse({rookie.name for rookie in first} & {rookie.name for rookie in second})

    def test_back_name_gets_an_initial_only_when_surname_is_shared(self) -> None:
        rookies = run(team(), seed=3)
        surnames = Counter(rookie.profile.back_name.split(".")[-1] for rookie in rookies)
        for rookie in rookies:
            shared = surnames[rookie.profile.back_name.split(".")[-1]] > 1
            self.assertEqual("." in rookie.profile.back_name, shared)
        # 現在の選手と重なる苗字にも頭文字が付く
        taken = rookies[0].profile.back_name.split(".")[-1]
        again = run(team(surnames={taken: 1}), seed=3, used=set())
        self.assertIn(".", again[0].profile.back_name)


class DeterminismTest(unittest.TestCase):
    def test_same_random_gives_same_class(self) -> None:
        self.assertEqual(run(team(), seed=9), run(team(), seed=9))
        self.assertNotEqual(run(team(), seed=9), run(team(), seed=10))


if __name__ == "__main__":
    unittest.main()
