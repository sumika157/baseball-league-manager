"""GM の外国人のオフの指名（`foreign_market.ForeignSigningPlan`・`confirm_chosen_signings`。#169 F2）。DB 不要。

期待値の根拠: 候補は球団ごとに8人・見えるのはスカウトの見立てだけ・オフは外国人の保有がゲームの上限6人まで
（2026-10-25 ユーザー決定 Q4 A）。獲得した外国人は新しい支配下の選手の枠の内側で数える（Q2 A）。
指名した候補は AI の選択と差し替え、締めるときに枠・保有の上限・位置の最低人数に収まらない分を見立ての低い順に削る。
"""

from __future__ import annotations

import random
import unittest
from collections import Counter
from dataclasses import replace

from myapp.domain.pennant import draft
from myapp.domain.pennant.draft_meeting import DraftContext
from myapp.domain.pennant.draft_order import DraftOrderRule, DraftStandings
from myapp.domain.pennant.foreign_market import (
    MAX_FOREIGN_HELD,
    CandidateKey,
    ForeignSigningPlan,
    SigningPhase,
    candidate_outlook_grade,
    confirm_chosen_signings,
    held_after_chosen_departures,
    offseason_key,
    offseason_signings,
    scouting_report,
)
from myapp.domain.pennant.offseason import plan_departures, plan_offseason
from myapp.domain.pennant.outlook import PEAK_AGE, outlook_grade, projected_ratings
from myapp.domain.value_objects import Position

from .test_foreign_market import SEED, START_YEAR, YEAR, candidates, club, team, values_of


def keys_of(*indices: int) -> frozenset[CandidateKey]:
    return frozenset(offseason_key(YEAR, index) for index in indices)


class ForeignSigningPlanTest(unittest.TestCase):
    def test_none_is_auto_and_an_empty_set_is_manual_with_nobody(self) -> None:
        self.assertFalse(ForeignSigningPlan(1, YEAR).is_manual)
        empty = ForeignSigningPlan(1, YEAR, frozenset())
        self.assertTrue(empty.is_manual)
        self.assertEqual(empty.indices, frozenset())
        self.assertEqual(ForeignSigningPlan(1, YEAR, keys_of(0, 5)).indices, {0, 5})

    def test_only_the_offseason_candidates_of_the_year(self) -> None:
        for key in (
            CandidateKey(YEAR - 1, SigningPhase.OFFSEASON, 0, 0),
            CandidateKey(YEAR, SigningPhase.MIDSEASON, 0, 0),
            CandidateKey(YEAR, SigningPhase.OFFSEASON, 1, 0),
        ):
            with self.subTest(key=key), self.assertRaises(ValueError):
                ForeignSigningPlan(1, YEAR, frozenset({key}))

    def test_the_key_is_the_offseason_draw(self) -> None:
        self.assertEqual([c.key for c in candidates()], [offseason_key(YEAR, index) for index in range(8)])


class ConfirmChosenSigningsTest(unittest.TestCase):
    def test_the_gm_is_not_bound_by_the_ai_shortfall(self) -> None:
        """外国人が目標（4人）に達した球団でも、指名した候補を獲得する（保有の上限まで）。"""
        full = team(foreign=4)
        self.assertEqual(draft.foreign_count_to_add(full, draft.newcomer_plan(full)[0]), 0, "前提: AI は獲得しない")
        chosen = confirm_chosen_signings(candidates(), keys_of(0, 4), full, world_seed=SEED)
        self.assertEqual({c.key.index for c in chosen}, {0, 4})

    def test_nothing_chosen_signs_nobody(self) -> None:
        self.assertEqual(confirm_chosen_signings(candidates(), frozenset(), team(), world_seed=SEED), ())

    def test_the_newcomer_slots_keep_the_best_estimates(self) -> None:
        """新しい支配下の選手の枠（上端までの1人）を超える分は、見立ての低い候補から削る。"""
        narrow = team(size=65)
        self.assertEqual(draft.newcomer_plan(narrow)[0], 1, "前提: 枠は1人")
        drawn = candidates()
        values = values_of(drawn)
        chosen = confirm_chosen_signings(drawn, keys_of(0, 1, 4), narrow, world_seed=SEED)
        best = max((offseason_key(YEAR, i) for i in (0, 1, 4)), key=lambda key: (values[key], -key.index))
        self.assertEqual([c.key for c in chosen], [best])

    def test_the_held_limit(self) -> None:
        chosen = confirm_chosen_signings(
            candidates(), keys_of(0, 1, 2), team(foreign=MAX_FOREIGN_HELD - 1), world_seed=SEED
        )
        self.assertEqual(len(chosen), 1)
        self.assertEqual(
            confirm_chosen_signings(candidates(), keys_of(0), team(foreign=MAX_FOREIGN_HELD), world_seed=SEED), ()
        )

    def test_the_position_minimum_is_kept(self) -> None:
        """投手が新人の枠の分だけ足りない球団は、指名した野手を獲得しない（会議の指名・AI の獲得と同じ制約）。"""
        counts = {Position.PITCHER: 9, Position.CATCHER: 3, Position.INFIELDER: 25, Position.OUTFIELDER: 22}
        short_pitchers = replace(team(), position_counts=counts)
        drawn = candidates()
        infielder = next(c for c in drawn if c.position is Position.INFIELDER)
        pitcher = next(c for c in drawn if c.position is Position.PITCHER)
        chosen = confirm_chosen_signings(
            drawn, frozenset({infielder.key, pitcher.key}), short_pitchers, world_seed=SEED
        )
        self.assertEqual(chosen, (pitcher,))

    def test_unknown_keys_are_ignored_and_the_order_does_not_matter(self) -> None:
        drawn = list(candidates())
        shuffled = drawn[:]
        random.Random(2).shuffle(shuffled)
        chosen = keys_of(1, 3, 6) | {offseason_key(YEAR, 99)}
        self.assertEqual(
            confirm_chosen_signings(drawn, chosen, team(), world_seed=SEED),
            confirm_chosen_signings(shuffled, chosen, team(), world_seed=SEED),
        )
        self.assertEqual(len(confirm_chosen_signings(drawn, chosen, team(), world_seed=SEED)), 3)


