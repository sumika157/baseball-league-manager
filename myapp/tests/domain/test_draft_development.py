"""育成ドラフト（ドラフト会議の育成の巡。#198 D2）と、育成を錨の母集団から外すこと。DB 不要。

本指名は `test_draft_meeting.py`、オフ全体の分布は `test_offseason_population.py` が見る。
"""

from __future__ import annotations

import unittest
from collections import Counter
from dataclasses import replace

from myapp.domain.exceptions import InvalidDraftAction
from myapp.domain.pennant import draft
from myapp.domain.pennant.draft import (
    MAX_DEVELOPMENT_PICKS,
    DraftRoute,
    DraftTeam,
    development_draftee_count,
    development_positions,
    development_target,
    plan_team_draft,
)
from myapp.domain.pennant.draft_meeting import (
    DraftAction,
    DraftActionKind,
    DraftContext,
    DraftSlot,
    DraftState,
    draw_pool,
    run_draft,
)
from myapp.domain.pennant.draft_order import DraftOrderRule, DraftStandings, strategy_for
from myapp.domain.pennant.roster import DEVELOPMENT_BAND, OPENING_REGISTERED_BAND
from myapp.domain.simulation.randomness import make_random
from myapp.domain.simulation.ratings import BatterRatings, PitcherRatings
from myapp.domain.simulation.spread import rescale, spread_ratings
from myapp.domain.value_objects import AcquisitionRoute, ContractStatus, JerseyNumber, Position
from myapp.domain.virtual_players import generator

YEAR = 2027


def club(team_id: int, *, development: int = 0, target: int = 20, registered: int | None = None) -> DraftTeam:
    """帯の中（59人。ふつうの新人6人）の球団。外国人は目標に達している。育成は `development` 人・目標 `target`。"""
    size = OPENING_REGISTERED_BAND.high - draft.USUAL_DRAFTEES - 1 if registered is None else registered
    return DraftTeam(
        team_id=team_id,
        position_counts=generator.largest_remainder(size, generator.POSITION_RATIOS),
        foreign_count=4,
        foreign_limit=4,
        used_numbers=frozenset(),
        surname_counts={},
        registered_limit=70,
        development_counts=generator.largest_remainder(development, generator.POSITION_RATIOS),
        development_target=target,
    )


def meeting(teams: list[DraftTeam], rule: DraftOrderRule = DraftOrderRule.NPB, *, actions=()) -> DraftState:
    ids = [team.team_id for team in teams]
    slots = [DraftSlot.of(team, plan_team_draft(team)) for team in teams]
    planned = Counter(position for slot in slots for position in slot.planned)
    pool = draw_pool(planned, world_seed=1, year=YEAR, used_names=set())
    strategy = strategy_for(rule, DraftStandings(leagues=(tuple(ids),)), world_seed=1, year=YEAR)
    return DraftState.open(strategy, slots, pool, actions)


def run(teams: list[DraftTeam], rule: DraftOrderRule = DraftOrderRule.NPB):
    context = DraftContext(rule, DraftStandings(leagues=(tuple(team.team_id for team in teams),)))
    return run_draft(teams, context, year=YEAR, world_seed=1, used_names=set())


class DevelopmentCountTest(unittest.TestCase):
    def test_the_shortfall_up_to_the_yearly_maximum(self) -> None:
        self.assertEqual(development_draftee_count(18, target=20), 2)
        self.assertEqual(development_draftee_count(0, target=20), MAX_DEVELOPMENT_PICKS)
        self.assertEqual(development_draftee_count(20, target=20), 0)
        self.assertEqual(development_draftee_count(25, target=20), 0)

    def test_the_target_is_in_the_band_and_fixed_for_the_club(self) -> None:
        targets = [development_target(team_id, world_seed=7) for team_id in range(1, 201)]
        self.assertTrue(all(DEVELOPMENT_BAND.low <= value <= DEVELOPMENT_BAND.high for value in targets))
        self.assertGreater(len(set(targets)), 10, "球団ごとに違う")
        self.assertEqual(targets[0], development_target(1, world_seed=7))
        self.assertNotEqual(targets, [development_target(t, world_seed=8) for t in range(1, 201)])

    def test_positions_follow_the_target_composition_of_the_developmental_roster(self) -> None:
        positions = development_positions({Position.PITCHER: 10}, 6)
        self.assertEqual(len(positions), 6)
        self.assertNotIn(Position.PITCHER, positions, "投手は目標の構成を超えている")
        self.assertEqual(development_positions({}, 0), ())

    def test_development_does_not_change_the_registered_plan(self) -> None:
        plain = plan_team_draft(club(1, target=0))
        with_development = plan_team_draft(club(1, development=3, target=25))
        self.assertEqual(plain.japanese_positions, with_development.japanese_positions)
        self.assertEqual(plain.foreign_positions, with_development.foreign_positions)
        self.assertEqual(len(with_development.development_positions), MAX_DEVELOPMENT_PICKS)


