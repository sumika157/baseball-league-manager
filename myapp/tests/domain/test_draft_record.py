"""ドラフト会議の記録（`domain/pennant/draft_record.py`）と、GM が1巡ずつ進める会議のための domain の部品
（#297 D3）。DB 不要。

候補の指紋（身元だけ）・人数の幅（`meeting_slots`）・記録の再生・状態・確定した動き・指名できるかの判定・
会議の前に決める入力を変えられない理由を見る。会議のエンジンそのものは `test_draft_meeting.py`。
"""

from __future__ import annotations

import unittest
from collections import Counter
from dataclasses import replace
from datetime import date

from myapp.domain.exceptions import DraftChanged, DraftConflict, InvalidDraftAction
from myapp.domain.pennant import draft
from myapp.domain.pennant.draft import MAX_DEVELOPMENT_PICKS, DraftTeam, plan_team_draft
from myapp.domain.pennant.draft_meeting import (
    DraftAction,
    DraftActionKind,
    DraftContext,
    DraftSlot,
    DraftState,
    ProspectPool,
    draw_pool,
    meeting_slots,
    run_draft,
)
from myapp.domain.pennant.draft_order import DraftOrderRule, DraftStandings, strategy_for
from myapp.domain.pennant.draft_record import DraftMeetingStatus, DraftRecord, pre_draft_input_blocker
from myapp.domain.pennant.roster import OPENING_REGISTERED_BAND
from myapp.domain.simulation.ratings import GrowthType, Potential
from myapp.domain.value_objects import Position
from myapp.domain.virtual_players import generator

YEAR = 2027
SEED = 5
GM = 3


def club(team_id: int, *, development: int = 0, target: int = 20) -> DraftTeam:
    """帯の中（59人。ふつうの新人6人）の球団。外国人は目標に達している。育成は `development` 人・目標 `target`。"""
    size = OPENING_REGISTERED_BAND.high - draft.USUAL_DRAFTEES - 1
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


def teams() -> list[DraftTeam]:
    # 育成は目標まで2人（計画の人数 2 < 最多 6）
    return [club(team_id, development=18) for team_id in range(1, 7)]


def standings(team_ids: list[int]) -> DraftStandings:
    return DraftStandings(leagues=(tuple(team_ids),))


def setup(
    rule: DraftOrderRule = DraftOrderRule.NPB, *, managed: int | None = GM
) -> tuple[tuple[DraftSlot, ...], ProspectPool, object]:
    clubs = teams()
    plans = {team.team_id: plan_team_draft(team) for team in clubs}
    slots = meeting_slots(clubs, plans, managed_team_id=managed)
    planned = Counter(position for slot in slots for position in slot.planned)
    pool = draw_pool(planned, world_seed=SEED, year=YEAR, used_names=set())
    strategy = strategy_for(rule, standings([team.team_id for team in clubs]), world_seed=SEED, year=YEAR)
    return slots, pool, strategy


def open_state(rule: DraftOrderRule = DraftOrderRule.NPB, actions=(), *, managed: int | None = GM) -> DraftState:
    slots, pool, strategy = setup(rule, managed=managed)
    return DraftState.open(strategy, slots, pool, actions)  # type: ignore[arg-type]


