"""GM の育成選手のオフの指名（`DevelopmentPlan`・`ensure_development_plan`・`decisions_as_chosen`・
`confirm_chosen_promotions`。#170 P2）と、締める処理（`plan_departures`）への組み込み。DB 不要。

期待値の根拠: 行ごとに1択で、満了の行だけ再契約・自由契約（2026-10-24 ユーザー決定 Q4 A）。満了は
`is_contract_expiring`（育成3年目から毎年）。昇格は新人の枠に上乗せ（Q2。戦力外が自動なら指名した昇格の分も空ける）。
GM の判断は AI の規則（`MAX_PROMOTIONS`・新人の人数を守る）で縛らず、支配下の上限だけは超えない。
"""

from __future__ import annotations

import unittest

from myapp.domain.exceptions import InvalidDevelopmentPlan
from myapp.domain.pennant.development import (
    DEVELOPMENT_CONTRACT_YEARS,
    MAX_PROMOTIONS,
    DevelopmentDecision,
    DevelopmentMember,
    DevelopmentPlan,
    confirm_chosen_promotions,
    decisions_as_chosen,
    ensure_development_plan,
)
from myapp.domain.pennant.release import release_counts

from .test_development import departures, prospect
from .test_release import SEED, START_YEAR, YEAR, club, roster

KEEP = DevelopmentDecision.KEEP
PROMOTE = DevelopmentDecision.PROMOTE
RESIGN = DevelopmentDecision.RESIGN
RELEASE = DevelopmentDecision.RELEASE
# 締める年 YEAR に満了する育成の入団の年（3年目）と、満了しない育成の入団の年（1年目）
EXPIRING = YEAR - DEVELOPMENT_CONTRACT_YEARS + 1
FIRST_YEAR = YEAR


class DevelopmentDecisionTest(unittest.TestCase):
    def test_choices_depend_on_the_expiry(self) -> None:
        self.assertEqual(DevelopmentDecision.choices_for(is_expiring=True), (PROMOTE, RESIGN, RELEASE))
        self.assertEqual(DevelopmentDecision.choices_for(is_expiring=False), (KEEP, PROMOTE))

    def test_unchanged_is_resign_or_keep(self) -> None:
        self.assertIs(DevelopmentDecision.unchanged(is_expiring=True), RESIGN)
        self.assertIs(DevelopmentDecision.unchanged(is_expiring=False), KEEP)

    def test_from_label(self) -> None:
        for decision in DevelopmentDecision:
            self.assertIs(DevelopmentDecision.from_label(decision.label), decision)
        with self.assertRaises(InvalidDevelopmentPlan):
            DevelopmentDecision.from_label("放出")

    def test_plan_counts_and_auto(self) -> None:
        plan = DevelopmentPlan(1, YEAR, {10: PROMOTE, 11: PROMOTE, 12: RELEASE, 13: KEEP})
        self.assertTrue(plan.is_manual)
        self.assertEqual((plan.count(PROMOTE), plan.count(RELEASE), plan.count(RESIGN)), (2, 1, 0))
        auto = DevelopmentPlan(1, YEAR)
        self.assertFalse(auto.is_manual)
        self.assertEqual(auto.count(PROMOTE), 0)


class EnsureDevelopmentPlanTest(unittest.TestCase):
    MEMBERS = (DevelopmentMember(10, "一年", is_expiring=False), DevelopmentMember(11, "三年", is_expiring=True))

    def test_every_choice_allowed_for_each_row(self) -> None:
        for young in DevelopmentDecision.choices_for(is_expiring=False):
            for old in DevelopmentDecision.choices_for(is_expiring=True):
                with self.subTest(young=young, old=old):
                    ensure_development_plan({10: young, 11: old}, self.MEMBERS)

    def test_no_development_players_and_no_decisions(self) -> None:
        ensure_development_plan({}, ())

    def test_a_missing_player_is_not_filled_in(self) -> None:
        with self.assertRaisesRegex(InvalidDevelopmentPlan, "三年の扱いを選んでください"):
            ensure_development_plan({10: KEEP}, self.MEMBERS)

    def test_an_unknown_player_is_refused(self) -> None:
        with self.assertRaisesRegex(InvalidDevelopmentPlan, "id=99"):
            ensure_development_plan({10: KEEP, 11: RESIGN, 99: PROMOTE}, self.MEMBERS)

    def test_resign_and_release_only_for_expiring_players(self) -> None:
        for decision in (RESIGN, RELEASE):
            with self.subTest(decision=decision), self.assertRaisesRegex(InvalidDevelopmentPlan, "満了しない"):
                ensure_development_plan({10: decision, 11: RESIGN}, self.MEMBERS)

    def test_keep_is_not_for_expiring_players(self) -> None:
        with self.assertRaisesRegex(InvalidDevelopmentPlan, "満了する"):
            ensure_development_plan({10: KEEP, 11: KEEP}, self.MEMBERS)

    def test_promotions_are_not_capped_by_the_ai_rule(self) -> None:
        members = [DevelopmentMember(10 + index, f"育成{index}", is_expiring=False) for index in range(5)]
        self.assertGreater(len(members), MAX_PROMOTIONS)
        ensure_development_plan({member.player_id: PROMOTE for member in members}, members)


