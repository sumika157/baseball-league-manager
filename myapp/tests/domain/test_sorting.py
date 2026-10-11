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


class BatterSortTest(TestCase):
    def setUp(self):
        self.players = [
            _batter("少", 1, at_bats=20, singles=4, home_runs=1),
            _batter("多", 2, at_bats=20, singles=2, home_runs=5),
        ]

    def test_default_is_ops_descending(self):
        players, key, desc = services.sort_batters(self.players)

        self.assertEqual(key, "ops")
        self.assertTrue(desc)
        self.assertEqual([p.name for p in players], ["多", "少"])

    def test_sort_by_home_runs(self):
        players, key, desc = services.sort_batters(self.players, "home_runs")

        self.assertEqual(key, "home_runs")
        self.assertTrue(desc, "本塁打は多い順が既定")
        self.assertEqual([p.name for p in players], ["多", "少"])

    def test_direction_can_be_reversed(self):
        players, _, desc = services.sort_batters(self.players, "home_runs", False)

        self.assertFalse(desc)
        self.assertEqual([p.name for p in players], ["少", "多"])

    def test_name_defaults_to_ascending(self):
        _, key, desc = services.sort_batters(self.players, "name")
        self.assertEqual(key, "name")
        self.assertFalse(desc, "名前は昇順が既定")

    def test_unknown_key_falls_back_to_default(self):
        """キーは URL 由来なので、不正でもエラーにせず既定に落とす。"""
        players, key, _ = services.sort_batters(self.players, "drop table")

        self.assertEqual(key, "ops")
        self.assertEqual([p.name for p in players], ["多", "少"])

    def test_roster_default_is_jersey_number_ascending(self):
        """名簿の既定を渡すと、未指定でも不正なキーでも背番号の小さい順に落ちる。"""
        for key in (None, "drop table"):
            with self.subTest(key=key):
                players, used, desc = services.sort_batters(
                    self.players[::-1], key, default_key=services.DEFAULT_ROSTER_SORT
                )
                self.assertEqual(used, "number")
                self.assertFalse(desc)
                self.assertEqual([p.number.value for p in players], ["1", "2"])

    def test_jersey_number_order_is_double_zero_zero_then_numeric_not_text(self):
        """背番号順は 00 → 0 → 1 → 2 → 10 → 100。文字列順（「10」が「2」より前）にならない。"""
        players = [
            Player(
                name=f"選手{text}",
                number=JerseyNumber(text),
                position=Position.INFIELDER,
                id=index,
                batting=BattingLine(),
            )
            for index, text in enumerate(["100", "10", "2", "0", "00", "1"], start=1)
        ]
        ordered, _, _ = services.sort_batters(players, "number")
        self.assertEqual([p.number.value for p in ordered], ["00", "0", "1", "2", "10", "100"])

    def test_ties_are_broken_by_jersey_number(self):
        same = [
            _batter("B", 20, at_bats=10, singles=3),
            _batter("A", 10, at_bats=10, singles=3),
        ]
        players, _, _ = services.sort_batters(same, "average")
        self.assertEqual([p.number.value for p in players], ["10", "20"])