class SignatureTest(unittest.TestCase):
    """候補の指紋は身元だけ（番号・名前・位置・経歴・生年月日。#297 Q2）。"""

    def setUp(self) -> None:
        self.pool = draw_pool({Position.PITCHER: 4, Position.CATCHER: 1}, world_seed=SEED, year=YEAR, used_names=set())

    def _changed(self, index: int, **rookie_changes: object) -> ProspectPool:
        prospect = self.pool.prospects[index]
        rookie = replace(prospect.rookie, **rookie_changes)  # type: ignore[arg-type]
        prospects = list(self.pool.prospects)
        prospects[index] = replace(prospect, rookie=rookie)
        return replace(self.pool, prospects=tuple(prospects))

    def test_the_same_world_and_year_give_the_same_signature(self) -> None:
        again = draw_pool({Position.PITCHER: 4, Position.CATCHER: 1}, world_seed=SEED, year=YEAR, used_names=set())
        self.assertEqual(self.pool.signature, again.signature)

    def test_hidden_values_and_ratings_do_not_change_the_signature(self) -> None:
        """隠し値（伸びしろ・成長型）や能力の式を後から変えても、途中の会議は壊れない。"""
        ratings = self.pool.prospects[0].rookie.ratings
        first = next(iter(ratings.LABELS))
        for changed in (
            replace(ratings, potential=Potential(99)),
            replace(ratings, potential=None),
            replace(ratings, growth=next(g for g in GrowthType if g is not ratings.growth)),
            replace(ratings, **{first: getattr(ratings, first) + 7}),
        ):
            with self.subTest(changed=changed):
                self.assertEqual(self._changed(0, ratings=changed).signature, self.pool.signature)

    def test_the_identity_changes_the_signature(self) -> None:
        rookie = self.pool.prospects[0].rookie
        profile = rookie.profile
        assert profile.birth_date is not None
        for changes in (
            {"name": rookie.name + "改"},
            {"position": Position.CATCHER},
            {"profile": replace(profile, birth_date=date(profile.birth_date.year - 1, 1, 1))},
            {"profile": replace(profile, high_school=profile.high_school + "分校")},
        ):
            with self.subTest(changes=changes):
                self.assertNotEqual(self._changed(0, **changes).signature, self.pool.signature)

    def test_a_different_route_changes_the_signature(self) -> None:
        rookie = self.pool.prospects[1].rookie
        other = next(route for route in draft.DraftRoute if route is not rookie.route)
        self.assertNotEqual(self._changed(1, route=other).signature, self.pool.signature)


class MeetingSlotsTest(unittest.TestCase):
    """人数の幅は `meeting_slots` が唯一の出典。GM の球団だけ育成の最多が `MAX_DEVELOPMENT_PICKS`。"""

    def test_only_the_managed_club_gets_the_development_maximum(self) -> None:
        clubs = teams()
        plans = {team.team_id: plan_team_draft(team) for team in clubs}
        slots = meeting_slots(list(reversed(clubs)), plans, managed_team_id=GM)
        self.assertEqual([slot.team_id for slot in slots], sorted(team.team_id for team in clubs))
        for slot in slots:
            with self.subTest(team=slot.team_id):
                expected = DraftSlot.of(next(t for t in clubs if t.team_id == slot.team_id), plans[slot.team_id])
                if slot.team_id == GM:
                    self.assertEqual(slot.development_max, MAX_DEVELOPMENT_PICKS)
                    self.assertEqual(replace(slot, development_max=expected.development_max), expected)
                else:
                    self.assertEqual(slot, expected)
        self.assertEqual(
            meeting_slots(clubs, plans, managed_team_id=None), tuple(DraftSlot.of(t, plans[t.team_id]) for t in clubs)
        )

    def test_leaving_the_managed_club_to_the_ai_gives_the_same_rookies(self) -> None:
        """会議を始めなかった年・お任せの新人は、受け持つ球団の有無で変わらない
        （AI は育成の計画の人数で選択を終える）。"""
        clubs = teams()
        for rule in DraftOrderRule:
            with self.subTest(rule=rule):
                ids = [team.team_id for team in clubs]
                with_gm = run_draft(
                    clubs,
                    DraftContext(rule, standings(ids), managed_team_id=GM),
                    year=YEAR,
                    world_seed=SEED,
                    used_names=set(),
                )
                without = run_draft(
                    clubs, DraftContext(rule, standings(ids)), year=YEAR, world_seed=SEED, used_names=set()
                )
                self.assertEqual(with_gm.draftees, without.draftees)
                self.assertEqual(with_gm.lotteries, without.lotteries)
                self.assertEqual(with_gm.state.selections, without.state.selections)


class DevelopmentPassTest(unittest.TestCase):
    """AI は育成の計画の人数に達したら選択を終える（GM の球団の最多が計画より多くても）。"""

    def _to_development_turn(self, state: DraftState, team_id: int) -> None:
        while (turn := state.next_turn()) is not None and not (turn.is_development and turn.team_id == team_id):
            state.apply(state.ai_action())

    def test_the_ai_passes_at_the_planned_count(self) -> None:
        state = open_state()
        planned = len(state.slots[GM].development_planned)
        self.assertGreater(MAX_DEVELOPMENT_PICKS, planned, "前提: GM の最多は計画より多い")
        for _ in range(planned):
            self._to_development_turn(state, GM)
            self.assertIs(state.ai_action().kind, DraftActionKind.PICK)
            state.apply(state.ai_action())
        self._to_development_turn(state, GM)
        self.assertEqual(state.ai_action(), DraftAction(DraftActionKind.PASS, GM))

    def test_the_gm_can_pick_beyond_the_plan_up_to_the_maximum(self) -> None:
        state = open_state()
        picks = 0
        while (turn := state.next_turn()) is not None:
            if turn.team_id == GM and turn.is_development:
                choice = next(p for p in state.pool.prospects if state.selectable(GM, p) and p.allows_development_pick)
                state.apply(DraftAction(DraftActionKind.BID if turn.is_bid else DraftActionKind.PICK, GM, choice.key))
                picks += 1
            else:
                state.apply(state.ai_action())
        self.assertEqual(picks, MAX_DEVELOPMENT_PICKS)


