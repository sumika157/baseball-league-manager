"""能力を引く年の規則（`ratings_year`）。Django も DB も使わない。"""

from datetime import date
from unittest import TestCase

from myapp.domain.pennant.season import ratings_year


class RatingsYearTest(TestCase):
    def test_the_year_of_the_next_game_comes_first(self):
        year = ratings_year(next_game_on=date(2027, 3, 29), last_played_on=date(2026, 10, 1), start_year=2026)

        self.assertEqual(year, 2027)

    def test_without_a_schedule_it_is_the_year_of_the_last_game(self):
        year = ratings_year(next_game_on=None, last_played_on=date(2026, 10, 1), start_year=2026)

        self.assertEqual(year, 2026)

    def test_before_any_game_it_is_the_opening_year(self):
        self.assertEqual(ratings_year(next_game_on=None, last_played_on=None, start_year=2031), 2031)
