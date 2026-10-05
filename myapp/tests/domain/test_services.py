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

    def test_hit_ranking_counts_every_kind_of_hit(self):
        """最多安打は単打も長打も1本として数える。"""
        sluggers = _batter("長打", 1, at_bats=20, home_runs=3, doubles=1)
        singles = _batter("単打", 2, at_bats=20, singles=5)
        hitless = _batter("無安打", 3, at_bats=20)

        result = services.leaders_by_hits([sluggers, singles, hitless])

        self.assertEqual([r.player.name for r in result], ["単打", "長打"])
        self.assertEqual(result[0].value, 5.0)

    def test_stolen_base_ranking_excludes_zero(self):
        runner = _batter("俊足", 1, at_bats=10, singles=3, stolen_bases=4)
        slow = _batter("鈍足", 2, at_bats=10, singles=3)

        result = services.leaders_by_stolen_bases([slow, runner])

        self.assertEqual([r.player.name for r in result], ["俊足"])
        self.assertEqual(result[0].value, 4.0)

    def test_counting_batting_titles_exclude_pitchers(self):
        """投手の打撃成績は打撃タイトルに入れない（本塁打・打点と同じ）。"""
        pitcher = Player(
            name="投手",
            number=JerseyNumber(11),
            position=Position.PITCHER,
            id=11,
            batting=BattingLine(at_bats=10, singles=5, stolen_bases=2),
        )

        self.assertEqual(services.leaders_by_hits([pitcher]), [])
        self.assertEqual(services.leaders_by_stolen_bases([pitcher]), [])

    def test_on_base_percentage_ranking_counts_walks(self):
        """打率が同じでも四球の多い選手が上に来る。"""
        patient = _batter("選球眼", 1, at_bats=10, singles=3, walks=5)
        free = _batter("早打ち", 2, at_bats=10, singles=3)

        result = services.leaders_by_on_base_percentage([free, patient])

        self.assertEqual([r.player.name for r in result], ["選球眼", "早打ち"])
        self.assertAlmostEqual(result[0].value, 8 / 15)

    def test_on_base_percentage_requires_qualifying_plate_appearances(self):
        """首位打者と同じく規定打席未満は除く。1打席1四球で出塁率10割の選手を首位にしない。"""
        regular = _batter("規定到達", 1, at_bats=30, singles=9, walks=2)
        part_timer = _batter("代打", 2, walks=1)

        result = services.leaders_by_on_base_percentage([regular, part_timer], team_games={1: 10, 2: 10})

        self.assertEqual([r.player.name for r in result], ["規定到達"])


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

    def test_win_ranking_sorted_and_excludes_zero(self):
        ace = _pitcher("エース", 18, wins=12)
        second = _pitcher("二番手", 19, wins=8)
        winless = _pitcher("未勝利", 20, wins=0)

        result = services.leaders_by_wins([second, ace, winless])

        self.assertEqual([r.player.name for r in result], ["エース", "二番手"])
        self.assertEqual(result[0].value, 12.0)

    def test_win_ranking_ignores_the_innings_threshold(self):
        """勝利は数そのものが記録なので、投球回が少なくても載る。"""
        reliever = _pitcher("救援", 30, notation="10.0", wins=5, relief_wins=5)

        result = services.leaders_by_wins([reliever])

        self.assertEqual([r.player.name for r in result], ["救援"])

    def test_save_ranking_sorted_and_excludes_zero(self):
        closer = _pitcher("守護神", 22, saves=30)
        setup = _pitcher("勝ちパターン", 23, saves=3)
        starter = _pitcher("先発", 24, saves=0)

        result = services.leaders_by_saves([setup, closer, starter])

        self.assertEqual([r.player.name for r in result], ["守護神", "勝ちパターン"])
        self.assertEqual(result[0].value, 30.0)

    def test_hold_point_ranking_counts_relief_wins(self):
        """最優秀中継ぎは HP（ホールド＋救援勝利）で決まる。ホールドだけで並べない。"""
        holder = _pitcher("ホールド型", 40, holds=20)
        winner = _pitcher("救援勝利型", 41, holds=15, wins=8, relief_wins=8)
        starter = _pitcher("先発", 42, wins=10)

        result = services.leaders_by_hold_points([holder, starter, winner])

        self.assertEqual([r.player.name for r in result], ["救援勝利型", "ホールド型"])
        self.assertEqual(result[0].value, 23.0)

    def test_winning_percentage_requires_thirteen_wins(self):
        """最高勝率は13勝以上が対象。12勝0敗の10割でも載らない。"""
        unbeaten = _pitcher("無敗", 11, wins=12)
        ace = _pitcher("エース", 18, wins=13, losses=4)

        result = services.leaders_by_winning_percentage([unbeaten, ace])

        self.assertEqual(services.WINNING_PERCENTAGE_MINIMUM_WINS, 13)
        self.assertEqual([r.player.name for r in result], ["エース"])
        self.assertAlmostEqual(result[0].value, 13 / 17)

    def test_winning_percentage_ranking_sorted_by_rate_not_wins(self):
        many = _pitcher("多勝", 11, wins=18, losses=8)
        efficient = _pitcher("高勝率", 18, wins=14, losses=2)

        result = services.leaders_by_winning_percentage([many, efficient])

        self.assertEqual([r.player.name for r in result], ["高勝率", "多勝"])

    def test_batters_are_not_in_win_or_save_rankings(self):
        batter = _batter("野手", 1, at_bats=10, singles=3)

        self.assertEqual(services.leaders_by_wins([batter]), [])
        self.assertEqual(services.leaders_by_saves([batter]), [])

    def test_empty_input_returns_empty(self):
        self.assertEqual(services.leaders_by_era([]), [])
        self.assertEqual(services.leaders_by_ops([]), [])
        self.assertEqual(services.leaders_by_wins([]), [])
        self.assertEqual(services.leaders_by_saves([]), [])