class PitcherSortTest(TestCase):
    def setUp(self):
        self.players = [
            _pitcher("炎上", 11, earned_runs=9, strikeouts=3),
            _pitcher("好投", 18, earned_runs=1, strikeouts=12),
        ]

    def test_default_is_era_ascending(self):
        players, key, desc = services.sort_pitchers(self.players)

        self.assertEqual(key, "era")
        self.assertFalse(desc)
        self.assertEqual([p.name for p in players], ["好投", "炎上"])

    def test_sort_by_strikeouts_descending(self):
        players, key, desc = services.sort_pitchers(self.players, "strikeouts")

        self.assertEqual(key, "strikeouts")
        self.assertTrue(desc)
        self.assertEqual([p.name for p in players], ["好投", "炎上"])

    def test_unpitched_stays_last_when_ascending(self):
        players = self.players + [_pitcher("未登板", 20, notation="0.0")]
        result, _, _ = services.sort_pitchers(players, "era")

        self.assertEqual(result[-1].name, "未登板")

    def test_unpitched_stays_last_when_descending(self):
        """向きを反転しても、未登板が先頭に来ないこと。"""
        players = self.players + [_pitcher("未登板", 20, notation="0.0")]
        result, _, _ = services.sort_pitchers(players, "era", True)

        self.assertEqual(result[-1].name, "未登板")
        self.assertEqual(result[0].name, "炎上")

    def test_unpitched_last_applies_to_whip_too(self):
        players = self.players + [_pitcher("未登板", 20, notation="0.0")]
        result, _, _ = services.sort_pitchers(players, "whip")

        self.assertEqual(result[-1].name, "未登板")

    def test_counting_stats_do_not_exclude_unpitched(self):
        """勝利数のような実数は未登板でも0として並べてよい。"""
        players = self.players + [_pitcher("未登板", 20, notation="0.0")]
        result, _, _ = services.sort_pitchers(players, "wins")

        self.assertEqual(len(result), 3)

    def test_unknown_key_falls_back_to_default(self):
        _, key, _ = services.sort_pitchers(self.players, "nope")
        self.assertEqual(key, "era")

    def test_roster_default_is_jersey_number_ascending(self):
        """防御率の良い投手が背番号で後ろにいても、名簿の既定なら背番号順になる。"""
        for key in (None, "nope"):
            with self.subTest(key=key):
                players, used, desc = services.sort_pitchers(
                    self.players, key, default_key=services.DEFAULT_ROSTER_SORT
                )
                self.assertEqual(used, "number")
                self.assertFalse(desc)
                self.assertEqual([p.name for p in players], ["炎上", "好投"])


class SortKeyCatalogTest(TestCase):
    def test_every_key_declares_a_default_direction(self):
        for name, table in (
            ("BATTER", services.BATTER_SORT_KEYS),
            ("PITCHER", services.PITCHER_SORT_KEYS),
        ):
            for key, value in table.items():
                with self.subTest(table=name, key=key):
                    self.assertEqual(len(value), 2)
                    self.assertTrue(callable(value[0]))
                    self.assertIsInstance(value[1], bool)

    def test_defaults_exist_in_the_catalog(self):
        self.assertIn(services.DEFAULT_BATTER_SORT, services.BATTER_SORT_KEYS)
        self.assertIn(services.DEFAULT_PITCHER_SORT, services.PITCHER_SORT_KEYS)
        self.assertIn(services.DEFAULT_ROSTER_SORT, services.BATTER_SORT_KEYS)
        self.assertIn(services.DEFAULT_ROSTER_SORT, services.PITCHER_SORT_KEYS)


class PlayedCountSortTest(TestCase):
    """出場試合数・登板数は Player に無いので、選手 id → 数の引き当てを渡して並べる。"""

    def test_batters_sort_by_games_most_first(self):
        players = [_batter("少", 1), _batter("多", 2)]

        ordered, key, desc = services.sort_batters(players, "games", games={1: 3, 2: 10})

        self.assertEqual((key, desc), ("games", True))
        self.assertEqual([p.name for p in ordered], ["多", "少"])

    def test_pitchers_sort_by_appearances_most_first(self):
        players = [_pitcher("少", 11), _pitcher("多", 18)]

        ordered, key, desc = services.sort_pitchers(players, "appearances", appearances={11: 1, 18: 40})

        self.assertEqual((key, desc), ("appearances", True))
        self.assertEqual([p.name for p in ordered], ["多", "少"])

    def test_without_counts_the_key_falls_back_to_the_default(self):
        """数を読んでいない呼び出しでは、キーを指定されても既定の並びに落ちる（エラーにしない）。"""
        _, batter_key, _ = services.sort_batters([_batter("一", 1)], "games")
        _, pitcher_key, _ = services.sort_pitchers([_pitcher("一", 11)], "appearances")

        self.assertEqual((batter_key, pitcher_key), ("ops", "era"))