class DevelopmentRoundTest(unittest.TestCase):
    """本指名の後の育成の巡。"""

    def test_every_club_fills_its_development_plan_after_the_main_rounds(self) -> None:
        teams = [club(1, development=18), club(2, development=25, target=27), club(3, target=0)]
        for rule in DraftOrderRule:
            with self.subTest(rule=rule):
                state = meeting(teams, rule)
                state.auto_complete()
                self.assertTrue(state.is_complete)
                for team in teams:
                    plan = plan_team_draft(team)
                    picks = [pick for _, pick in state.drafted()[team.team_id] if pick.is_development]
                    self.assertEqual(
                        [pick.round for pick in picks], list(range(1, len(plan.development_positions) + 1))
                    )
                main = [s for s in state.selections if not s.is_development]
                development = [s for s in state.selections if s.is_development]
                self.assertEqual(state.selections, main + development, "育成は本指名の後")

    def test_the_order_continues_the_last_main_round(self) -> None:
        """6巡で終わった本指名の続きは7巡目＝NPB 方式では順位順（1位から）。完全ウェーバーは逆順のまま。"""
        teams = [club(team_id, target=25) for team_id in (1, 2, 3, 4)]
        npb = meeting(teams, DraftOrderRule.NPB)
        npb.auto_complete()
        first = [s.team_id for s in npb.selections if s.is_development and s.round_number == 1]
        self.assertEqual(first, [1, 2, 3, 4])
        second = [s.team_id for s in npb.selections if s.is_development and s.round_number == 2]
        self.assertEqual(second, [4, 3, 2, 1])
        waiver = meeting(teams, DraftOrderRule.WAIVER)
        waiver.auto_complete()
        self.assertEqual(
            [s.team_id for s in waiver.selections if s.is_development and s.round_number == 1], [4, 3, 2, 1]
        )

    def test_all_lottery_bids_in_the_development_rounds_too(self) -> None:
        state = meeting([club(team_id, target=25) for team_id in (1, 2, 3)], DraftOrderRule.ALL_LOTTERY)
        actions = state.auto_complete()
        state = meeting([club(team_id, target=25) for team_id in (1, 2, 3)], DraftOrderRule.ALL_LOTTERY)
        for action in actions:
            turn = state.next_turn()
            assert turn is not None
            if turn.is_development:
                self.assertTrue(turn.is_bid)
                self.assertIs(action.kind, DraftActionKind.BID)
            state.apply(action)

    def test_no_development_round_once_the_main_total_reaches_ten_per_club(self) -> None:
        """NPB の「合計120名に達していなければ育成」の一般化（球団数 × 10）。"""
        teams = [club(team_id, registered=30, target=25) for team_id in (1, 2)]
        self.assertTrue(all(len(plan_team_draft(t).japanese_positions) == draft.MAX_DRAFTEES for t in teams))
        state = meeting(teams)
        state.auto_complete()
        self.assertFalse(any(s.is_development for s in state.selections))

    def test_a_club_without_development_slots_does_not_take_part(self) -> None:
        state = meeting([club(1, target=25), club(2, target=0)])
        state.auto_complete()
        self.assertEqual({s.team_id for s in state.selections if s.is_development}, {1})

    def test_a_club_can_pass_at_any_time_in_the_development_rounds(self) -> None:
        teams = [club(1, target=25), club(2, target=25)]
        state = meeting(teams)
        while (turn := state.next_turn()) is not None and not turn.is_development:
            state.apply(state.ai_action())
        turn = state.next_turn()
        assert turn is not None
        state.apply(DraftAction(DraftActionKind.PASS, turn.team_id))
        state.auto_complete()
        self.assertFalse(any(s.team_id == turn.team_id and s.is_development for s in state.selections))
        with self.assertRaises(InvalidDraftAction):
            state.apply(DraftAction(DraftActionKind.PASS, turn.team_id))

    def test_rebuilding_from_the_actions_gives_the_same_state(self) -> None:
        teams = [club(team_id, development=team_id, target=24) for team_id in range(1, 5)]
        state = meeting(teams, DraftOrderRule.ALL_LOTTERY)
        actions = state.auto_complete()
        again = meeting(teams, DraftOrderRule.ALL_LOTTERY, actions=actions)
        self.assertEqual(again.selections, state.selections)
        self.assertEqual(again.lotteries, state.lotteries)
        self.assertTrue(again.is_complete)

    def test_the_main_picks_are_the_same_with_or_without_development(self) -> None:
        """育成の候補は本指名の残りなので、育成の巡を足しても本指名の結果は変わらない。"""
        plain = meeting([club(team_id, target=0) for team_id in range(1, 7)])
        plain.auto_complete()
        with_development = meeting([club(team_id, target=25) for team_id in range(1, 7)])
        with_development.auto_complete()
        self.assertEqual(plain.selections, [s for s in with_development.selections if not s.is_development])

    def test_slot_must_fit_the_yearly_development_maximum(self) -> None:
        with self.assertRaises(ValueError):
            DraftSlot(1, 0, 0, (), {}, development_max=MAX_DEVELOPMENT_PICKS + 1)


