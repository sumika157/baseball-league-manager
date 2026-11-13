"""GM が開いたドラフト会議を、シーズンを締める計算が使う（#297 D3 T2）。DB 不要。

会議を始めるときと締めるときが同じ関数（`offseason.prepare_meeting`）で人数の幅と候補を作ること、締めるときに
保存した会議を再生すること（終わっていれば保存した指名どおり・途中なら `DraftInProgress`・幅や候補が違えば
`DraftChanged`）、画面の出し分けの判定（`passable`・`round_order`）を見る。
"""

from __future__ import annotations

import copy
import unittest
from dataclasses import replace

from myapp.domain.exceptions import DraftChanged, DraftInProgress, InvalidDraftAction
from myapp.domain.pennant.draft_meeting import (
    DraftAction,
    DraftActionKind,
    DraftContext,
    DraftState,
    meeting_pool,
)
from myapp.domain.pennant.draft_order import DraftOrderRule, DraftStandings
from myapp.domain.pennant.draft_record import DraftRecord
from myapp.domain.pennant.offseason import OffseasonClub, PreparedMeeting, plan_offseason, prepare_meeting
from myapp.domain.value_objects import Position

from .test_offseason import START_YEAR, YEAR, club, player

SEED = 5
GM = 2
POSITIONS = (Position.PITCHER, Position.CATCHER, Position.INFIELDER, Position.OUTFIELDER)


def clubs() -> list[OffseasonClub]:
    """3球団（各40人。位置は順に回す）。"""
    return [
        club(*(player(i + 100 * t, position=POSITIONS[i % len(POSITIONS)]) for i in range(1, 41)), team_id=t)
        for t in (1, 2, 3)
    ]


def context(rule: DraftOrderRule = DraftOrderRule.NPB) -> DraftContext:
    return DraftContext(rule, DraftStandings(leagues=((1, 2, 3),)), managed_team_id=GM)


def prepared(rule: DraftOrderRule = DraftOrderRule.NPB) -> PreparedMeeting:
    return prepare_meeting(
        clubs(), year=YEAR, world_seed=SEED, start_year=START_YEAR, used_names=(), draft=context(rule)
    )


def close(meeting: DraftRecord | None, rule: DraftOrderRule = DraftOrderRule.NPB):
    return plan_offseason(
        clubs(),
        year=YEAR,
        world_seed=SEED,
        start_year=START_YEAR,
        used_names=(),
        draft=context(rule),
        meeting=meeting,
    )


def started(rule: DraftOrderRule = DraftOrderRule.NPB) -> tuple[DraftRecord, DraftState]:
    """会議を始めたとき（GM の番まで AI を進めた記録と状態）。サービスの `start` と同じ手順。"""
    setup = prepared(rule).setup
    state = DraftState.open(setup.strategy, setup.slots, setup.pool)
    actions = state.advance_ai(stop_at=GM)
    return DraftRecord.start(YEAR, setup.pool, setup.slots, actions), state


def names_of(plan, team_id: int) -> list[str]:
    return [draftee.name for draftee in plan.draftees if draftee.team_id == team_id]


class SameSetupTest(unittest.TestCase):
    def test_the_meeting_is_opened_from_the_same_slots_and_pool_as_the_close(self):
        """会議を始めるときの計算は、締める計算と同じ人数の幅と候補（再生した会議の新人が同じになる前提）。"""
        first, second = prepared(), prepared()
        self.assertEqual(first.setup.slots, second.setup.slots)
        self.assertEqual(first.setup.pool.signature, second.setup.pool.signature)
        # 1手ごとに写した幅から作り直す候補も同じ（名前は世界の選手の名前だけで足りる）
        rebuilt = meeting_pool(first.setup.slots, world_seed=SEED, year=YEAR, used_names=())
        self.assertEqual(rebuilt.signature, first.setup.pool.signature)

    def test_a_meeting_left_to_the_ai_drafts_the_same_rookies_as_no_meeting(self):
        """会議を始めて残りを全部お任せにした年は、会議を開かなかった年と同じ新人になる。"""
        for rule in DraftOrderRule:
            with self.subTest(rule=rule):
                record, state = started(rule)
                record = record.appended(state.auto_complete(), expected_seq=record.last_seq)
                self.assertEqual(close(record, rule).draftees, close(None, rule).draftees)


class ReplayAtCloseTest(unittest.TestCase):
    def test_the_gms_picks_are_the_rookies(self):
        """締めるときは、GM が選んだ候補が GM の球団の新人になる（AI が選んだ候補ではない）。

        抽選で外れないように、入札の無い規則（完全ウェーバー）で見る。"""
        rule = DraftOrderRule.WAIVER
        record, state = started(rule)
        turn = state.next_turn()
        assert turn is not None and turn.team_id == GM and not turn.is_bid
        ai_choice = state.ai_action().prospect_key
        chosen = next(
            prospect
            for prospect in reversed(state.pool.prospects)
            if prospect.key != ai_choice and state.selectable(GM, prospect)
        )
        state.apply(DraftAction(DraftActionKind.PICK, GM, chosen.key))
        state.auto_complete()
        record = record.appended(state.actions[record.last_seq :], expected_seq=record.last_seq)

        plan = close(record, rule)

        self.assertIn(chosen.rookie.name, names_of(plan, GM))
        self.assertNotEqual(names_of(plan, GM), names_of(close(None, rule), GM))

    def test_an_unfinished_meeting_is_not_closed(self):
        record, _ = started()
        with self.assertRaises(DraftInProgress):
            close(record)

    def test_slots_that_changed_since_the_start_are_a_changed_meeting(self):
        record, state = started()
        record = record.appended(state.auto_complete(), expected_seq=record.last_seq)
        changed = replace(record, slots=(replace(record.slots[0], development_max=0), *record.slots[1:]))
        with self.assertRaises(DraftChanged):
            close(changed)

    def test_a_pool_that_changed_since_the_start_is_a_changed_meeting(self):
        record, state = started()
        record = record.appended(state.auto_complete(), expected_seq=record.last_seq)
        with self.assertRaises(DraftChanged):
            close(replace(record, signature="0" * 32))


class ScreenHelpersTest(unittest.TestCase):
    def test_passable_agrees_with_apply(self):
        """「選択終了」を出せるか（`passable`）は、`apply` が選択終了を受けるかと食い違わない。"""
        _, state = started(DraftOrderRule.WAIVER)
        checked = 0
        while (turn := state.next_turn()) is not None and checked < 200:
            passable = state.passable(turn.team_id)
            trial = copy.deepcopy(state)
            try:
                trial.apply(DraftAction(DraftActionKind.PASS, turn.team_id))
                accepted = True
            except InvalidDraftAction:
                accepted = False
            self.assertEqual(passable, accepted, f"{turn}")
            other = next(team_id for team_id in state.slots if team_id != turn.team_id)
            self.assertFalse(state.passable(other), "番でない球団は終えられない")
            state.apply(state.ai_action())
            checked += 1

    def test_the_round_order_is_the_teams_of_the_meeting_in_the_strategys_order(self):
        record, state = started(DraftOrderRule.WAIVER)
        # 完全ウェーバーの1巡目は最下位から（1リーグ・球団 id の順の最終順位なので 3 → 2 → 1）
        self.assertEqual(state.round_order, (3, 2, 1))
        state.auto_complete()
        self.assertEqual(state.round_order, (), "会議が終われば空")
        self.assertTrue(record.team_ids >= {1, 2, 3})


if __name__ == "__main__":
    unittest.main()
