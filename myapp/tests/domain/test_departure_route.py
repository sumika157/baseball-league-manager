"""退団の経路（`DepartureRoute`・`Stint.left_via`）と戦力外（`Team.release_player`）。Django も DB も使わない。"""

from unittest import TestCase

from myapp.domain.entities import Stint, Team
from myapp.domain.exceptions import InvalidStint
from myapp.domain.value_objects import DepartureRoute, JerseyNumber, Position, RosterLimits


class DepartureRouteTest(TestCase):
    def test_labels_round_trip(self):
        for route in DepartureRoute:
            self.assertIs(DepartureRoute.from_label(route.label), route)
        self.assertEqual([route.label for route in DepartureRoute], ["引退", "戦力外"])

    def test_unknown_label_is_rejected(self):
        with self.assertRaises(InvalidStint):
            DepartureRoute.from_label("トレード")


class StintLeftViaTest(TestCase):
    def test_an_open_stint_cannot_have_a_departure_route(self):
        with self.assertRaises(InvalidStint):
            Stint(team_id=1, number=JerseyNumber("10"), from_year=2026, left_via=DepartureRoute.RELEASED)

    def test_a_closed_stint_may_have_one_or_be_unknown(self):
        closed = Stint(
            team_id=1, number=JerseyNumber("10"), from_year=2024, to_year=2026, left_via=DepartureRoute.RETIRED
        )
        self.assertIs(closed.left_via, DepartureRoute.RETIRED)
        self.assertIsNone(Stint(team_id=1, number=JerseyNumber("10"), from_year=2024, to_year=2026).left_via)

    def test_close_records_the_route(self):
        stint = Stint(team_id=1, number=JerseyNumber("10"), from_year=2024)
        stint.close(2026, DepartureRoute.RELEASED)
        self.assertEqual((stint.to_year, stint.left_via), (2026, DepartureRoute.RELEASED))

    def test_close_without_a_route_leaves_it_unknown(self):
        stint = Stint(team_id=1, number=JerseyNumber("10"), from_year=2024)
        stint.close(2026)
        self.assertIsNone(stint.left_via)


class ReleasePlayerTest(TestCase):
    def setUp(self):
        self.team = Team(name="テストチーム", id=1, league_id=1)
        self.player = self.team.add_player(
            "山田", JerseyNumber("10"), Position.INFIELDER, from_year=2024, limits=RosterLimits.UNLIMITED
        )

    def test_release_closes_the_stint_as_released(self):
        self.team.release_player(self.player, year=2026)

        stint = self.player.career[0]
        self.assertEqual((stint.to_year, stint.left_via), (2026, DepartureRoute.RELEASED))
        self.assertFalse(self.player.is_active)
        self.assertIsNone(self.team.current_stint(self.player))

    def test_the_number_becomes_free(self):
        self.team.release_player(self.player, year=2026)
        newcomer = self.team.add_player(
            "田中", JerseyNumber("10"), Position.OUTFIELDER, from_year=2027, limits=RosterLimits.UNLIMITED
        )
        self.assertEqual(newcomer.number, JerseyNumber("10"))

    def test_a_released_captain_is_removed(self):
        self.team.appoint_captain(self.player, 2025)
        self.team.release_player(self.player, year=2026)
        self.assertIsNone(self.team.current_captain)
        self.assertEqual(self.player.captaincies[0].to_year, 2026)

    def test_the_year_is_required(self):
        with self.assertRaises(TypeError):
            self.team.release_player(self.player)

    def test_a_player_not_on_the_team_cannot_be_released(self):
        self.team.release_player(self.player, year=2026)
        with self.assertRaises(InvalidStint):
            self.team.release_player(self.player, year=2027)

    def test_retire_records_the_route_when_given(self):
        self.team.retire_player(self.player, 2026, left_via=DepartureRoute.RETIRED)
        self.assertIs(self.player.career[0].left_via, DepartureRoute.RETIRED)

    def test_retire_without_a_route_stays_unknown(self):
        """実データの退団（管理画面・移籍）は経路を記録しない（不明のまま）。"""
        self.team.retire_player(self.player, 2026)
        self.assertIsNone(self.player.career[0].left_via)