class RecordTest(unittest.TestCase):
    def setUp(self) -> None:
        self.slots, self.pool, self.strategy = setup()
        state = DraftState.open(self.strategy, self.slots, self.pool)  # type: ignore[arg-type]
        self.actions = state.advance_ai(stop_at=GM)
        self.record = DraftRecord.start(YEAR, self.pool, reversed(self.slots), self.actions)  # type: ignore[arg-type]

    def test_start_copies_the_signature_and_orders_the_slots(self) -> None:
        self.assertEqual(self.record.signature, self.pool.signature)
        self.assertEqual(self.record.slots, self.slots)
        self.assertEqual(self.record.last_seq, len(self.actions))
        with self.assertRaises(ValueError):
            DraftRecord.start(YEAR + 1, self.pool, self.slots)

    def test_replaying_the_record_gives_the_same_state(self) -> None:
        full = DraftState.open(self.strategy, self.slots, self.pool)  # type: ignore[arg-type]
        rest = full.auto_complete()
        record = DraftRecord.start(YEAR, self.pool, self.slots, rest)
        replay = record.state(self.strategy, self.pool)  # type: ignore[arg-type]
        self.assertEqual(replay.selections, full.selections)
        self.assertEqual(replay.lotteries, full.lotteries)
        self.assertEqual(replay.pool, full.pool)
        self.assertTrue(replay.is_complete)

    def test_a_different_pool_is_a_changed_meeting(self) -> None:
        other = draw_pool({Position.PITCHER: 4}, world_seed=SEED + 1, year=YEAR, used_names=set())
        with self.assertRaises(DraftChanged):
            self.record.state(self.strategy, other)  # type: ignore[arg-type]

    def test_an_action_that_cannot_be_replayed_is_a_changed_meeting(self) -> None:
        broken = replace(self.record, actions=(DraftAction(DraftActionKind.PICK, GM, 0), *self.record.actions))
        with self.assertRaises(DraftChanged):
            broken.state(self.strategy, self.pool)  # type: ignore[arg-type]

    def test_appending_needs_the_last_seq(self) -> None:
        turn = self.record.state(self.strategy, self.pool).next_turn()  # type: ignore[arg-type]
        assert turn is not None
        self.assertEqual(turn.team_id, GM)
        action = DraftAction(DraftActionKind.BID, GM, 0)
        appended = self.record.appended([action], expected_seq=self.record.last_seq)
        self.assertEqual(appended.last_seq, self.record.last_seq + 1)
        with self.assertRaises(DraftConflict):
            self.record.appended([action], expected_seq=self.record.last_seq - 1)

    def test_the_slots_must_match_when_closing(self) -> None:
        self.record.ensure_same_slots(list(reversed(self.slots)))
        changed = (replace(self.slots[0], maximum=self.slots[0].maximum - 1), *self.slots[1:])
        with self.assertRaises(DraftChanged):
            self.record.ensure_same_slots(changed)

    def test_a_record_needs_clubs_once_each_and_actions_only_by_them(self) -> None:
        with self.assertRaises(ValueError):
            DraftRecord(YEAR, "x", ())
        with self.assertRaises(ValueError):
            DraftRecord(YEAR, "x", (self.slots[0], self.slots[0]))
        with self.assertRaises(InvalidDraftAction):
            DraftRecord(YEAR, "x", self.slots, (DraftAction(DraftActionKind.PASS, 999),))


class StatusTest(unittest.TestCase):
    def test_the_status_is_derived_from_the_record_and_the_state(self) -> None:
        slots, pool, strategy = setup()
        self.assertIs(DraftMeetingStatus.of(None, None), DraftMeetingStatus.NOT_STARTED)
        state = DraftState.open(strategy, slots, pool)  # type: ignore[arg-type]
        record = DraftRecord.start(YEAR, pool, slots, state.advance_ai(stop_at=GM))
        self.assertIs(DraftMeetingStatus.of(record, state), DraftMeetingStatus.IN_PROGRESS)
        state.auto_complete()
        self.assertIs(DraftMeetingStatus.of(record, state), DraftMeetingStatus.FINISHED)
        with self.assertRaises(ValueError):
            DraftMeetingStatus.of(record, None)


