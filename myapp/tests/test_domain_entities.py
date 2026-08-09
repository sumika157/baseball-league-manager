"""集約（Team）とエンティティ（Player）の単体テスト。DB は使わない。"""

from unittest import TestCase

from myapp.domain.entities import Team
from myapp.domain.exceptions import (
    DomainError,
    DuplicateJerseyNumber,
    PlayerNotFound,
)
from myapp.domain.value_objects import (
    BattingLine,
    InningsPitched,
    JerseyNumber,
    PitchingLine,
    Position,
)


def _team() -> Team:
    return Team(id=1, league_id=1, name='テストチーム')


class TeamRosterTest(TestCase):
    def test_add_player(self):
        team = _team()
        player = team.add_player('山田', JerseyNumber(10), Position.INFIELDER)

        self.assertEqual(player.name, '山田')
        self.assertEqual(team.active_players, [player])

    def test_duplicate_number_is_rejected(self):
        """背番号の一意性は集約が保証する。"""
        team = _team()
        team.add_player('山田', JerseyNumber(10), Position.INFIELDER)

        with self.assertRaises(DuplicateJerseyNumber):
            team.add_player('田中', JerseyNumber(10), Position.OUTFIELDER)

    def test_retired_player_frees_the_number(self):
        team = _team()
        retired = team.add_player('山田', JerseyNumber(10), Position.INFIELDER)
        retired.retire()

        # 引退した選手の背番号は再利用できる
        team.add_player('田中', JerseyNumber(10), Position.OUTFIELDER)
        self.assertEqual(len(team.active_players), 1)

    def test_empty_name_is_rejected(self):
        team = _team()
        with self.assertRaises(DomainError):
            team.add_player('   ', JerseyNumber(10), Position.INFIELDER)

    def test_change_number_to_free_one(self):
        team = _team()
        player = team.add_player('山田', JerseyNumber(10), Position.INFIELDER)

        team.change_player_number(player, JerseyNumber(11))
        self.assertEqual(player.number, JerseyNumber(11))

    def test_change_number_to_taken_one_is_rejected(self):
        team = _team()
        team.add_player('山田', JerseyNumber(10), Position.INFIELDER)
        tanaka = team.add_player('田中', JerseyNumber(11), Position.OUTFIELDER)

        with self.assertRaises(DuplicateJerseyNumber):
            team.change_player_number(tanaka, JerseyNumber(10))

    def test_keeping_the_same_number_is_allowed(self):
        """自分自身の背番号は重複とみなさない。"""
        team = _team()
        player = team.add_player('山田', JerseyNumber(10), Position.INFIELDER)

        team.change_player_number(player, JerseyNumber(10))
        self.assertEqual(player.number, JerseyNumber(10))

    def test_find_missing_player(self):
        with self.assertRaises(PlayerNotFound):
            _team().find_player(999)


class RosterOrderingTest(TestCase):
    def test_batters_are_sorted_by_ops(self):
        team = _team()
        weak = team.add_player('弱', JerseyNumber(1), Position.INFIELDER)
        strong = team.add_player('強', JerseyNumber(2), Position.OUTFIELDER)

        weak.record_batting(BattingLine(at_bats=10, singles=1))
        strong.record_batting(BattingLine(at_bats=10, home_runs=4))

        self.assertEqual([p.name for p in team.batters_by_ops()], ['強', '弱'])

    def test_pitchers_are_sorted_by_era(self):
        team = _team()
        bad = team.add_player('炎上', JerseyNumber(11), Position.PITCHER)
        good = team.add_player('好投', JerseyNumber(18), Position.PITCHER)

        bad.record_pitching(
            PitchingLine(innings=InningsPitched.from_notation('9.0'), earned_runs=9)
        )
        good.record_pitching(
            PitchingLine(innings=InningsPitched.from_notation('9.0'), earned_runs=1)
        )

        self.assertEqual([p.name for p in team.pitchers_by_era()], ['好投', '炎上'])

    def test_pitchers_without_innings_go_last(self):
        """未登板は防御率0になるため、不当に上位へ来ないこと。"""
        team = _team()
        team.add_player('未登板', JerseyNumber(11), Position.PITCHER)
        pitched = team.add_player('登板済', JerseyNumber(18), Position.PITCHER)
        pitched.record_pitching(
            PitchingLine(innings=InningsPitched.from_notation('9.0'), earned_runs=3)
        )

        self.assertEqual([p.name for p in team.pitchers_by_era()], ['登板済', '未登板'])

    def test_batters_and_pitchers_are_separated(self):
        team = _team()
        team.add_player('野手', JerseyNumber(1), Position.INFIELDER)
        team.add_player('投手', JerseyNumber(11), Position.PITCHER)
        team.add_player('DH', JerseyNumber(2), Position.DESIGNATED_HITTER)

        self.assertEqual(
            sorted(p.name for p in team.batters_by_ops()), ['DH', '野手']
        )
        self.assertEqual([p.name for p in team.pitchers_by_era()], ['投手'])


class PlayerTest(TestCase):
    def test_rename_trims_whitespace(self):
        team = _team()
        player = team.add_player('山田', JerseyNumber(10), Position.INFIELDER)

        player.rename('  山田太郎  ')
        self.assertEqual(player.name, '山田太郎')

    def test_rename_to_empty_is_rejected(self):
        team = _team()
        player = team.add_player('山田', JerseyNumber(10), Position.INFIELDER)

        with self.assertRaises(DomainError):
            player.rename('')

    def test_position_change_keeps_both_stat_lines(self):
        """転向しても成績行が欠落しない（旧実装で起きていた欠損の防止）。"""
        team = _team()
        player = team.add_player('二刀流', JerseyNumber(17), Position.PITCHER)
        player.record_batting(BattingLine(at_bats=10, home_runs=3))

        player.change_position(Position.OUTFIELDER)

        self.assertFalse(player.is_pitcher)
        self.assertEqual(player.batting.home_runs, 3)
        self.assertIsNotNone(player.pitching)
