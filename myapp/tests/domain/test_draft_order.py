"""ドラフト会議の指名順の規則（`domain/pennant/draft_order.py`）。DB 不要。"""

from __future__ import annotations

import unittest

from myapp.domain.pennant.draft_order import (
    DraftOrderRule,
    DraftStandings,
    lottery_order,
    strategy_for,
)
from myapp.domain.pennant.world import league_priority

# 2リーグ × 6球団。リーグ 10 は 1〜6、リーグ 20 は 7〜12（どちらも1位から順）
CENTRAL = (1, 2, 3, 4, 5, 6)
PACIFIC = (7, 8, 9, 10, 11, 12)
STANDINGS = DraftStandings(leagues=(CENTRAL, PACIFIC))
REVERSE = (6, 12, 5, 11, 4, 10, 3, 9, 2, 8, 1, 7)


class RuleTest(unittest.TestCase):
    def test_every_rule_has_a_strategy(self) -> None:
        """規則を足して戦略を足し忘れると KeyError になる。ここで見張る。"""
        for rule in DraftOrderRule:
            with self.subTest(rule=rule):
                strategy = strategy_for(rule, STANDINGS, world_seed=1, year=2026)
                self.assertEqual(sorted(strategy.order(1)), sorted(REVERSE))

    def test_labels_and_descriptions_are_for_the_screen(self) -> None:
        self.assertEqual([rule.label for rule in DraftOrderRule], ["NPB 方式", "完全ウェーバー", "くじ順", "全巡入札"])
        for rule in DraftOrderRule:
            self.assertTrue(rule.description)


class StandingsTest(unittest.TestCase):
    """「球団順位の逆順」: 下からの段 × 段の中はリーグの優先順（NPB の選択手順の一般化）。"""

    def test_two_leagues_alternate_from_the_bottom_with_the_priority_league_first(self) -> None:
        self.assertEqual(STANDINGS.reverse_order(), REVERSE)
        self.assertEqual(STANDINGS.standing_order(), tuple(reversed(REVERSE)))

    def test_a_smaller_league_drops_out_at_the_upper_tiers(self) -> None:
        standings = DraftStandings(leagues=((1, 2, 3, 4), (5, 6, 7, 8, 9, 10)))
        self.assertEqual(standings.reverse_order(), (4, 10, 3, 9, 2, 8, 1, 7, 6, 5))

    def test_one_league_is_the_plain_reverse_order(self) -> None:
        self.assertEqual(DraftStandings(leagues=((3, 1, 2),)).reverse_order(), (2, 1, 3))

    def test_three_leagues_take_turns_in_the_priority_order(self) -> None:
        standings = DraftStandings(leagues=((1, 2), (3, 4), (5, 6)))
        self.assertEqual(standings.reverse_order(), (2, 4, 6, 1, 3, 5))

    def test_the_priority_league_rotates_every_year(self) -> None:
        """優先リーグは年ごとに交互（NPB の 2019〜2025 年の記載）。並べるのは `world.league_priority`。"""
        firsts = []
        for year in range(2026, 2030):
            priority = league_priority([10, 20], year=year, start_year=2026)
            leagues = {10: CENTRAL, 20: PACIFIC}
            firsts.append(DraftStandings(leagues=tuple(leagues[league] for league in priority)).reverse_order()[0])
        self.assertEqual(firsts, [6, 12, 6, 12])

    def test_unranked_clubs_come_first(self) -> None:
        standings = DraftStandings(leagues=((1, 2),), unranked=(9, 8))
        self.assertEqual(standings.reverse_order(), (8, 9, 2, 1))

    def test_a_club_cannot_appear_twice(self) -> None:
        with self.assertRaises(ValueError):
            DraftStandings(leagues=((1, 2), (2, 3)))


class StrategyTest(unittest.TestCase):
    def test_npb_bids_the_first_round_then_snakes_from_the_reverse_order(self) -> None:
        strategy = strategy_for(DraftOrderRule.NPB, STANDINGS, world_seed=1, year=2026)
        self.assertEqual([strategy.is_bidding(n) for n in range(1, 5)], [True, False, False, False])
        self.assertEqual(strategy.order(2), REVERSE)
        self.assertEqual(strategy.order(3), tuple(reversed(REVERSE)))
        self.assertEqual(strategy.order(4), REVERSE)

    def test_waiver_never_bids_and_always_goes_from_the_bottom(self) -> None:
        strategy = strategy_for(DraftOrderRule.WAIVER, STANDINGS, world_seed=1, year=2026)
        for round_number in range(1, 11):
            self.assertFalse(strategy.is_bidding(round_number))
            self.assertEqual(strategy.order(round_number), REVERSE)

    def test_all_lottery_bids_every_round(self) -> None:
        strategy = strategy_for(DraftOrderRule.ALL_LOTTERY, STANDINGS, world_seed=1, year=2026)
        self.assertTrue(all(strategy.is_bidding(round_number) for round_number in range(1, 11)))

    def test_random_snake_draws_an_order_that_snakes_and_ignores_the_standings(self) -> None:
        strategy = strategy_for(DraftOrderRule.RANDOM_SNAKE, STANDINGS, world_seed=1, year=2026)
        first = strategy.order(1)
        self.assertFalse(strategy.is_bidding(1))
        self.assertEqual(strategy.order(2), tuple(reversed(first)))
        self.assertEqual(strategy.order(3), first)
        # 同じ世界・同じ年なら同じ順。順位を入れ替えても変わらない（前年の順位に依らない）
        flipped = DraftStandings(leagues=(tuple(reversed(PACIFIC)), tuple(reversed(CENTRAL))))
        self.assertEqual(strategy_for(DraftOrderRule.RANDOM_SNAKE, flipped, world_seed=1, year=2026).order(1), first)

    def test_the_lottery_order_changes_with_the_year(self) -> None:
        ids = list(range(1, 13))
        orders = {lottery_order(ids, world_seed=1, year=year) for year in range(2026, 2031)}
        self.assertGreater(len(orders), 1)
        self.assertEqual(lottery_order(ids, world_seed=1, year=2026), lottery_order(ids, world_seed=1, year=2026))


if __name__ == "__main__":
    unittest.main()