class MovesTest(unittest.TestCase):
    """確定した動き（画面の「直近の動き」）。保留中の入札は出さない。"""

    def test_pending_bids_are_not_moves_until_the_round_is_resolved(self) -> None:
        state = open_state(DraftOrderRule.NPB)
        state.advance_ai(stop_at=GM)
        self.assertEqual(state.resolved_moves(), (), "GM の入札を待つ間は、入札が出ない")
        turn = state.next_turn()
        assert turn is not None and turn.is_bid
        before = len(state.actions)
        state.apply(DraftAction(DraftActionKind.BID, GM, state.ai_action().prospect_key))
        state.advance_ai(stop_at=GM)
        moves = state.resolved_moves(before)
        self.assertTrue(moves)
        self.assertTrue(all(move.kind is DraftActionKind.BID for move in moves if move.round_number == 1))
        self.assertEqual([move.lottery for move in moves if move.lottery is not None], state.lotteries)
        self.assertTrue(state.lotteries, "前提: 1位の入札が重なって抽選がある")
        self.assertEqual({move.prospect_key for move in moves}, {s.prospect_key for s in state.selections})

    def test_moves_are_filtered_by_seq_and_cover_every_selection(self) -> None:
        state = open_state(DraftOrderRule.WAIVER)
        state.auto_complete()
        moves = state.resolved_moves()
        picks = [move for move in moves if move.kind is not DraftActionKind.PASS]
        self.assertEqual(
            [(m.team_id, m.prospect_key, m.round_number, m.is_development) for m in picks],
            [(s.team_id, s.prospect_key, s.round_number, s.is_development) for s in state.selections],
        )
        self.assertEqual([m.seq for m in moves], sorted(m.seq for m in moves))
        middle = moves[len(moves) // 2].seq
        self.assertTrue(all(move.seq > middle for move in state.resolved_moves(middle)))
        passes = [move for move in moves if move.kind is DraftActionKind.PASS]
        self.assertEqual(len(passes), sum(1 for a in state.actions if a.kind is DraftActionKind.PASS))
        self.assertTrue(all(move.prospect_key is None for move in passes))


class SelectableTest(unittest.TestCase):
    """`selectable` は `apply` の検査と食い違わない。"""

    def _assert_agrees(self, state: DraftState) -> None:
        turn = state.next_turn()
        assert turn is not None
        kind = DraftActionKind.BID if turn.is_bid else DraftActionKind.PICK
        for prospect in state.pool.prospects:
            for team_id in (turn.team_id, next(t for t in state.slots if t != turn.team_id)):
                copy = DraftState.open(state.strategy, list(state.slots.values()), state.pool, state.actions)
                try:
                    copy.apply(DraftAction(kind, team_id, prospect.key))
                    applied = True
                except InvalidDraftAction:
                    applied = False
                with self.subTest(prospect=prospect.key, team=team_id):
                    self.assertEqual(state.selectable(team_id, prospect), applied)

    def test_in_the_main_rounds(self) -> None:
        state = open_state(DraftOrderRule.WAIVER)
        for _ in range(9):
            state.apply(state.ai_action())
        self._assert_agrees(state)

    def test_in_the_development_rounds(self) -> None:
        state = open_state(DraftOrderRule.WAIVER)
        while (turn := state.next_turn()) is not None and not turn.is_development:
            state.apply(state.ai_action())
        state.apply(state.ai_action())
        self._assert_agrees(state)

    def test_nothing_is_selectable_after_the_meeting(self) -> None:
        state = open_state(DraftOrderRule.WAIVER)
        state.auto_complete()
        self.assertFalse(any(state.selectable(GM, p) for p in state.pool.prospects))


class PreDraftInputBlockerTest(unittest.TestCase):
    def test_inputs_before_the_meeting_are_fixed_once_it_starts(self) -> None:
        self.assertIsNone(pre_draft_input_blocker(meeting_started=False))
        self.assertIn("ドラフト会議が始まっている", pre_draft_input_blocker(meeting_started=True) or "")


if __name__ == "__main__":
    unittest.main()
