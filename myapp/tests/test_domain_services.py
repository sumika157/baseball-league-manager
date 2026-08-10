"""ランキングのドメインサービスの単体テスト。DB は使わない。"""

from unittest import TestCase

from myapp.domain import services
from myapp.domain.entities import Player
from myapp.domain.value_objects import (
    BattingLine,
    InningsPitched,
    JerseyNumber,
    PitchingLine,
    Position,
)


def _batter(name, number, **line) -> Player:
    return Player(
        name=name,
        number=JerseyNumber(number),
        position=Position.INFIELDER,
        id=number,
        batting=BattingLine(**line),
    )


def _pitcher(name, number, notation="9.0", **line) -> Player:
    return Player(
        name=name,
        number=JerseyNumber(number),
        position=Position.PITCHER,
        id=number,
        pitching=PitchingLine(innings=InningsPitched.from_notation(notation), **line),
    )


class BattingLeaderTest(TestCase):
    def test_sorted_by_ops(self):
        weak = _batter("弱", 1, at_bats=10, singles=1)
        strong = _batter("強", 2, at_bats=10, home_runs=4)

        result = services.leaders_by_ops([weak, strong])

        self.assertEqual([r.player.name for r in result], ["強", "弱"])
        self.assertEqual([r.rank for r in result], [1, 2])

    def test_players_without_at_bats_are_excluded(self):
        """打数0の選手が並ぶとランキングとして意味をなさない。"""
        played = _batter("出場", 1, at_bats=10, singles=3)
        benched = _batter("未出場", 2)

        result = services.leaders_by_ops([played, benched])

        self.assertEqual([r.player.name for r in result], ["出場"])

    def test_minimum_at_bats_can_be_raised(self):
        """規定打数を上げると少数打席の選手が除かれる。"""
        few = _batter("少打席", 1, at_bats=1, singles=1)
        many = _batter("規定到達", 2, at_bats=20, singles=6)

        result = services.leaders_by_ops([few, many], minimum_at_bats=10)

        self.assertEqual([r.player.name for r in result], ["規定到達"])

    def test_pitchers_are_not_included(self):
        batter = _batter("野手", 1, at_bats=10, singles=3)
        pitcher = _pitcher("投手", 11, strikeouts=5)

        result = services.leaders_by_ops([batter, pitcher])

        self.assertEqual([r.player.name for r in result], ["野手"])

    def test_ties_share_the_same_rank(self):
        a = _batter("あ", 1, at_bats=10, singles=3)
        b = _batter("い", 2, at_bats=10, singles=3)

        result = services.leaders_by_ops([a, b])

        self.assertEqual([r.rank for r in result], [1, 1])

    def test_limit_is_applied(self):
        players = [_batter(f"選手{i}", i, at_bats=10, singles=i) for i in range(1, 9)]
        self.assertEqual(len(services.leaders_by_ops(players, limit=3)), 3)

    def test_batting_average_ranking(self):
        a = _batter("三割", 1, at_bats=10, singles=3)
        b = _batter("五割", 2, at_bats=10, singles=5)

        result = services.leaders_by_batting_average([a, b])

        self.assertEqual([r.player.name for r in result], ["五割", "三割"])
        self.assertAlmostEqual(result[0].value, 0.5)

    def test_home_run_ranking_excludes_zero(self):
        a = _batter("打った", 1, at_bats=10, home_runs=2)
        b = _batter("打ってない", 2, at_bats=10, singles=1)

        result = services.leaders_by_home_runs([a, b])

        self.assertEqual([r.player.name for r in result], ["打った"])
        self.assertEqual(result[0].value, 2.0)


class PitchingLeaderTest(TestCase):
    def test_sorted_by_lowest_era(self):
        bad = _pitcher("炎上", 11, earned_runs=9)
        good = _pitcher("好投", 18, earned_runs=1)

        result = services.leaders_by_era([bad, good])

        self.assertEqual([r.player.name for r in result], ["好投", "炎上"])

    def test_pitchers_without_innings_are_excluded(self):
        """未登板は防御率0となり、除外しないと首位に立ってしまう。"""
        pitched = _pitcher("登板済", 11, earned_runs=3)
        never = _pitcher("未登板", 18, notation="0.0")

        result = services.leaders_by_era([pitched, never])

        self.assertEqual([r.player.name for r in result], ["登板済"])

    def test_batters_are_not_included(self):
        pitcher = _pitcher("投手", 11, earned_runs=1)
        batter = _batter("野手", 1, at_bats=10, singles=3)

        result = services.leaders_by_era([pitcher, batter])

        self.assertEqual([r.player.name for r in result], ["投手"])

    def test_strikeout_ranking_excludes_zero(self):
        a = _pitcher("奪三振王", 11, strikeouts=12)
        b = _pitcher("奪三振なし", 18, strikeouts=0)

        result = services.leaders_by_strikeouts([a, b])

        self.assertEqual([r.player.name for r in result], ["奪三振王"])

    def test_empty_input_returns_empty(self):
        self.assertEqual(services.leaders_by_era([]), [])
        self.assertEqual(services.leaders_by_ops([]), [])
