"""仮想の選手の名前・プロフィールの生成（`domain/virtual_players/`）。DB 不要。"""

from __future__ import annotations

import random
import unittest
from datetime import date

from myapp.domain.value_objects import Handedness, Position, Profile
from myapp.domain.virtual_players import generator, pools
from myapp.domain.virtual_players.generator import AmateurPath


class OnlyRandom:
    """`random()` しか持たない乱数源。他のメソッド（`choice` など）を呼ぶと AttributeError で落ちる。"""

    def __init__(self, seed: int) -> None:
        self._rng = random.Random(seed)

    def random(self) -> float:
        return self._rng.random()


class RandomOnlyTest(unittest.TestCase):
    """生成は `random()` だけを使う（版をまたいで同じ名前になるため）。"""

    def test_every_generator_runs_on_random_only(self) -> None:
        rng = OnlyRandom(1)
        used: set[str] = set()
        generator.japanese_name(rng, used)
        generator.foreign_name(rng, used)
        generator.prefecture(rng)
        generator.birth_date_for(rng, age=20, as_of=date(2027, 4, 1))
        generator.amateur_career(rng, is_foreign=False, birthplace="北海道")
        generator.amateur_career(rng, is_foreign=True, birthplace="アメリカ合衆国")
        for position in Position:
            generator.physique(rng, position)
            generator.handedness(rng, position)

    def test_same_random_gives_same_names(self) -> None:
        def names(seed: int) -> list[str]:
            rng, used = OnlyRandom(seed), set[str]()
            return [generator.japanese_name(rng, used).name for _ in range(30)]

        self.assertEqual(names(7), names(7))
        self.assertNotEqual(names(7), names(8))


class NameTest(unittest.TestCase):
    def test_names_do_not_repeat_and_are_recorded(self) -> None:
        rng, used = OnlyRandom(3), set[str]()
        names = [generator.japanese_name(rng, used).name for _ in range(300)]
        self.assertEqual(len(set(names)), 300)
        self.assertTrue(set(names) <= used)

    def test_taken_names_are_avoided(self) -> None:
        """同じ乱数でも、使用済みの名前は避ける。"""
        used: set[str] = set()
        first = generator.japanese_name(OnlyRandom(3), used).name
        again = generator.japanese_name(OnlyRandom(3), used).name
        self.assertNotEqual(first, again)

    def test_foreign_name_has_country_and_romaji(self) -> None:
        name = generator.foreign_name(OnlyRandom(5), set())
        self.assertIn("・", name.name)
        self.assertEqual(name.kana, name.name)
        self.assertIn(name.country, [group["country"] for group in pools.FOREIGN_GROUPS])
        self.assertTrue(name.surname_romaji.isupper() and name.given_romaji.isupper())

    def test_back_name_marks_shared_surname_only(self) -> None:
        self.assertEqual(generator.back_name("SATO", "KENTA", shares_surname=False), "SATO")
        self.assertEqual(generator.back_name("SATO", "KENTA", shares_surname=True), "K.SATO")


class ProfileTest(unittest.TestCase):
    def test_birth_date_matches_the_age_on_that_day(self) -> None:
        rng = OnlyRandom(11)
        as_of = date(2027, 4, 1)
        for age in (18, 22, 30):
            for _ in range(50):
                born = generator.birth_date_for(rng, age=age, as_of=as_of)
                self.assertEqual(Profile(birth_date=born).age(as_of), age)

    def test_amateur_career_follows_the_path(self) -> None:
        rng = OnlyRandom(13)

        def career(path: AmateurPath) -> generator.AmateurCareer:
            return generator.amateur_career(rng, is_foreign=False, birthplace="北海道", path=path)

        high = career(AmateurPath.HIGH_SCHOOL)
        self.assertEqual((high.university, high.corporate_team, high.debut_age), ("", "", 18))
        university = career(AmateurPath.UNIVERSITY)
        self.assertTrue(university.university and not university.corporate_team)
        self.assertEqual(university.debut_age, 22)
        corporate = career(AmateurPath.CORPORATE)
        self.assertTrue(corporate.corporate_team and 23 <= corporate.debut_age <= 26)

    def test_foreign_player_has_no_japanese_school(self) -> None:
        career = generator.amateur_career(OnlyRandom(2), is_foreign=True, birthplace="アメリカ合衆国")
        self.assertEqual(
            (career.high_school, career.university, career.corporate_team, career.path), ("", "", "", None)
        )

    def test_physique_stays_in_the_position_range(self) -> None:
        rng = OnlyRandom(17)
        for position, (heights, weights) in generator.PHYSIQUE_RANGES.items():
            for _ in range(40):
                height, weight = generator.physique(rng, position)
                self.assertTrue(heights[0] <= height <= heights[1])
                self.assertTrue(weights[0] <= weight <= weights[1])

    def test_catchers_throw_right_more_often(self) -> None:
        rng = OnlyRandom(19)
        catcher_rights = sum(generator.handedness(rng, Position.CATCHER)[0] is Handedness.RIGHT for _ in range(400))
        other_rights = sum(generator.handedness(rng, Position.OUTFIELDER)[0] is Handedness.RIGHT for _ in range(400))
        self.assertGreater(catcher_rights, other_rights)


class AllocationTest(unittest.TestCase):
    def test_position_ratios_are_keyed_by_position(self) -> None:
        self.assertEqual(set(generator.POSITION_RATIOS), set(Position))
        self.assertLessEqual(sum(generator.POSITION_RATIOS.values()), 1.0)

    def test_largest_remainder_keeps_the_total(self) -> None:
        for total in range(generator.MIN_ROSTER, generator.MAX_ROSTER + 1):
            allocation = generator.largest_remainder(total, generator.POSITION_RATIOS)
            self.assertEqual(sum(allocation.values()), total)


if __name__ == "__main__":
    unittest.main()