class DecisionsAsChosenTest(unittest.TestCase):
    def test_promotions_and_releases_in_id_order(self) -> None:
        players = [*roster(3), prospect(201, 40, entered_year=FIRST_YEAR), prospect(200, 40, entered_year=EXPIRING)]
        players.append(prospect(202, 40, entered_year=EXPIRING))
        chosen = decisions_as_chosen({201: PROMOTE, 200: PROMOTE, 202: RELEASE}, players, year=YEAR)
        self.assertEqual(chosen.promotions, (200, 201))
        self.assertEqual(chosen.releases, (202,))

    def test_players_no_longer_development_or_gone_are_dropped(self) -> None:
        # 引退した選手（`remaining` に無い）・支配下の選手は黙って除く
        players = [*roster(3), prospect(200, 40)]
        chosen = decisions_as_chosen({1: PROMOTE, 200: PROMOTE, 300: PROMOTE}, players, year=YEAR)
        self.assertEqual(chosen.promotions, (200,))

    def test_release_only_when_the_contract_expires(self) -> None:
        # 保存した後に満了でなくなることは無いが、満了しない選手を自由契約にしない（満了の判定は domain の1か所）
        players = [prospect(200, 40, entered_year=FIRST_YEAR)]
        self.assertEqual(decisions_as_chosen({200: RELEASE}, players, year=YEAR).releases, ())


class ConfirmChosenPromotionsTest(unittest.TestCase):
    def confirm(self, players, chosen, **limits):
        return confirm_chosen_promotions(
            club(players, **limits), players, chosen, year=YEAR, start_year=START_YEAR, world_seed=SEED
        )

    def test_the_gm_choice_is_not_trimmed_to_keep_the_draftees(self) -> None:
        # AI なら新人を削らないために昇格を削る人数（支配下 64人）でも、GM の指名は上げる
        players = [*roster(64), prospect(200, 30), prospect(201, 30)]
        self.assertEqual([item.player_id for item in self.confirm(players, (200, 201))], [200, 201])

    def test_beyond_the_limit_the_lower_valued_choices_are_dropped(self) -> None:
        players = [*roster(69), prospect(200, 30), prospect(201, 60)]
        self.assertEqual([item.player_id for item in self.confirm(players, (200, 201))], [201])
        full = [*roster(70), prospect(200, 30)]
        self.assertEqual(self.confirm(full, (200,)), ())
        self.assertEqual(len(self.confirm(full, (200,), registered_limit=None)), 1, "無制限なら削らない")

    def test_numbers_are_free_registered_numbers(self) -> None:
        players = [*roster(60), prospect(200, 30), prospect(201, 30)]
        promoted = self.confirm(players, (200, 201))
        used = {player.number for player in players}
        self.assertEqual(len({item.number for item in promoted}), 2)
        for item in promoted:
            self.assertNotIn(item.number, used)
            self.assertLessEqual(int(item.number), 99)


class PlanDeparturesWithManualDevelopmentTest(unittest.TestCase):
    def test_the_choices_are_used_instead_of_the_rule(self) -> None:
        # 自動なら上がらない弱い育成を上げ、自動なら再契約する強い満了の育成を自由契約にする
        weak = prospect(200, 30, entered_year=FIRST_YEAR)
        strong_expiring = prospect(201, 70, entered_year=EXPIRING)
        players = [*roster(64), weak, strong_expiring]
        self.assertEqual(departures(players).expired, ())

        result = departures(players, manual_development={1: {200: PROMOTE, 201: RELEASE}})
        self.assertEqual([item.player_id for item in result.promotions], [200])
        self.assertEqual(result.expired, (201,))
        survivor = next(player for player in result.survivors if player.player_id == 200)
        self.assertTrue(survivor.is_registered)
        self.assertEqual(survivor.promoted_year, YEAR + 1)

    def test_automatic_releases_make_room_for_the_chosen_promotions(self) -> None:
        players = [*roster(64), prospect(200, 30), prospect(201, 30)]
        usual = release_counts(registered=64, registered_limit=70).registered
        result = departures(players, manual_development={1: {200: PROMOTE, 201: PROMOTE}})
        self.assertEqual(len(result.released), usual + 2, "上乗せ（AI の昇格と同じ）")
        self.assertEqual(len(result.promotions), 2)

    def test_resign_and_keep_write_nothing(self) -> None:
        players = [*roster(64), prospect(200, 10, entered_year=EXPIRING), prospect(201, 70, entered_year=FIRST_YEAR)]
        result = departures(players, manual_development={1: {200: RESIGN, 201: KEEP}})
        self.assertEqual((result.promotions, result.expired), ((), ()))
        self.assertEqual(result.draft_teams[0].development_count, 2)

    def test_other_clubs_stay_automatic(self) -> None:
        # 受け持つ球団（id 2）の指名は、ほかの球団（id 1）の自動の規則を変えない
        players = [*roster(64), prospect(200, 10, entered_year=EXPIRING)]
        result = departures(players, manual_development={2: {200: RESIGN}})
        self.assertEqual(result.expired, (200,))


if __name__ == "__main__":
    unittest.main()
