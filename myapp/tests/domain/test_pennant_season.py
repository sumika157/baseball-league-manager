import datetime
import unittest

from myapp.domain.pennant.season import SeasonPhase, season_phase, world_today


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
