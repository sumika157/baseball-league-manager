"""在籍（経歴）の単体テスト。Django も DB も使わない。"""

from unittest import TestCase

from myapp.domain.entities import Stint, Team
from myapp.domain.exceptions import DuplicateJerseyNumber, InvalidStint
from myapp.domain.value_objects import JerseyNumber, Position


def _stint(from_year, to_year=None, number=10, team_id=1) -> Stint:
    return Stint(
        team_id=team_id, number=JerseyNumber(number),
        from_year=from_year, to_year=to_year,
    )


class StintTest(TestCase):
    def test_open_stint_means_currently_belonging(self):
        self.assertTrue(_stint(2026).is_current)
        self.assertFalse(_stint(2024, 2025).is_current)

    def test_covers_the_years_in_range(self):
        stint = _stint(2024, 2026)

        self.assertFalse(stint.covers(2023))
        self.assertTrue(stint.covers(2024))
        self.assertTrue(stint.covers(2026))
        self.assertFalse(stint.covers(2027))

    def test_open_stint_covers_future_years(self):
        self.assertTrue(_stint(2024).covers(2099))

    def test_leaving_before_joining_is_rejected(self):
        with self.assertRaises(InvalidStint):
            _stint(2026, 2025)

    def test_close_sets_the_leaving_year(self):
        stint = _stint(2024)
        stint.close(2026)

        self.assertEqual(stint.to_year, 2026)
        self.assertFalse(stint.is_current)

    def test_close_before_joining_is_rejected(self):
        with self.assertRaises(InvalidStint):
            _stint(2026).close(2025)

    def test_overlap(self):
        self.assertTrue(_stint(2024, 2026).overlaps(_stint(2025, 2027)))
        self.assertTrue(_stint(2024, 2026).overlaps(_stint(2026, 2028)))
        self.assertFalse(_stint(2024, 2025).overlaps(_stint(2026, 2027)))

    def test_open_stints_always_overlap_later_periods(self):
        self.assertTrue(_stint(2020).overlaps(_stint(2026, 2027)))


class RosterWithStintsTest(TestCase):
    def setUp(self):
        self.team = Team(name='テストチーム', id=1, league_id=1)

    def test_adding_a_player_opens_a_stint(self):
        player = self.team.add_player(
            '山田', JerseyNumber(10), Position.INFIELDER, from_year=2026
        )

        self.assertEqual(len(player.career), 1)
        self.assertTrue(player.career[0].is_current)
        self.assertEqual(player.career[0].from_year, 2026)

    def test_number_in_use_is_rejected(self):
        self.team.add_player('山田', JerseyNumber(10), Position.INFIELDER, from_year=2026)

        with self.assertRaises(DuplicateJerseyNumber):
            self.team.add_player('田中', JerseyNumber(10), Position.OUTFIELDER, from_year=2026)

    def test_number_is_free_after_the_stint_closes(self):
        """過去に同じ番号を付けた選手がいても、期間が重ならなければ使える。"""
        first = self.team.add_player(
            '山田', JerseyNumber(10), Position.INFIELDER, from_year=2024
        )
        self.team.retire_player(first, 2025)

        second = self.team.add_player(
            '田中', JerseyNumber(10), Position.OUTFIELDER, from_year=2026
        )

        self.assertEqual(second.number, JerseyNumber(10))
        self.assertEqual([p.name for p in self.team.active_players], ['田中'])

    def test_changing_the_number_updates_the_stint(self):
        player = self.team.add_player(
            '山田', JerseyNumber(10), Position.INFIELDER, from_year=2026
        )

        self.team.change_player_number(player, JerseyNumber(11))

        self.assertEqual(player.number, JerseyNumber(11))
        self.assertEqual(self.team.current_stint(player).number, JerseyNumber(11))

    def test_retiring_closes_the_stint(self):
        player = self.team.add_player(
            '山田', JerseyNumber(10), Position.INFIELDER, from_year=2024
        )

        self.team.retire_player(player, 2026)

        self.assertFalse(player.is_active)
        self.assertEqual(player.career[0].to_year, 2026)
        self.assertIsNone(self.team.current_stint(player))

    def test_career_keeps_past_teams(self):
        """移籍しても過去の在籍が残る。"""
        player = self.team.add_player(
            '山田', JerseyNumber(10), Position.INFIELDER, from_year=2024
        )
        self.team.retire_player(player, 2025)
        player.career.append(Stint(
            team_id=2, team_name='移籍先', number=JerseyNumber(7), from_year=2026
        ))

        self.assertEqual(len(player.career), 2)
        self.assertEqual(
            [(s.team_id, s.from_year, s.to_year) for s in player.career],
            [(1, 2024, 2025), (2, 2026, None)],
        )
