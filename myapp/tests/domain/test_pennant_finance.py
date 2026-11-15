"""ペナントのチケットの値段と球団の収支（#208 E2）。Django も DB も使わない。

守りたいこと（2026-11-14 ユーザーの決定。#371 のコメント「ユーザーの決定」）:
- 値段は5段階（倍率 0.7〜1.3、入場率への効き +0.08〜−0.10、人気への効き 年 +1〜−2）。高いほど単価が上がり、
  入場率と人気が下がる
- AI（お任せ）は人気で決める: 80以上で高い・65以上でやや高い・20以下で安い・35以下でやや安い・それ以外は標準
- 手動の値段があればそれ、無ければ AI の規則
- 変えられるのはその年の開幕前だけ（その年の試合が1つも無い）
- 収支は円で NPB の水準: 入場料＝観客動員 × 単価、ポストシーズンは単価の1.5倍、仮の人件費は支配下 4,000万円・
  育成 500万円、赤字は親会社が補填（補填後は0）、平均的な球団で補填前の収支がおおむね0
- 人気への値段の効きは世界の平均を引かない（全球団が上げれば全球団の人気が下がる）
"""

from dataclasses import replace
from datetime import date
from unittest import TestCase

from myapp.domain.exceptions import InvalidSeason
from myapp.domain.pennant.finance import (
    DEVELOPMENTAL_PAYROLL,
    OKU,
    POSTSEASON_PRICE_FACTOR,
    REGISTERED_PAYROLL,
    FinanceInputs,
    RosterSize,
    club_finance,
    money_label,
    parent_subsidy,
    provisional_payroll,
    season_share,
)
from myapp.domain.pennant.popularity import (
    PENNANT_DEFAULT_CAPACITY,
    Popularity,
    PopularityFactors,
    next_popularities,
    pennant_capacity,
)
from myapp.domain.pennant.ticket_price import (
    BASE_TICKET_PRICE,
    ClubTicketPrice,
    TicketPrice,
    ai_ticket_price,
    resolved_ticket_price,
    ticket_price_change_blocker,
)

YEAR = 2026
SEED = 20261114
# 平均的な球団の1年（8リーグの世界の1年目の実測。標準の収容人数3万人の球団のホームの観客動員 約179万人、
# ポストシーズンの入場料 平均 3.5億円 ≒ 5.8万人、支配下65人・育成20人。
# Wiki「ペナントモードの仕組み」の「実測値」の「人気と収支」）
AVERAGE_ATTENDANCE = 1_790_000
AVERAGE_POSTSEASON = 58_000
AVERAGE_ROSTER = RosterSize(registered=65, developmental=20)


def _inputs(**overrides) -> FinanceInputs:
    average = FinanceInputs(
        regular_attendance=AVERAGE_ATTENDANCE,
        postseason_attendance=AVERAGE_POSTSEASON,
        ticket_price=TicketPrice.STANDARD,
        popularity=Popularity(50),
        roster=AVERAGE_ROSTER,
        capacity=None,
    )
    return replace(average, **overrides)


