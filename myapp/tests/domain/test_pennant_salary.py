"""ペナントの年俸の契約と契約更改（#40 K1a。`domain/pennant/contract.py`・`salary.py`）。Django も DB も使わない。

守りたいこと（#40 の計画 issuecomment-6891228201 と、2026-11-15 のユーザーの決定）:
- 年俸は円の整数・万円刻み。表記は「420万円」「1億円」「1億2,000万円」
- 同じ選手の契約は期間が重ならない
- 最低年俸は支配下 420万円（野球協約 2025 第89条）・育成 230万円（育成選手に関する規約 第4条②）
- 減額制限（協約 第92条）: 前の年俸が1億円を超えれば40%、1億円以下なら25%までしか下げない
- 相場は決定的で、能力と出場に対して下がらない。前の年俸は材料にしない
- AI の更改は相場へ一定の割合だけ寄せ、減額の下限で切る。上げ幅は余裕の倍率（K1b の口）で変わる
- 新人の初年度の年俸は 1,600万円まで（12球団の申し合わせ）
- 人件費は年俸の合計
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date

from myapp.domain.exceptions import InvalidSalaryContract
from myapp.domain.pennant.contract import (
    MAN,
    OKU,
    Salary,
    SalaryContract,
    ensure_no_overlap,
    salary_change_label,
    salary_label,
)
from myapp.domain.pennant.draft import Draftee, DraftRoute
from myapp.domain.pennant.finance import FinanceInputs, club_finance
from myapp.domain.pennant.offseason_player import OffseasonPlayer, TrackRecord
from myapp.domain.pennant.payroll_cap import PayrollCap
from myapp.domain.pennant.popularity import Popularity
from myapp.domain.pennant.ratings import PlayerRatings
from myapp.domain.pennant.retirement import PlayingTime
from myapp.domain.pennant.salary import (
    FOREIGN_MIN_SALARY,
    MAX_MARKET_SALARY,
    MIN_DEVELOPMENTAL_SALARY,
    MIN_REGISTERED_SALARY,
    ROOKIE_SALARY_CAP,
    MarketFacts,
    SalaryBasis,
    ai_renewal_offer,
    basis_of,
    decide_renewals,
    entered_year_of,
    foreign_salary,
    initial_salary,
    market_salary,
    payroll,
    rookie_salary,
    salary_basis,
    salary_floor,
)
from myapp.domain.pennant.ticket_price import TicketPrice
from myapp.domain.simulation.ratings import BatterRatings, PitcherRatings
from myapp.domain.value_objects import ContractStatus, DraftPick, Position, Profile

YEAR = 2027
REGISTERED = ContractStatus.REGISTERED
DEVELOPMENTAL = ContractStatus.DEVELOPMENTAL


def man(value: int) -> Salary:
    return Salary(value * MAN)


def basis(value: float = 50.0, *, seasons: int = 10, foreign: bool = False, contract=REGISTERED) -> SalaryBasis:
    return SalaryBasis(contract=contract, value=value, seasons_as_pro=seasons, is_foreign=foreign)


def batter(level: int) -> BatterRatings:
    return BatterRatings(contact=level, power=level, eye=level, speed=level, fielding=level)


def offseason_player(
    player_id: int,
    *,
    salary: Salary | None,
    level: int = 50,
    plate_appearances: int = 400,
    contract: ContractStatus = REGISTERED,
) -> OffseasonPlayer:
    return OffseasonPlayer(
        player_id=player_id,
        position=Position.INFIELDER,
        profile=Profile(birth_date=date(1995, 7, 1)),
        number=str(player_id),
        joined_year=2020,
        ratings=PlayerRatings(player_id, YEAR, batter(level)),
        contract=contract,
        playing_time=PlayingTime(plate_appearances=plate_appearances),
        salary=salary,
    )


class SalaryLabelTest(unittest.TestCase):
    def test_labels_use_oku_and_man(self):
        self.assertEqual(salary_label(420 * MAN), "420万円")
        self.assertEqual(salary_label(1_600 * MAN), "1,600万円")
        self.assertEqual(salary_label(OKU), "1億円")
        self.assertEqual(salary_label(OKU + 2_000 * MAN), "1億2,000万円")
        self.assertEqual(salary_label(12 * OKU), "12億円")
        self.assertEqual(salary_label(0), "0円")
        self.assertEqual(salary_label(-500 * MAN), "-500万円")
        self.assertEqual(man(440).label, "440万円")

    def test_change_labels(self):
        self.assertEqual(salary_change_label(1_000 * MAN, 2_000 * MAN), "+1,000万円")
        self.assertEqual(salary_change_label(2_000 * MAN, 1_500 * MAN), "-500万円")
        self.assertEqual(salary_change_label(OKU, OKU), "±0")


class SalaryTest(unittest.TestCase):
    def test_salary_is_a_positive_amount_in_man_yen(self):
        for bad in (0, -MAN, MAN + 1, 4_200_500):
            with self.subTest(bad=bad), self.assertRaises(InvalidSalaryContract):
                Salary(bad)
        with self.assertRaises(InvalidSalaryContract):
            Salary(True)  # type: ignore[arg-type]  # 真偽値は整数として通さない

    def test_contract_period(self):
        contract = SalaryContract(player_id=1, first_year=2027, last_year=2028, salary=man(500))
        self.assertTrue(contract.covers(2027))
        self.assertTrue(contract.covers(2028))
        self.assertFalse(contract.covers(2026))
        self.assertFalse(contract.covers(2029))
        with self.assertRaises(InvalidSalaryContract):
            SalaryContract(player_id=1, first_year=2028, last_year=2027, salary=man(500))
        self.assertEqual(SalaryContract.for_year(1, 2027, man(500)).last_year, 2027)


class NoOverlapTest(unittest.TestCase):
    def test_the_same_player_cannot_have_overlapping_periods_in_any_order(self):
        a = SalaryContract(player_id=1, first_year=2027, last_year=2028, salary=man(500))
        b = SalaryContract.for_year(1, 2028, man(600))
        for contracts in ([a, b], [b, a]):
            with self.subTest(order=contracts), self.assertRaises(InvalidSalaryContract):
                ensure_no_overlap(contracts)

    def test_adjacent_years_and_other_players_do_not_overlap(self):
        ensure_no_overlap(
            [
                SalaryContract.for_year(1, 2027, man(500)),
                SalaryContract.for_year(1, 2028, man(600)),
                SalaryContract.for_year(2, 2027, man(700)),
            ]
        )


class FloorTest(unittest.TestCase):
    """減額の下限（協約 第92条）と最低年俸（第89条・育成規約 第4条②）。"""

    def test_minimum_salaries(self):
        self.assertEqual(MIN_REGISTERED_SALARY, 420 * MAN)
        self.assertEqual(MIN_DEVELOPMENTAL_SALARY, 230 * MAN)

    def test_exactly_one_oku_is_limited_to_25_percent(self):
        self.assertEqual(salary_floor(Salary(OKU), contract=REGISTERED), man(7_500))

    def test_above_one_oku_is_limited_to_40_percent(self):
        self.assertEqual(salary_floor(Salary(OKU + MAN), contract=REGISTERED), man(6_001), "6,000.6万円は切り上げる")
        self.assertEqual(salary_floor(man(30_000), contract=REGISTERED), man(18_000))

    def test_below_one_oku_is_limited_to_25_percent(self):
        self.assertEqual(salary_floor(man(9_999), contract=REGISTERED), man(7_500), "7,499.25万円は切り上げる")
        self.assertEqual(salary_floor(man(2_000), contract=REGISTERED), man(1_500))

    def test_the_floor_never_goes_below_the_minimum_salary_of_the_next_contract(self):
        self.assertEqual(salary_floor(man(500), contract=REGISTERED), man(420))
        self.assertEqual(salary_floor(man(300), contract=DEVELOPMENTAL), man(230))
        self.assertEqual(salary_floor(man(300), contract=REGISTERED), man(420), "育成から昇格したら支配下の最低年俸")


class MarketTest(unittest.TestCase):
    def test_market_rises_with_ability(self):
        values = [market_salary(MarketFacts(basis(value), plate_appearances=300)) for value in range(25, 85)]
        self.assertEqual(values, sorted(values))
        self.assertLess(values[0], values[-1])

    def test_market_rises_with_playing_time_and_titles(self):
        by_plate = [market_salary(MarketFacts(basis(), plate_appearances=pa)) for pa in range(0, 701, 50)]
        by_outs = [market_salary(MarketFacts(basis(), outs=outs)) for outs in range(0, 601, 30)]
        by_titles = [market_salary(MarketFacts(basis(), plate_appearances=500, titles=t)) for t in range(6)]
        for values in (by_plate, by_outs, by_titles):
            self.assertEqual(values, sorted(values))
        self.assertLess(by_plate[0], by_plate[-1])
        self.assertLess(by_titles[0], by_titles[3])
        self.assertEqual(by_titles[3], by_titles[5], "数えるタイトルは3つまで")

    def test_market_is_deterministic_and_within_the_bounds(self):
        facts = MarketFacts(basis(60), plate_appearances=550, titles=1)
        self.assertEqual(market_salary(facts), market_salary(facts))
        self.assertGreaterEqual(market_salary(MarketFacts(basis(10))).yen, MIN_REGISTERED_SALARY)
        self.assertLessEqual(
            market_salary(MarketFacts(basis(99), plate_appearances=700, titles=3)).yen, MAX_MARKET_SALARY
        )

    def test_developmental_and_foreign_players(self):
        dev = market_salary(MarketFacts(basis(45, contract=DEVELOPMENTAL)))
        self.assertGreaterEqual(dev.yen, MIN_DEVELOPMENTAL_SALARY)
        self.assertLess(dev, market_salary(MarketFacts(basis(45))))
        self.assertGreaterEqual(market_salary(MarketFacts(basis(30, foreign=True))).yen, FOREIGN_MIN_SALARY)
        self.assertGreater(
            market_salary(MarketFacts(basis(55, foreign=True), plate_appearances=500)),
            market_salary(MarketFacts(basis(55), plate_appearances=500)),
        )


class RenewalOfferTest(unittest.TestCase):
    def test_moves_up_toward_the_market_but_not_past_it(self):
        offer = ai_renewal_offer(man(1_000), man(5_000), contract=REGISTERED)
        self.assertGreater(offer, man(1_000))
        self.assertLess(offer, man(5_000))

    def test_moves_down_toward_the_market_but_stops_at_the_floor(self):
        offer = ai_renewal_offer(man(5_000), man(4_500), contract=REGISTERED)
        self.assertLess(offer, man(5_000))
        self.assertGreater(offer, man(4_500))
        self.assertEqual(ai_renewal_offer(man(5_000), man(420), contract=REGISTERED), man(3_750), "25%まで")
        self.assertEqual(ai_renewal_offer(man(30_000), man(420), contract=REGISTERED), man(18_000), "40%まで")

    def test_stays_when_the_market_matches(self):
        self.assertEqual(ai_renewal_offer(man(2_000), man(2_000), contract=REGISTERED), man(2_000))

    def test_the_room_factor_scales_raises_only(self):
        """K1b が総額の上限の余裕で上げ幅を変える口。下げには効かない。"""
        self.assertEqual(ai_renewal_offer(man(1_000), man(5_000), contract=REGISTERED, room_factor=0.0), man(1_000))
        self.assertLess(
            ai_renewal_offer(man(1_000), man(5_000), contract=REGISTERED, room_factor=0.5),
            ai_renewal_offer(man(1_000), man(5_000), contract=REGISTERED),
        )
        self.assertEqual(
            ai_renewal_offer(man(5_000), man(1_000), contract=REGISTERED, room_factor=0.0),
            ai_renewal_offer(man(5_000), man(1_000), contract=REGISTERED),
        )
        with self.assertRaises(ValueError):
            ai_renewal_offer(man(1_000), man(5_000), contract=REGISTERED, room_factor=-0.1)


class DecideRenewalsTest(unittest.TestCase):
    def test_renewals_do_not_depend_on_the_order_of_players(self):
        players = [offseason_player(i, salary=man(400 + 100 * i), level=40 + i) for i in range(1, 8)]
        forward = decide_renewals(players, year=YEAR, budget=None)
        backward = decide_renewals(list(reversed(players)), year=YEAR, budget=None)
        self.assertEqual(forward, backward)
        self.assertEqual([r.player_id for r in forward], list(range(1, 8)))

    def test_players_without_a_salary_are_not_renewed(self):
        renewals = decide_renewals(
            [offseason_player(1, salary=None), offseason_player(2, salary=man(500))], year=YEAR, budget=None
        )
        self.assertEqual([r.player_id for r in renewals], [2])

    def test_a_promoted_player_gets_at_least_the_registered_minimum(self):
        (renewal,) = decide_renewals(
            [offseason_player(1, salary=man(300), level=30, plate_appearances=0)], year=YEAR, budget=None
        )
        self.assertGreaterEqual(renewal.salary.yen, MIN_REGISTERED_SALARY)

    def test_a_star_regular_gets_a_raise_and_an_idle_veteran_a_cut(self):
        star, idle = decide_renewals(
            [
                offseason_player(1, salary=man(3_000), level=65, plate_appearances=600),
                offseason_player(2, salary=man(20_000), level=40, plate_appearances=0),
            ],
            year=YEAR,
            budget=None,
        )
        self.assertGreater(star.change, 0)
        self.assertLess(idle.change, 0)
        self.assertEqual(idle.salary, man(12_000), "1億円を超える選手は40%まで")

    def test_the_market_uses_the_track_record_titles(self):
        plain = offseason_player(1, salary=man(5_000), level=60, plate_appearances=550)
        champion = replace(plain, track_record=TrackRecord(titles=2))
        (a,) = decide_renewals([plain], year=YEAR, budget=None)
        (b,) = decide_renewals([champion], year=YEAR, budget=None)
        self.assertGreater(b.salary, a.salary)


class NewcomerSalaryTest(unittest.TestCase):
    @staticmethod
    def draftee(round_number: int | None, *, development: bool = False) -> Draftee:
        return Draftee(
            team_id=1,
            route=DraftRoute.HIGH_SCHOOL,
            position=Position.PITCHER,
            number="18",
            name="新人",
            profile=Profile(draft=DraftPick(year=YEAR, round=round_number, is_development=development)),
            ratings=PitcherRatings(),
        )

    def test_rookie_salaries_by_round_and_the_cap(self):
        salaries = [rookie_salary(self.draftee(n)) for n in range(1, 9)]
        self.assertEqual(salaries[0].yen, ROOKIE_SALARY_CAP)
        self.assertEqual(salaries, sorted(salaries, reverse=True))
        self.assertTrue(all(s.yen <= ROOKIE_SALARY_CAP for s in salaries))
        self.assertTrue(all(s.yen >= MIN_REGISTERED_SALARY for s in salaries))
        development = rookie_salary(self.draftee(1, development=True))
        self.assertGreaterEqual(development.yen, MIN_DEVELOPMENTAL_SALARY)
        self.assertLess(development.yen, MIN_REGISTERED_SALARY)

    def test_foreign_signings_cost_at_least_the_foreign_minimum_and_rise_with_ability(self):
        weak = foreign_salary(Position.INFIELDER, batter(35), year=YEAR)
        strong = foreign_salary(Position.INFIELDER, batter(60), year=YEAR)
        self.assertGreaterEqual(weak.yen, FOREIGN_MIN_SALARY)
        self.assertGreater(strong, weak)


class InitialSalaryTest(unittest.TestCase):
    def test_young_players_are_paid_less_than_veterans_of_the_same_ability(self):
        values = [initial_salary(basis(58, seasons=n)) for n in range(1, 10)]
        self.assertEqual(values, sorted(values))
        self.assertLess(values[0], values[-1])

    def test_foreign_and_developmental_players_do_not_ramp(self):
        self.assertEqual(initial_salary(basis(55, seasons=1, foreign=True)), initial_salary(basis(55, foreign=True)))
        self.assertGreaterEqual(initial_salary(basis(30, contract=DEVELOPMENTAL)).yen, MIN_DEVELOPMENTAL_SALARY)

    def test_the_closing_and_the_reader_build_the_same_basis(self):
        """締める処理（`basis_of`）と読み口（`salary_basis`）は同じ作り方（補った値が一致する）。"""
        player = replace(
            offseason_player(1, salary=None, level=55),
            profile=Profile(draft=DraftPick(year=2021, round=2), is_foreign_player=False),
        )
        assert player.ratings is not None
        self.assertEqual(
            basis_of(player, YEAR),
            salary_basis(
                position=player.position,
                ratings=player.ratings.ratings,
                contract=REGISTERED,
                is_foreign=False,
                entered_year=2022,
                year=YEAR,
            ),
        )

    def test_the_entry_year_is_the_year_after_the_draft_or_the_joined_year(self):
        """プロ入りの年は `DraftPick.entry_year`（ドラフトの翌年）、無ければ在籍の開始年。
        `OffseasonPlayer.entered_year` と同じ規則。"""
        self.assertEqual(entered_year_of(draft_year=2020, joined_year=2026), 2021)
        self.assertEqual(entered_year_of(draft_year=None, joined_year=2026), 2026)
        player = replace(
            offseason_player(1, salary=None), profile=Profile(draft=DraftPick(year=2020)), joined_year=2026
        )
        self.assertEqual(player.entered_year, entered_year_of(draft_year=2020, joined_year=2026))

    def test_population_of_initial_salaries_is_in_the_npb_range(self):
        """能力の総合値が平均45・標準偏差8ほどの支配下の日本人の年俸（プロ6年目）の中央値は 1,000〜3,000万円、
        最高は相場の上限まで（選手会の 2026年度の調査の中央値 2,000万円。目安で、端を詰めない）。"""
        values = sorted(initial_salary(basis(29 + i * 0.4)).yen for i in range(81))
        self.assertGreaterEqual(values[40], 1_000 * MAN)
        self.assertLessEqual(values[40], 3_000 * MAN)
        self.assertLessEqual(values[-1], MAX_MARKET_SALARY)


class PayrollTest(unittest.TestCase):
    def test_payroll_is_the_sum_of_salaries_and_feeds_the_finance(self):
        salaries = [man(420), man(1_500), Salary(OKU)]
        self.assertEqual(payroll(salaries), (420 + 1_500 + 10_000) * MAN)
        self.assertEqual(payroll([]), 0)
        finance = club_finance(
            FinanceInputs(
                regular_attendance=0,
                postseason_attendance=0,
                ticket_price=TicketPrice.STANDARD,
                popularity=Popularity(50),
                payroll=payroll(salaries),
                capacity=None,
                payroll_cap=PayrollCap(yen=100 * OKU, estimated=False),
            )
        )
        self.assertEqual(finance.payroll, payroll(salaries))
