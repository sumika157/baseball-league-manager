import datetime
import unittest

from myapp.domain.pennant.season import (
    MAX_GAMES_PER_ADVANCE,
    MAX_SUMMARY_DAYS,
    SeasonPhase,
    is_final_season,
    ratings_year,
    season_phase,
    season_year,
    summary_period,
    world_today,
)
from myapp.domain.pennant.world import MAX_SEASONS_PER_WORLD

D = datetime.date


class SeasonPhaseTest(unittest.TestCase):
    def test_before_any_game_it_is_before_opening(self):
        """日程があってもなくても、試合がまだ無ければ開幕前。"""
        for next_fixture_on in (None, D(2026, 3, 27)):
            with self.subTest(next_fixture_on=next_fixture_on):
                self.assertIs(
                    season_phase(last_played_on=None, next_fixture_on=next_fixture_on), SeasonPhase.BEFORE_OPENING
                )

    def test_in_season_while_fixtures_remain_in_the_same_year(self):
        self.assertIs(
            season_phase(last_played_on=D(2026, 5, 14), next_fixture_on=D(2026, 5, 15)), SeasonPhase.IN_SEASON
        )
        self.assertIs(
            season_phase(last_played_on=D(2026, 12, 30), next_fixture_on=D(2026, 12, 31)), SeasonPhase.IN_SEASON
        )

    def test_finished_when_fixtures_run_out(self):
        self.assertIs(season_phase(last_played_on=D(2026, 10, 3), next_fixture_on=None), SeasonPhase.FINISHED)

    def test_the_next_years_fixtures_after_the_last_game_mean_before_opening(self):
        """締めた直後（前年の最終日が今日で、翌年の日程だけが残っている）は、シーズン中ではなく翌年の開幕前。"""
        self.assertIs(
            season_phase(last_played_on=D(2026, 10, 3), next_fixture_on=D(2027, 3, 26)), SeasonPhase.BEFORE_OPENING
        )

    def test_the_value_is_the_label(self):
        self.assertEqual(SeasonPhase.IN_SEASON.value, "シーズン中")


class SeasonYearTest(unittest.TestCase):
    def test_it_is_the_year_of_the_next_fixture(self):
        year = season_year(next_fixture_on=D(2027, 3, 26), last_played_on=D(2026, 10, 3), start_year=2026)

        self.assertEqual(year, 2027)

    def test_without_fixtures_it_is_the_year_of_the_last_game(self):
        self.assertEqual(season_year(next_fixture_on=None, last_played_on=D(2026, 10, 3), start_year=2026), 2026)

    def test_without_any_game_it_is_the_start_year(self):
        self.assertEqual(season_year(next_fixture_on=None, last_played_on=None, start_year=2026), 2026)

    def test_it_follows_the_year_the_ratings_are_drawn_from(self):
        """能力を引く年と別の規則を作らない（画面の年と試合で使う能力の年が食い違わない）。"""
        for next_fixture_on, last_played_on in (
            (D(2027, 3, 26), D(2026, 10, 3)),
            (None, D(2026, 10, 3)),
            (D(2026, 4, 3), None),
            (None, None),
        ):
            with self.subTest(next=next_fixture_on, last=last_played_on):
                self.assertEqual(
                    season_year(next_fixture_on=next_fixture_on, last_played_on=last_played_on, start_year=2026),
                    ratings_year(next_game_on=next_fixture_on, last_played_on=last_played_on, start_year=2026),
                )


class FinalSeasonTest(unittest.TestCase):
    def test_the_limit_is_ten_seasons(self):
        self.assertEqual(MAX_SEASONS_PER_WORLD, 10)

    def test_the_tenth_season_is_the_final_one(self):
        """開幕年が1シーズン目。2026 開幕なら 2035 年が10シーズン目で、締められない。"""
        self.assertFalse(is_final_season(current_year=2026, start_year=2026))
        self.assertFalse(is_final_season(current_year=2034, start_year=2026))
        self.assertTrue(is_final_season(current_year=2035, start_year=2026))


class WorldTodayTest(unittest.TestCase):
    def test_it_is_the_last_played_day(self):
        self.assertEqual(world_today(2026, datetime.date(2026, 5, 14)), datetime.date(2026, 5, 14))

    def test_before_the_first_game_it_is_the_opening_day(self):
        today = world_today(2026, None)
        self.assertEqual(today.year, 2026)
        self.assertEqual(today.weekday(), 4, "開幕日は金曜")


class SummaryPeriodTest(unittest.TestCase):
    TODAY = datetime.date(2026, 6, 30)

    def test_the_period_is_after_since_through_today(self):
        self.assertEqual(
            summary_period(datetime.date(2026, 6, 20), self.TODAY), (datetime.date(2026, 6, 20), self.TODAY)
        )

    def test_a_long_period_is_rounded_to_a_month(self):
        start, end = summary_period(datetime.date(2026, 3, 1), self.TODAY)

        self.assertEqual((end - start).days, MAX_SUMMARY_DAYS)

    def test_a_missing_since_or_a_world_without_games_shows_nothing(self):
        self.assertIsNone(summary_period(None, self.TODAY))
        self.assertIsNone(summary_period(datetime.date(2026, 6, 1), None))

    def test_since_today_or_later_shows_nothing(self):
        """今日と同じ日や未来の日は、エラーにせずまとめを出さない。"""
        self.assertIsNone(summary_period(self.TODAY, self.TODAY))
        self.assertIsNone(summary_period(self.TODAY + datetime.timedelta(days=1), self.TODAY))

    def test_the_limit_of_one_advance_fits_a_week_of_eight_leagues(self):
        """8リーグの1週間（138〜144試合）が収まる。"""
        self.assertGreaterEqual(MAX_GAMES_PER_ADVANCE, 144)