class ManualOffseasonSigningsTest(unittest.TestCase):
    def sign(self, teams, manual=None):
        return offseason_signings(teams, year=YEAR, world_seed=SEED, used_names=set(), manual=manual)

    def test_the_chosen_candidates_replace_the_ai_choice(self) -> None:
        result = self.sign([team(1, foreign=4), team(2, foreign=1)], manual={1: keys_of(2, 7)})
        mine = [s for s in result.signings if s.team_id == 1]
        self.assertEqual({s.candidate.key.index for s in mine}, {2, 7})
        self.assertEqual(result.teams[0].foreign_count, 6)

    def test_an_empty_choice_signs_nobody_even_when_short(self) -> None:
        result = self.sign([team(1, foreign=0)], manual={1: frozenset()})
        self.assertEqual(result.signings, ())
        self.assertEqual(result.teams, (team(1, foreign=0),))

    def test_other_clubs_stay_with_the_ai(self) -> None:
        """受け持つ球団の指名は、ほかの球団の獲得を変えない（候補は球団ごと・名前の衝突は名前だけ引き直す）。"""
        clubs = [team(1, foreign=0), team(2, foreign=1)]
        auto = self.sign(clubs)
        manual = self.sign(clubs, manual={1: keys_of(0)})
        others = [(s.candidate.key, s.number) for s in auto.signings if s.team_id == 2]
        self.assertEqual(others, [(s.candidate.key, s.number) for s in manual.signings if s.team_id == 2])


class ManualPlanOffseasonTest(unittest.TestCase):
    def test_japanese_rookies_are_the_newcomers_minus_the_chosen_signings(self) -> None:
        """GM が0人を選べば、外国人の不足があっても新人は全員日本人（Q2 A: 獲得は新人の枠の内側）。"""
        clubs = [club(1, foreign=0)]
        context = DraftContext(DraftOrderRule.NPB, DraftStandings(leagues=((1,),)))
        for chosen, signed in ((frozenset(), 0), (keys_of(0, 1), 2)):
            result = plan_offseason(
                clubs,
                year=YEAR,
                world_seed=SEED,
                start_year=START_YEAR,
                used_names=(),
                draft=context,
                manual_signings={1: chosen},
            )
            (draft_team,) = plan_departures(clubs, year=YEAR, world_seed=SEED, start_year=START_YEAR).draft_teams
            count, _ = draft.newcomer_plan(draft_team)
            japanese = [d for d in result.draftees if not d.is_development]
            with self.subTest(signed=signed):
                self.assertEqual(len(result.signings), signed)
                self.assertEqual(len(japanese), count - signed)
                self.assertEqual(Counter(s.candidate.key for s in result.signings), Counter(chosen))


class CandidateOutlookTest(unittest.TestCase):
    def test_the_outlook_uses_the_scouting_report(self) -> None:
        drawn = candidates()
        for candidate in drawn:
            report = scouting_report(candidate, world_seed=SEED)
            grade = candidate_outlook_grade(candidate, report)
            with self.subTest(index=candidate.key.index, age=candidate.age):
                if candidate.age >= PEAK_AGE:
                    self.assertIsNone(grade, "ピークの年齢以上は「—」")
                else:
                    expected = outlook_grade(
                        projected_ratings(
                            report.ratings,
                            age=candidate.age,
                            position=candidate.position,
                            potential_estimate=report.potential,
                        )
                    )
                    self.assertEqual(grade, expected)
        self.assertTrue(any(c.age < PEAK_AGE for c in drawn), "前提: ピークより若い候補がいる")


class HeldAfterChosenDeparturesTest(unittest.TestCase):
    def test_foreigners_chosen_to_leave_are_not_counted(self) -> None:
        self.assertEqual(held_after_chosen_departures([1, 2, 3], []), 3)
        self.assertEqual(held_after_chosen_departures([1, 2, 3], [2, 9]), 2, "外国人でない選手の指名は数えに効かない")


if __name__ == "__main__":
    unittest.main()
