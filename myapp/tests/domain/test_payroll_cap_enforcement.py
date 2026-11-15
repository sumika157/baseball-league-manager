"""年俸の上限で更改の上げ幅と外国人の獲得を止める（#40 K1b-2・#394）。Django も DB も使わない。

守りたいこと（#394 の計画と 2026-11-15 のユーザーの決定）:
- 更改の上げ幅は、球団ごとの予算（翌年の上限 − 新人の取り置き）に収まるよう0〜1倍に縮める。上限が十分なら K1a と
  同じ更改。下げは減額制限のとおりで、予算を超えても更改は通す（上限で止めるのは年俸の増える獲得だけ）
- 新人の取り置きは本指名の巡ごとの年俸の和（育成は数えない）
- 外国人の年俸は球団の見立てから（獲得を決めた時点の額を書く。#394 Q2 の A）
- AI の獲得・GM の指名・期中の獲得は、獲得の後の合計が上限を超える候補を取らない（ちょうどは取る）。GM の指名は
  見立ての低い候補から削る。上限で取れなかった枠はドラフトの日本人の新人に回る
- 保有の6人の上限はやめた（7人目も取れる）
"""

from __future__ import annotations

import unittest
from dataclasses import replace

from myapp.domain.exceptions import InvalidSigning
from myapp.domain.pennant import draft
from myapp.domain.pennant.contract import MAN, OKU, Salary
from myapp.domain.pennant.draft_meeting import DraftContext
from myapp.domain.pennant.draft_order import DraftOrderRule, DraftStandings
from myapp.domain.pennant.foreign_market import (
    candidate_salary,
    choose_signings,
    confirm_chosen_signings,
    offseason_key,
    offseason_signings,
    scouting_report,
)
from myapp.domain.pennant.offseason import plan_departures, plan_offseason, renew_within_caps
from myapp.domain.pennant.offseason_player import OffseasonClub
from myapp.domain.pennant.payroll_cap import PayrollCap, ensure_within_cap, room_factor
from myapp.domain.pennant.salary import (
    ROOKIE_LATER_SALARY,
    ROOKIE_SALARIES,
    decide_renewals,
    foreign_salary,
    payroll,
    rookie_reserve,
    rookie_round_salary,
)
from myapp.tests.domain.payroll_builders import AMPLE_CAP

from .test_foreign_market import AVERAGE, SEED, START_YEAR, YEAR, candidates, club, team, values_of
from .test_pennant_salary import offseason_player


def cap(oku: float) -> PayrollCap:
    return PayrollCap(yen=round(oku * OKU), estimated=False)


class RoomFactorTest(unittest.TestCase):
    def test_enough_budget_keeps_the_full_raise(self):
        self.assertEqual(room_factor(budget=100, at_zero=60, at_full=100), 1.0)
        self.assertEqual(room_factor(budget=200, at_zero=60, at_full=100), 1.0, "余裕があっても1を超えない")

    def test_no_budget_for_raises(self):
        self.assertEqual(room_factor(budget=60, at_zero=60, at_full=100), 0.0)
        self.assertEqual(room_factor(budget=10, at_zero=60, at_full=100), 0.0)

    def test_the_budget_in_between_is_interpolated(self):
        self.assertAlmostEqual(room_factor(budget=80, at_zero=60, at_full=100), 0.5)
        self.assertAlmostEqual(room_factor(budget=70, at_zero=60, at_full=100), 0.25)


class EnsureWithinCapTest(unittest.TestCase):
    def test_exactly_the_cap_passes_and_one_yen_more_does_not(self):
        ensure_within_cap(40 * OKU, cap(40))
        with self.assertRaisesRegex(InvalidSigning, "上限"):
            ensure_within_cap(40 * OKU + MAN, cap(40))


class RookieReserveTest(unittest.TestCase):
    def test_the_reserve_is_the_sum_of_the_rounds(self):
        self.assertEqual(rookie_reserve(0), 0)
        self.assertEqual(rookie_reserve(-1), 0)
        self.assertEqual(rookie_reserve(1), 1_600 * MAN)
        self.assertEqual(rookie_reserve(len(ROOKIE_SALARIES)), sum(ROOKIE_SALARIES))
        self.assertEqual(rookie_reserve(len(ROOKIE_SALARIES) + 2), sum(ROOKIE_SALARIES) + 2 * ROOKIE_LATER_SALARY)

    def test_the_reserve_matches_the_rookie_salaries_of_the_rounds(self):
        """取り置きは、実際に新人に書く巡ごとの年俸（`rookie_round_salary`）の和と同じ出典。"""
        for slots in range(9):
            with self.subTest(slots=slots):
                self.assertEqual(
                    rookie_reserve(slots), sum(rookie_round_salary(number).yen for number in range(1, slots + 1))
                )