class CorporateNotInDevelopmentTest(unittest.TestCase):
    """社会人の選手は育成で指名できない（日本野球連盟の申し入れによる慣行。#307）。本指名では指名できる。"""

    TEAMS = [club(team_id, development=team_id, target=25) for team_id in range(1, 7)]

    def _until_development(self, state: DraftState) -> None:
        while (turn := state.next_turn()) is not None and not turn.is_development:
            state.apply(state.ai_action())

    def test_the_rule_is_on_the_route(self) -> None:
        self.assertFalse(DraftRoute.CORPORATE.allows_development_pick)
        for route in (DraftRoute.HIGH_SCHOOL, DraftRoute.UNIVERSITY):
            self.assertTrue(route.allows_development_pick)

    def test_the_ai_never_picks_corporate_players_in_the_development_rounds(self) -> None:
        for rule in DraftOrderRule:
            with self.subTest(rule=rule):
                state = meeting(self.TEAMS, rule)
                state.auto_complete()
                routes = Counter(
                    (selection.is_development, state.pool.get(selection.prospect_key).route)
                    for selection in state.selections
                )
                self.assertGreater(sum(n for (dev, _), n in routes.items() if dev), 0, "前提: 育成の巡がある")
                self.assertEqual(routes[(True, DraftRoute.CORPORATE)], 0)
                self.assertGreater(routes[(False, DraftRoute.CORPORATE)], 0, "本指名では社会人を指名する")

    def test_picking_or_bidding_a_corporate_player_in_the_development_rounds_is_rejected(self) -> None:
        for rule in (DraftOrderRule.NPB, DraftOrderRule.ALL_LOTTERY):
            with self.subTest(rule=rule):
                state = meeting(self.TEAMS, rule)
                self._until_development(state)
                turn = state.next_turn()
                assert turn is not None and turn.is_development
                taken = {selection.prospect_key for selection in state.selections}
                corporate = next(
                    p for p in state.pool.prospects if p.route is DraftRoute.CORPORATE and p.key not in taken
                )
                kind = DraftActionKind.BID if turn.is_bid else DraftActionKind.PICK
                before = list(state.actions)
                with self.assertRaisesRegex(InvalidDraftAction, "社会人の選手は育成で指名できません"):
                    state.apply(DraftAction(kind, turn.team_id, corporate.key))
                self.assertEqual(state.actions, before, "何も変えない")
                state.apply(state.ai_action())

    def test_a_corporate_player_can_be_picked_in_the_main_rounds(self) -> None:
        state = meeting(self.TEAMS)
        turn = state.next_turn()
        assert turn is not None and not turn.is_development
        corporate = next(p for p in state.pool.prospects if p.route is DraftRoute.CORPORATE)
        kind = DraftActionKind.BID if turn.is_bid else DraftActionKind.PICK
        state.apply(DraftAction(kind, turn.team_id, corporate.key))

    def test_a_prospect_added_in_the_development_rounds_is_not_corporate(self) -> None:
        pool = draw_pool({Position.PITCHER: 1}, world_seed=1, year=YEAR, used_names=set())
        added = pool
        for _ in range(60):
            added = added.with_extra(Position.PITCHER, development=True)
        self.assertTrue(all(p.allows_development_pick for p in added.prospects[len(pool) :]))
        main = pool
        for _ in range(60):
            main = main.with_extra(Position.PITCHER)
        self.assertTrue(any(not p.allows_development_pick for p in main.prospects[len(pool) :]), "前提")

    def test_prospects_added_in_the_development_rounds_are_the_same_on_replay(self) -> None:
        """候補が尽きて育成の巡で足した候補は社会人でなく、行動の並びから作り直しても同じ候補になる。"""
        teams = [club(team_id, target=25) for team_id in (1, 2)]
        slots = [DraftSlot.of(team, plan_team_draft(team)) for team in teams]
        pool = draw_pool({Position.PITCHER: 1}, world_seed=1, year=YEAR, used_names=set())
        strategy = strategy_for(DraftOrderRule.WAIVER, DraftStandings(leagues=((1, 2),)), world_seed=1, year=YEAR)
        state = DraftState.open(strategy, slots, pool)
        added_in_development: list[int] = []
        while (turn := state.next_turn()) is not None:
            action = state.ai_action()
            if turn.is_development and action.extra_position is not None:
                assert action.prospect_key is not None
                added_in_development.append(action.prospect_key)
            state.apply(action)
        self.assertTrue(added_in_development, "前提: 育成の巡で候補を足す")
        for key in added_in_development:
            self.assertTrue(state.pool.get(key).allows_development_pick)
        replay = DraftState.open(strategy, slots, pool, state.actions)
        self.assertEqual(replay.selections, state.selections)
        self.assertEqual(replay.pool, state.pool)