class TicketPriceTest(TestCase):
    def test_five_levels_from_cheap_to_expensive(self):
        self.assertEqual([price.label for price in TicketPrice], ["安い", "やや安い", "標準", "やや高い", "高い"])
        self.assertEqual([price.multiplier for price in TicketPrice], [0.7, 0.85, 1.0, 1.15, 1.3])
        self.assertEqual([price.fill_effect for price in TicketPrice], [0.08, 0.04, 0.0, -0.05, -0.10])
        self.assertEqual([price.popularity_effect for price in TicketPrice], [1.0, 0.5, 0.0, -1.0, -2.0])

    def test_the_standard_unit_price_is_four_thousand_yen(self):
        self.assertEqual(BASE_TICKET_PRICE, 4_000)
        self.assertEqual(TicketPrice.STANDARD.unit_price, 4_000)
        self.assertEqual(TicketPrice.HIGH.unit_price, 5_200)
        self.assertEqual(TicketPrice.LOW.unit_price, 2_800)

    def test_the_ai_rule_follows_popularity(self):
        cases = {1: "安い", 20: "安い", 21: "やや安い", 35: "やや安い", 36: "標準", 64: "標準"}
        cases |= {65: "やや高い", 79: "やや高い", 80: "高い", 100: "高い"}
        for popularity, label in cases.items():
            with self.subTest(popularity=popularity):
                self.assertEqual(ai_ticket_price(popularity).label, label)

    def test_a_manual_price_wins_over_the_ai_rule(self):
        self.assertIs(resolved_ticket_price(TicketPrice.LOW, 90), TicketPrice.LOW)
        self.assertIs(resolved_ticket_price(None, 90), TicketPrice.HIGH)

    def test_the_price_can_change_only_before_the_opening_of_the_year(self):
        """2026-11-14 ユーザー決定: 変えられるのはその年の開幕前だけ。"""
        self.assertIsNone(ticket_price_change_blocker(year=YEAR, last_played_on=None))
        self.assertIsNone(ticket_price_change_blocker(year=YEAR + 1, last_played_on=date(YEAR, 10, 30)))
        blocker = ticket_price_change_blocker(year=YEAR, last_played_on=date(YEAR, 3, 28))
        self.assertIsNotNone(blocker)
        self.assertIn("開幕", blocker or "")

    def test_a_club_price_validates_the_year(self):
        self.assertEqual(ClubTicketPrice(1, YEAR, TicketPrice.HIGH).year, YEAR)
        with self.assertRaises(InvalidSeason):
            ClubTicketPrice(1, 1800, TicketPrice.HIGH)


class PopularityEffectTest(TestCase):
    def _next(self, prices: dict[int, TicketPrice]) -> dict[int, int]:
        current = {team_id: Popularity(50) for team_id in prices}
        factors = {
            team_id: PopularityFactors(winning_percentage=0.5, ticket_price=price) for team_id, price in prices.items()
        }
        return {
            team_id: value.value
            for team_id, value in next_popularities(current, factors, world_seed=SEED, year=YEAR).items()
        }

    def test_the_standard_price_keeps_the_formula_before_e2(self):
        """標準の値段は、値段を足す前の式と同じ値（材料を省いたのと同じ）。"""
        current = {team_id: Popularity(50 + team_id) for team_id in range(6)}
        plain = {team_id: PopularityFactors(winning_percentage=0.4 + team_id / 30) for team_id in current}
        priced = {
            team_id: PopularityFactors(winning_percentage=0.4 + team_id / 30, ticket_price=TicketPrice.STANDARD)
            for team_id in current
        }
        self.assertEqual(
            next_popularities(current, plain, world_seed=SEED, year=YEAR),
            next_popularities(current, priced, world_seed=SEED, year=YEAR),
        )

    def test_a_high_price_lowers_and_a_low_price_raises_next_years_popularity(self):
        """同じ球団（同じぶれ）で値段だけを変えると、高いほど翌年の人気が下がる。"""
        values = [self._next({1: price, 2: TicketPrice.STANDARD})[1] for price in TicketPrice]
        self.assertEqual(values, sorted(values, reverse=True))
        self.assertGreater(values[0], values[-1])

    def test_the_price_effect_is_not_offset_by_the_world_average(self):
        """全球団が高くすれば、全球団の人気が下がる（世界の平均を引かない）。"""
        standard = self._next(dict.fromkeys(range(1, 7), TicketPrice.STANDARD))
        expensive = self._next(dict.fromkeys(range(1, 7), TicketPrice.HIGH))
        for team_id in standard:
            with self.subTest(team_id=team_id):
                self.assertEqual(expensive[team_id], standard[team_id] - 2)


