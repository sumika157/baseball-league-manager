"""主将（キャプテン）在任の単体テスト。Django も DB も使わない。"""

from unittest import TestCase

from myapp.domain.entities import Captaincy, Team
from myapp.domain.exceptions import (
    DuplicateCaptain,
    InvalidCaptaincy,
    PlayerNotEligibleForCaptaincy,
)
from myapp.domain.value_objects import JerseyNumber, Position


def _captaincy(from_year, to_year=None, team_id=1) -> Captaincy:
    return Captaincy(team_id=team_id, from_year=from_year, to_year=to_year)


class CaptaincyTest(TestCase):
    def test_open_captaincy_means_currently_captain(self):
        self.assertTrue(_captaincy(2026).is_current)
        self.assertFalse(_captaincy(2024, 2025).is_current)

    def test_leaving_before_joining_is_rejected(self):
        with self.assertRaises(InvalidCaptaincy):
            _captaincy(2026, 2025)

    def test_close_sets_the_leaving_year(self):
        captaincy = _captaincy(2024)
        captaincy.close(2026)

        self.assertEqual(captaincy.to_year, 2026)
        self.assertFalse(captaincy.is_current)

    def test_close_before_joining_is_rejected(self):
        with self.assertRaises(InvalidCaptaincy):
            _captaincy(2026).close(2025)

    def test_overlap(self):
        self.assertTrue(_captaincy(2024, 2026).overlaps(_captaincy(2025, 2027)))
        self.assertTrue(_captaincy(2024, 2026).overlaps(_captaincy(2026, 2028)))
        self.assertFalse(_captaincy(2024, 2025).overlaps(_captaincy(2026, 2027)))


class TeamCaptaincyTest(TestCase):
    def setUp(self):
        self.team = Team(name="テストチーム", id=1, league_id=1)

    def test_appointing_a_captain_opens_a_captaincy(self):
        player = self.team.add_player("山田", JerseyNumber(10), Position.INFIELDER, from_year=2026)

        self.team.appoint_captain(player, 2026)

        self.assertEqual(len(player.captaincies), 1)
        self.assertTrue(player.captaincies[0].is_current)
        self.assertIs(self.team.current_captain, player)

    def test_appointing_a_second_captain_is_rejected(self):
        first = self.team.add_player("山田", JerseyNumber(10), Position.INFIELDER, from_year=2026)
        second = self.team.add_player("田中", JerseyNumber(11), Position.OUTFIELDER, from_year=2026)
        self.team.appoint_captain(first, 2026)

        with self.assertRaises(DuplicateCaptain):
            self.team.appoint_captain(second, 2026)

    def test_appointing_a_player_not_on_the_roster_is_rejected(self):
        player = self.team.add_player("山田", JerseyNumber(10), Position.INFIELDER, from_year=2024)
        self.team.retire_player(player, 2025)

        with self.assertRaises(PlayerNotEligibleForCaptaincy):
            self.team.appoint_captain(player, 2026)

    def test_appointing_the_current_captain_again_is_a_no_op(self):
        player = self.team.add_player("山田", JerseyNumber(10), Position.INFIELDER, from_year=2026)
        self.team.appoint_captain(player, 2026)

        self.team.appoint_captain(player, 2026)

        self.assertEqual(len(player.captaincies), 1)

    def test_removing_the_captain_closes_the_captaincy(self):
        player = self.team.add_player("山田", JerseyNumber(10), Position.INFIELDER, from_year=2026)
        self.team.appoint_captain(player, 2026)

        self.team.remove_captain(player, 2027)

        self.assertFalse(player.captaincies[0].is_current)
        self.assertEqual(player.captaincies[0].to_year, 2027)
        self.assertIsNone(self.team.current_captain)

    def test_removing_a_non_captain_is_a_no_op(self):
        player = self.team.add_player("山田", JerseyNumber(10), Position.INFIELDER, from_year=2026)

        self.team.remove_captain(player, 2027)  # 例外にならない

        self.assertEqual(player.captaincies, [])

    def test_retiring_the_captain_also_closes_the_captaincy(self):
        """在籍していないのに主将、という両立しない状態を残さない。"""
        player = self.team.add_player("山田", JerseyNumber(10), Position.INFIELDER, from_year=2024)
        self.team.appoint_captain(player, 2024)

        self.team.retire_player(player, 2026)

        self.assertIsNone(self.team.current_captain)
        self.assertFalse(player.captaincies[0].is_current)

    def test_reappointing_a_different_captain_after_removal(self):
        first = self.team.add_player("山田", JerseyNumber(10), Position.INFIELDER, from_year=2026)
        second = self.team.add_player("田中", JerseyNumber(11), Position.OUTFIELDER, from_year=2026)
        self.team.appoint_captain(first, 2026)
        self.team.remove_captain(first, 2027)

        self.team.appoint_captain(second, 2027)

        self.assertIs(self.team.current_captain, second)