class RenewalsWithinBudgetTest(unittest.TestCase):
    def players(self):
        # 上げる選手（相場より安い主力）と、下げる選手（出場の無い高給の選手）
        return [
            offseason_player(1, salary=Salary(2_000 * MAN), level=65, plate_appearances=600),
            offseason_player(2, salary=Salary(1_000 * MAN), level=62, plate_appearances=550),
            offseason_player(3, salary=Salary(20_000 * MAN), level=40, plate_appearances=0),
        ]

    def test_an_ample_budget_gives_the_same_renewals_as_without_a_cap(self):
        players = self.players()
        self.assertEqual(
            decide_renewals(players, year=YEAR, budget=1_000 * OKU),
            decide_renewals(players, year=YEAR, budget=None),
        )

    def test_a_tight_budget_shrinks_only_the_raises(self):
        players = self.players()
        full = decide_renewals(players, year=YEAR, budget=None)
        none = decide_renewals(players, year=YEAR, budget=0)
        # 予算0: 上げない（前の年俸のまま）。下げは減額制限のとおり（予算を超えても更改は通す）
        self.assertEqual([r.salary for r in none[:2]], [p.salary for p in players[:2]])
        self.assertEqual(none[2].salary, full[2].salary)
        # 間の予算: 上げ幅は縮み、合計はおおむね予算（丸めの分だけずれる）
        budget = (payroll(r.salary for r in full) + payroll(r.salary for r in none)) // 2
        half = decide_renewals(players, year=YEAR, budget=budget)
        for before, shrunk, raised in zip(players[:2], half[:2], full[:2], strict=True):
            assert before.salary is not None
            self.assertLessEqual(before.salary, shrunk.salary)
            self.assertLess(shrunk.salary, raised.salary)
        self.assertLess(abs(payroll(r.salary for r in half) - budget), 100 * MAN)

    def test_the_order_does_not_matter(self):
        players = self.players()
        self.assertEqual(
            decide_renewals(players, year=YEAR, budget=25_000 * MAN),
            decide_renewals(list(reversed(players)), year=YEAR, budget=25_000 * MAN),
        )


class CandidateSalaryTest(unittest.TestCase):
    def test_the_salary_comes_from_the_scouting_report(self):
        """年俸は本当の能力でなく見立てから（候補の年俸から能力を逆算できない。#394 Q2 の A）。入団は翌年。"""
        for candidate in candidates():
            with self.subTest(index=candidate.key.index):
                report = scouting_report(candidate, world_seed=SEED)
                self.assertEqual(
                    candidate_salary(candidate, world_seed=SEED),
                    foreign_salary(candidate.position, report.ratings, year=YEAR + 1),
                )

    def test_the_signing_carries_the_salary_decided_when_signing(self):
        club_team = team()
        result = offseason_signings([club_team], year=YEAR, world_seed=SEED, used_names=set())
        self.assertTrue(result.signings, "前提: 獲得する")
        for signing in result.signings:
            self.assertEqual(signing.salary, candidate_salary(signing.candidate, world_seed=SEED))


def capped(team_, *, payroll_: int, cap_: PayrollCap):
    return replace(team_, payroll=payroll_, payroll_cap=cap_)