class FinanceTest(TestCase):
    def test_gate_and_postseason_revenue_use_the_unit_price(self):
        finance = club_finance(
            _inputs(regular_attendance=1_000_000, postseason_attendance=60_000, ticket_price=TicketPrice.HIGH)
        )
        self.assertEqual(finance.gate, 1_000_000 * 5_200)
        self.assertEqual(finance.postseason, round(60_000 * 5_200 * POSTSEASON_PRICE_FACTOR))

    def test_the_standard_gate_is_around_seventy_oku_yen_a_year(self):
        """2026-11-14 ユーザー決定: 入場料は標準で年 70億円前後。"""
        gate = club_finance(_inputs()).gate
        self.assertGreater(gate, 60 * OKU)
        self.assertLess(gate, 80 * OKU)

    def test_popularity_raises_broadcasting_sponsorship_and_merchandise(self):
        low = club_finance(_inputs(popularity=Popularity(30)))
        high = club_finance(_inputs(popularity=Popularity(80)))
        self.assertLess(low.broadcasting, high.broadcasting)
        self.assertLess(low.sponsorship, high.sponsorship)
        self.assertLess(low.merchandise, high.merchandise)
        for finance in (low, high):
            self.assertGreater(finance.broadcasting, 0)
            self.assertGreater(finance.sponsorship, 0)

    def test_the_provisional_payroll_has_two_contract_classes(self):
        """2026-11-14 ユーザー決定: 支配下 4,000万円・育成 500万円（外国人も支配下の定額）。"""
        self.assertEqual(REGISTERED_PAYROLL, 40_000_000)
        self.assertEqual(DEVELOPMENTAL_PAYROLL, 5_000_000)
        self.assertEqual(provisional_payroll(RosterSize(registered=2, developmental=3)), 95_000_000)
        self.assertEqual(club_finance(_inputs()).payroll, provisional_payroll(AVERAGE_ROSTER))

    def test_a_bigger_stadium_costs_more_to_run(self):
        small = club_finance(_inputs(capacity=20_000))
        big = club_finance(_inputs(capacity=45_000))
        self.assertLess(small.operations, big.operations)
        self.assertEqual(
            club_finance(_inputs(capacity=None)).operations,
            club_finance(_inputs(capacity=PENNANT_DEFAULT_CAPACITY)).operations,
        )
        self.assertEqual(pennant_capacity(None), PENNANT_DEFAULT_CAPACITY)

    def test_an_average_club_roughly_breaks_even_before_the_subsidy(self):
        """平均的な球団（人気50・標準の値段・標準の収容人数）で補填前の収支がおおむね0（±5億円）。"""
        balance = club_finance(_inputs()).operating_balance
        self.assertLess(abs(balance), 5 * OKU, money_label(balance))

    def test_a_deficit_is_covered_by_the_parent_company_and_a_surplus_is_not(self):
        deficit = club_finance(_inputs(popularity=Popularity(20), regular_attendance=1_200_000))
        self.assertLess(deficit.operating_balance, 0)
        self.assertEqual(deficit.subsidy, -deficit.operating_balance)
        self.assertEqual(deficit.balance, 0)
        surplus = club_finance(_inputs(popularity=Popularity(80), regular_attendance=2_300_000))
        self.assertGreater(surplus.operating_balance, 0)
        self.assertEqual(surplus.subsidy, 0)
        self.assertEqual(surplus.balance, surplus.operating_balance)
        self.assertEqual(parent_subsidy(-3), 3)
        self.assertEqual(parent_subsidy(0), 0)

    def test_annual_items_are_prorated_during_the_season(self):
        full = club_finance(_inputs())
        half = club_finance(_inputs(season_share=0.5))
        self.assertEqual(half.broadcasting, round(full.broadcasting / 2))
        self.assertEqual(half.payroll, round(full.payroll / 2))
        self.assertEqual(half.operations, round(full.operations / 2))
        self.assertEqual(half.gate, full.gate, "入場料は観客動員から（按分しない）")
        before_opening = club_finance(_inputs(regular_attendance=0, postseason_attendance=0, season_share=0.0))
        self.assertEqual((before_opening.revenue, before_opening.expenses), (0, 0))

    def test_the_season_share(self):
        self.assertEqual(season_share(games_played=0, games_per_team=143, finished=False), 0.0)
        self.assertAlmostEqual(season_share(games_played=71, games_per_team=143, finished=False), 71 / 143)
        self.assertEqual(season_share(games_played=10, games_per_team=143, finished=True), 1.0)
        self.assertEqual(season_share(games_played=150, games_per_team=143, finished=False), 1.0)

    def test_inputs_reject_negative_values(self):
        with self.assertRaises(ValueError):
            _inputs(regular_attendance=-1)
        with self.assertRaises(ValueError):
            _inputs(season_share=1.5)
        with self.assertRaises(ValueError):
            RosterSize(registered=-1, developmental=0)

    def test_money_is_shown_in_oku_yen(self):
        self.assertEqual(money_label(7_034_000_000), "70.3億円")
        self.assertEqual(money_label(-250_000_000), "-2.5億円")
        self.assertEqual(money_label(-1_000_000), "0.0億円")
        self.assertEqual(money_label(123_400_000_000), "1,234.0億円")
