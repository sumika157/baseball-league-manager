import datetime
import unittest

from myapp.domain.pennant.season import (
    MAX_GAMES_PER_ADVANCE,
    MAX_SUMMARY_DAYS,
    SeasonPhase,
    season_phase,
    summary_period,
    world_today,
)


class SeasonPhaseTest(unittest.TestCase):
    def test_before_any_game_it_is_before_opening(self):
        """日程があってもなくても、試合がまだ無ければ開幕前。"""
        for pending in (False, True):
            with self.subTest(fixtures_pending=pending):
                self.assertIs(season_phase(has_played=False, fixtures_pending=pending), SeasonPhase.BEFORE_OPENING)

    def test_in_season_while_fixtures_remain(self):
        self.assertIs(season_phase(has_played=True, fixtures_pending=True), SeasonPhase.IN_SEASON)

    def test_finished_when_fixtures_run_out(self):
        self.assertIs(season_phase(has_played=True, fixtures_pending=False), SeasonPhase.FINISHED)

    def test_the_value_is_the_label(self):
        self.assertEqual(SeasonPhase.IN_SEASON.value, "シーズン中")


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