class OffseasonSigningsWithinCapTest(unittest.TestCase):
    def test_the_ai_stops_when_nothing_fits(self):
        """上限に収まる候補がいなければ獲得せず、補いたかった人数を「上限で獲得しなかった人数」に数える。"""
        base = team()
        slots, _ = draft.newcomer_plan(base)
        wanted = draft.foreign_count_to_add(base, slots)
        self.assertGreater(wanted, 0, "前提: AI は外国人を補う")
        full = capped(base, payroll_=40 * OKU, cap_=cap(40))
        result = offseason_signings([full], year=YEAR, world_seed=SEED, used_names=set())
        self.assertEqual(result.signings, ())
        self.assertEqual(result.cap_trimmed, wanted)

    def test_the_ai_signs_within_the_cap_including_the_rookie_reserve(self):
        base = team()
        slots, _ = draft.newcomer_plan(base)
        drawn = candidates()
        cheapest = min(candidate_salary(c, world_seed=SEED).yen for c in drawn)
        # ちょうど1人ぶん（最も安い候補）と、残る新人の枠の取り置きだけ空いている
        room = cheapest + rookie_reserve(slots - 1)
        tight = capped(base, payroll_=40 * OKU - room, cap_=cap(40))
        chosen = choose_signings(drawn, 2, tight, reference=AVERAGE, world_seed=SEED)
        self.assertEqual([candidate_salary(c, world_seed=SEED).yen for c in chosen], [cheapest])
        result = offseason_signings([tight], year=YEAR, world_seed=SEED, used_names=set())
        signed = sum(s.salary.yen for s in result.signings)
        self.assertLessEqual(
            tight.payroll + signed + rookie_reserve(slots - len(result.signings)), tight.payroll_cap.yen
        )

    def test_the_gm_choice_is_trimmed_from_the_lowest_estimate(self):
        """GM の指名は、見立ての高い候補から上限に収まる分だけ確定する（収まらない候補を削り、削った人数を返す）。"""
        drawn = candidates()
        values = values_of(drawn)
        keys = frozenset(offseason_key(YEAR, index) for index in (0, 1, 4))
        ranked = sorted((c for c in drawn if c.key in keys), key=lambda c: (-values[c.key], c.key.index))
        best = ranked[0]
        base = team()
        slots, _ = draft.newcomer_plan(base)
        room = candidate_salary(best, world_seed=SEED).yen + rookie_reserve(slots - 1)
        tight = capped(base, payroll_=40 * OKU - room, cap_=cap(40))
        chosen = confirm_chosen_signings(drawn, keys, tight, world_seed=SEED)
        self.assertEqual(chosen[0], best)
        result = offseason_signings(
            [tight], year=YEAR, world_seed=SEED, used_names=set(), manual={tight.team_id: keys}
        )
        self.assertEqual(len(result.signings) + result.cap_trimmed, 3)
        self.assertGreater(result.cap_trimmed, 0)

    def test_a_seventh_foreigner_can_be_signed(self):
        """保有の6人の上限はやめた（#394）。外国人を6人持つ球団も、上限に収まれば指名した候補を獲得する。"""
        six = capped(team(foreign=6), payroll_=0, cap_=AMPLE_CAP)
        keys = frozenset(offseason_key(YEAR, index) for index in (0, 1))
        result = offseason_signings([six], year=YEAR, world_seed=SEED, used_names=set(), manual={six.team_id: keys})
        self.assertEqual(len(result.signings), 2)
        self.assertEqual(result.teams[0].foreign_count, 8)


def paid_club(team_id: int, *, foreign: int, payroll_cap: PayrollCap) -> OffseasonClub:
    """年俸のある球団（支配下59人。全員1,500万円）。"""
    base = club(team_id, foreign=foreign)
    players = tuple(replace(player, salary=Salary(1_500 * MAN)) for player in base.players)
    return replace(base, players=players, payroll_cap=payroll_cap)


class PlanWithinCapTest(unittest.TestCase):
    @staticmethod
    def plan(clubs):
        context = DraftContext(DraftOrderRule.NPB, DraftStandings(leagues=(tuple(c.team_id for c in clubs),)))
        return plan_offseason(clubs, year=YEAR, world_seed=SEED, start_year=START_YEAR, used_names=(), draft=context)

    def test_an_ample_cap_changes_nothing(self):
        """上限が十分なら、上限を見ないときと同じ更改・獲得（K1a と同じ）。"""
        ample = [paid_club(1, foreign=0, payroll_cap=AMPLE_CAP)]
        result = self.plan(ample)
        departures = plan_departures(ample, year=YEAR, world_seed=SEED, start_year=START_YEAR)
        self.assertEqual(result.renewals, decide_renewals(departures.survivors, year=YEAR, budget=None))
        self.assertEqual(result.cap_trimmed, 0)
        self.assertTrue(result.signings)

    def test_a_low_cap_stops_the_raises_and_the_signings(self):
        """上限が更改の前の合計より低い球団は、上げず（下げは通す）、外国人を獲得せず、その枠を日本人の新人が埋める。"""
        low = [paid_club(1, foreign=0, payroll_cap=cap(5))]
        result = self.plan(low)
        self.assertEqual(result.signings, ())
        self.assertGreater(result.cap_trimmed, 0)
        self.assertTrue(all(renewal.change <= 0 for renewal in result.renewals))
        departures = plan_departures(low, year=YEAR, world_seed=SEED, start_year=START_YEAR)
        slots, _ = draft.newcomer_plan(departures.draft_teams[0])
        japanese = [d for d in result.draftees if not d.is_development]
        self.assertEqual(len(japanese), slots)

    def test_renew_within_caps_puts_the_payroll_and_the_cap_on_the_teams(self):
        clubs = [paid_club(1, foreign=0, payroll_cap=cap(30)), paid_club(2, foreign=2, payroll_cap=AMPLE_CAP)]
        departures = plan_departures(clubs, year=YEAR, world_seed=SEED, start_year=START_YEAR)
        renewals, teams = renew_within_caps(clubs, departures, year=YEAR)
        self.assertEqual([r.player_id for r in renewals], sorted(r.player_id for r in renewals))
        own = {p.player_id for p in clubs[0].players}
        for team_ in teams:
            with self.subTest(team=team_.team_id):
                members = [r for r in renewals if (r.player_id in own) == (team_.team_id == 1)]
                self.assertEqual(team_.payroll, payroll(r.salary for r in members))
                self.assertEqual(team_.payroll_cap, clubs[team_.team_id - 1].payroll_cap)


if __name__ == "__main__":
    unittest.main()