class DevelopmentRookieTest(unittest.TestCase):
    def test_development_rookies_join_as_developmental_with_three_digit_numbers(self) -> None:
        result = run([club(1, target=25), club(2, target=25)])
        development = [d for d in result.draftees if d.is_development]
        self.assertEqual(len(development), 2 * MAX_DEVELOPMENT_PICKS)
        for draftee in development:
            self.assertIs(draftee.contract, ContractStatus.DEVELOPMENTAL)
            self.assertIs(draftee.acquisition_route, AcquisitionRoute.DEVELOPMENTAL_DRAFT)
            ContractStatus.DEVELOPMENTAL.ensure_number_fits(JerseyNumber(draftee.number))
            self.assertTrue(100 <= int(draftee.number) <= 199)
            assert draftee.profile.draft is not None
            self.assertTrue(draftee.profile.draft.is_development)
            self.assertFalse(draftee.profile.is_foreign_player)
        for draftee in result.draftees:
            if not draftee.is_development:
                self.assertIs(draftee.contract, ContractStatus.REGISTERED)

    def test_registered_rookies_keep_their_numbers(self) -> None:
        """育成の背番号は本指名と外国人の後に引くので、育成を足しても支配下の新人の背番号は変わらない。"""
        plain = list(run([club(1, target=0), club(2, target=0)]).draftees)
        with_development = [d for d in run([club(1, target=25), club(2, target=25)]).draftees if not d.is_development]
        self.assertEqual([(d.name, d.number) for d in plain], [(d.name, d.number) for d in with_development])


class AnchorPopulationTest(unittest.TestCase):
    """錨の写像は支配下で決め、育成にも同じ写像を当てる（2026-10-12 ユーザー決定）。"""

    def test_rescale_uses_only_the_marked_values_for_the_mapping(self) -> None:
        values = [40, 50, 60, 10, 20]
        marked = [True, True, True, False, False]
        moved = rescale(values, 50.0, 10.0, population=marked)
        self.assertEqual(moved[:3], rescale(values[:3], 50.0, 10.0))
        self.assertEqual(moved, sorted(moved[:3]) + moved[3:], "順位は保つ")
        self.assertLess(moved[3], moved[4])
        self.assertEqual(rescale(values, 50.0, 10.0, population=None), rescale(values, 50.0, 10.0))

    def test_adding_weak_developmental_players_does_not_move_the_registered(self) -> None:
        rng = make_random(3)
        registered = [
            BatterRatings(rng.randint(25, 75), rng.randint(25, 75), rng.randint(25, 75), rng.randint(25, 75), 50)
            for _ in range(200)
        ]
        weak = [replace(item, contact=max(1, item.contact - 20)) for item in registered[:60]]
        alone = spread_ratings(registered)
        together = spread_ratings([*registered, *weak], population=[True] * 200 + [False] * 60)
        self.assertEqual(together[:200], alone)
        everyone = spread_ratings([*registered, *weak])
        self.assertNotEqual(everyone[:200], alone, "前提: 母集団に入れると支配下が押し上げられる")

    def test_pitchers_and_batters_are_marked_separately(self) -> None:
        mixed: list[BatterRatings | PitcherRatings] = [
            BatterRatings(50, 50, 50, 50, 50),
            PitcherRatings(50, 50, 50, 50),
        ]
        self.assertEqual(spread_ratings(mixed, population=[True, False]), mixed, "人数が足りない区分は触らない")


if __name__ == "__main__":
    unittest.main()
