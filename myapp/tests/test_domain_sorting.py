"""並べ替えの単体テスト。Django も DB も使わない。"""

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
        name=name, number=JerseyNumber(number), position=Position.INFIELDER,
        id=number, batting=BattingLine(**line),
    )


def _pitcher(name, number, notation='9.0', **line) -> Player:
    return Player(
        name=name, number=JerseyNumber(number), position=Position.PITCHER, id=number,
        pitching=PitchingLine(innings=InningsPitched.from_notation(notation), **line),
    )


class BatterSortTest(TestCase):
    def setUp(self):
        self.players = [
            _batter('少', 1, at_bats=20, singles=4, home_runs=1),
            _batter('多', 2, at_bats=20, singles=2, home_runs=5),
        ]

    def test_default_is_ops_descending(self):
        players, key, desc = services.sort_batters(self.players)

        self.assertEqual(key, 'ops')
        self.assertTrue(desc)
        self.assertEqual([p.name for p in players], ['多', '少'])

    def test_sort_by_home_runs(self):
        players, key, desc = services.sort_batters(self.players, 'home_runs')

        self.assertEqual(key, 'home_runs')
        self.assertTrue(desc, '本塁打は多い順が既定')
        self.assertEqual([p.name for p in players], ['多', '少'])

    def test_direction_can_be_reversed(self):
        players, _, desc = services.sort_batters(self.players, 'home_runs', False)

        self.assertFalse(desc)
        self.assertEqual([p.name for p in players], ['少', '多'])

    def test_name_defaults_to_ascending(self):
        _, key, desc = services.sort_batters(self.players, 'name')
        self.assertEqual(key, 'name')
        self.assertFalse(desc, '名前は昇順が既定')

    def test_unknown_key_falls_back_to_default(self):
        """キーは URL 由来なので、不正でもエラーにせず既定に落とす。"""
        players, key, _ = services.sort_batters(self.players, 'drop table')

        self.assertEqual(key, 'ops')
        self.assertEqual([p.name for p in players], ['多', '少'])

    def test_ties_are_broken_by_jersey_number(self):
        same = [
            _batter('B', 20, at_bats=10, singles=3),
            _batter('A', 10, at_bats=10, singles=3),
        ]
        players, _, _ = services.sort_batters(same, 'average')
        self.assertEqual([p.number.value for p in players], [10, 20])


class PitcherSortTest(TestCase):
    def setUp(self):
        self.players = [
            _pitcher('炎上', 11, earned_runs=9, strikeouts=3),
            _pitcher('好投', 18, earned_runs=1, strikeouts=12),
        ]

    def test_default_is_era_ascending(self):
        players, key, desc = services.sort_pitchers(self.players)

        self.assertEqual(key, 'era')
        self.assertFalse(desc)
        self.assertEqual([p.name for p in players], ['好投', '炎上'])

    def test_sort_by_strikeouts_descending(self):
        players, key, desc = services.sort_pitchers(self.players, 'strikeouts')

        self.assertEqual(key, 'strikeouts')
        self.assertTrue(desc)
        self.assertEqual([p.name for p in players], ['好投', '炎上'])

    def test_unpitched_stays_last_when_ascending(self):
        players = self.players + [_pitcher('未登板', 20, notation='0.0')]
        result, _, _ = services.sort_pitchers(players, 'era')

        self.assertEqual(result[-1].name, '未登板')

    def test_unpitched_stays_last_when_descending(self):
        """向きを反転しても、未登板が先頭に来ないこと。"""
        players = self.players + [_pitcher('未登板', 20, notation='0.0')]
        result, _, _ = services.sort_pitchers(players, 'era', True)

        self.assertEqual(result[-1].name, '未登板')
        self.assertEqual(result[0].name, '炎上')

    def test_unpitched_last_applies_to_whip_too(self):
        players = self.players + [_pitcher('未登板', 20, notation='0.0')]
        result, _, _ = services.sort_pitchers(players, 'whip')

        self.assertEqual(result[-1].name, '未登板')

    def test_counting_stats_do_not_exclude_unpitched(self):
        """勝利数のような実数は未登板でも0として並べてよい。"""
        players = self.players + [_pitcher('未登板', 20, notation='0.0')]
        result, _, _ = services.sort_pitchers(players, 'wins')

        self.assertEqual(len(result), 3)

    def test_unknown_key_falls_back_to_default(self):
        _, key, _ = services.sort_pitchers(self.players, 'nope')
        self.assertEqual(key, 'era')


class SortKeyCatalogTest(TestCase):
    def test_every_key_declares_a_default_direction(self):
        for name, table in (
            ('BATTER', services.BATTER_SORT_KEYS),
            ('PITCHER', services.PITCHER_SORT_KEYS),
        ):
            for key, value in table.items():
                with self.subTest(table=name, key=key):
                    self.assertEqual(len(value), 2)
                    self.assertTrue(callable(value[0]))
                    self.assertIsInstance(value[1], bool)

    def test_defaults_exist_in_the_catalog(self):
        self.assertIn(services.DEFAULT_BATTER_SORT, services.BATTER_SORT_KEYS)
        self.assertIn(services.DEFAULT_PITCHER_SORT, services.PITCHER_SORT_KEYS)
