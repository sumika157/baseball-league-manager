"""ドラフト会議（`domain/pennant/draft_meeting.py`）。DB 不要。

候補・スカウトの推定・入札と抽選・AI・行動の並びから導く状態を見る。
"""

from __future__ import annotations

import unittest
from collections import Counter
from statistics import fmean, pstdev

from myapp.domain.exceptions import InvalidDraftAction
from myapp.domain.pennant import draft
from myapp.domain.pennant.draft import DraftTeam, plan_team_draft
from myapp.domain.pennant.draft_meeting import (
    SCOUT_STEP,
    TASTE_GROWTH,
    TASTE_LIKING_SD,
    TASTE_PITCHER,
    DraftAction,
    DraftActionKind,
    DraftContext,
    DraftSlot,
    DraftState,
    DraftTaste,
    draft_liking,
    draft_taste,
    draw_pool,
    future_value,
    run_draft,
    scouting_report,
    scouting_value,
)
from myapp.domain.pennant.draft_order import DraftOrderRule, DraftStandings, strategy_for
from myapp.domain.pennant.roster import OPENING_REGISTERED_BAND
from myapp.domain.simulation.ratings import GrowthType
from myapp.domain.value_objects import Position
from myapp.domain.virtual_players import generator

YEAR = 2027


def club(team_id: int, *, size: int | None = None, counts: dict[Position, int] | None = None) -> DraftTeam:
    """帯の中（59人。ふつうの新人6人）の球団。外国人は目標に達している（新人は全員が日本人）。"""
    default = generator.largest_remainder(
        size if size is not None else OPENING_REGISTERED_BAND.high - draft.USUAL_DRAFTEES - 1,
        generator.POSITION_RATIOS,
    )
    return DraftTeam(
        team_id=team_id,
        position_counts=default if counts is None else counts,
        foreign_count=4,
        foreign_limit=4,
        used_numbers=frozenset(),
        surname_counts={},
        registered_limit=70,
    )


def standings(team_ids: list[int]) -> DraftStandings:
    """1リーグ。`team_ids` は1位から順。"""
    return DraftStandings(leagues=(tuple(team_ids),))


def meeting(
    teams: list[DraftTeam], rule: DraftOrderRule = DraftOrderRule.NPB, *, seed: int = 1, actions=()
) -> DraftState:
    ids = [team.team_id for team in teams]
    slots = [DraftSlot.of(team, plan_team_draft(team)) for team in teams]
    planned = Counter(position for slot in slots for position in slot.planned)
    pool = draw_pool(planned, world_seed=seed, year=YEAR, used_names=set())
    return DraftState.open(strategy_for(rule, standings(ids), world_seed=seed, year=YEAR), slots, pool, actions)


def run(teams: list[DraftTeam], rule: DraftOrderRule = DraftOrderRule.NPB, *, seed: int = 1):
    context = DraftContext(rule, standings([team.team_id for team in teams]))
    return run_draft(teams, context, year=YEAR, world_seed=seed, used_names=set())


class AutoCompleteTest(unittest.TestCase):
    """会議を始めなかった年は、全球団を AI として最後まで進める（締める処理の経路）。"""

    def test_every_club_picks_its_maximum_at_the_planned_positions(self) -> None:
        teams = [club(team_id) for team_id in range(1, 13)]
        for rule in DraftOrderRule:
            with self.subTest(rule=rule):
                state = meeting(teams, rule)
                state.auto_complete()
                self.assertTrue(state.is_complete)
                self.assertIsNone(state.next_turn())
                for team in teams:
                    plan = plan_team_draft(team)
                    rookies = state.drafted()[team.team_id]
                    self.assertEqual(len(rookies), plan.japanese_count)
                    self.assertEqual(Counter(r.position for r, _ in rookies), Counter(plan.japanese_positions))
                    self.assertEqual([pick.round for _, pick in rookies], list(range(1, plan.japanese_count + 1)))

    def test_a_prospect_goes_to_one_club_only(self) -> None:
        state = meeting([club(team_id) for team_id in range(1, 13)])
        state.auto_complete()
        keys = [selection.prospect_key for selection in state.selections]
        self.assertEqual(len(keys), len(set(keys)))

    def test_the_same_world_and_year_give_the_same_meeting(self) -> None:
        teams = [club(team_id) for team_id in range(1, 9)]
        first, second = run(teams), run(teams)
        self.assertEqual(first.draftees, second.draftees)
        self.assertEqual(first.lotteries, second.lotteries)
        self.assertNotEqual(first.draftees, run(teams, seed=2).draftees)

    def test_foreign_rookies_are_made_outside_the_meeting(self) -> None:
        """NPB の会議に外国人はいない。外国人は会議の外で作り、ドラフトの指名を持たない。"""
        target = DraftTeam(
            team_id=1,
            position_counts=generator.largest_remainder(59, generator.POSITION_RATIOS),
            foreign_count=1,
            foreign_limit=4,
            used_numbers=frozenset(),
            surname_counts={},
            registered_limit=70,
        )
        result = run([target, club(2)])
        foreign = [d for d in result.draftees if d.team_id == 1 and d.is_foreign]
        self.assertEqual(len(foreign), 3)
        self.assertTrue(all(d.profile.draft is None for d in foreign))
        japanese = [d for d in result.draftees if d.team_id == 1 and not d.is_foreign]
        self.assertEqual([d.profile.draft.round for d in japanese if d.profile.draft], [1, 2, 3])

    def test_a_club_missing_from_the_standings_is_refused(self) -> None:
        context = DraftContext(DraftOrderRule.NPB, standings([1]))
        with self.assertRaises(ValueError):
            run_draft([club(1), club(2)], context, year=YEAR, world_seed=1, used_names=set())


class BiddingTest(unittest.TestCase):
    """NPB 方式の1巡目: 同時入札、重複は抽選で1球団、外れは同じ巡で再入札（外れ1位）。"""

    def test_each_club_gets_exactly_one_first_round_pick_and_lotteries_pick_a_bidder(self) -> None:
        state = meeting([club(team_id) for team_id in range(1, 13)])
        state.auto_complete()
        first = Counter(s.team_id for s in state.selections if s.round_number == 1)
        self.assertEqual(set(first.values()), {1})
        self.assertEqual(len(first), 12)
        for lottery in state.lotteries:
            self.assertGreaterEqual(len(lottery.bidders), 2)
            self.assertIn(lottery.winner, lottery.bidders)
            won = next(s for s in state.selections if s.prospect_key == lottery.prospect_key)
            self.assertEqual(won.team_id, lottery.winner)

    def test_scouting_differs_so_bids_do_not_always_gather_on_one_prospect(self) -> None:
        """球団ごとにスカウトの見立てが違うので、1回目の入札がいつも1人に集まるわけではない（同じ位置を計画した12球団）。"""
        spread = 0
        for seed in range(1, 6):
            state = meeting([club(team_id) for team_id in range(1, 13)], seed=seed)
            state.auto_complete()
            won_alone = 12 - sum(len(lot.bidders) for lot in state.lotteries if lot.attempt == 1)
            targets = won_alone + sum(1 for lot in state.lotteries if lot.attempt == 1)
            spread += targets > 1
        self.assertGreaterEqual(spread, 4)

    def test_a_club_that_loses_the_lottery_bids_again_in_the_same_round(self) -> None:
        state = meeting([club(1), club(2)])
        key = next(p.key for p in state.pool.prospects if p.position is Position.PITCHER)
        first = state.next_turn()
        assert first is not None
        state.apply(DraftAction(DraftActionKind.BID, first.team_id, key))
        second = state.next_turn()
        assert second is not None
        state.apply(DraftAction(DraftActionKind.BID, second.team_id, key))
        (lottery,) = state.lotteries
        loser = ({1, 2} - {lottery.winner}).pop()
        again = state.next_turn()
        assert again is not None
        self.assertEqual((again.team_id, again.round_number, again.is_bid, again.attempt), (loser, 1, True, 2))
        with self.assertRaises(InvalidDraftAction):
            state.apply(DraftAction(DraftActionKind.BID, loser, key))  # 当たった球団の選手

    def test_a_pass_after_the_others_bid_resolves_their_bids(self) -> None:
        """入札の巡で、最後の球団が選択を終えても、ほかの球団の入札は解かれて会議が進む。"""
        state = meeting([club(1, size=62), club(2, size=62)])
        first = state.next_turn()
        assert first is not None
        state.apply(DraftAction(DraftActionKind.BID, first.team_id, 0))
        second = state.next_turn()
        assert second is not None
        state.apply(DraftAction(DraftActionKind.PASS, second.team_id))
        self.assertEqual([(s.team_id, s.round_number) for s in state.selections], [(first.team_id, 1)])
        after = state.next_turn()
        assert after is not None
        self.assertEqual((after.team_id, after.round_number), (first.team_id, 2))

    def test_all_lottery_bids_every_round(self) -> None:
        state = meeting([club(team_id) for team_id in range(1, 7)], DraftOrderRule.ALL_LOTTERY)
        state.auto_complete()
        self.assertTrue(all(action.kind is DraftActionKind.BID for action in state.actions))

    def test_waiver_has_no_lottery(self) -> None:
        state = meeting([club(team_id) for team_id in range(1, 7)], DraftOrderRule.WAIVER)
        state.auto_complete()
        self.assertEqual(state.lotteries, [])
        self.assertTrue(all(action.kind is DraftActionKind.PICK for action in state.actions))


class OrderTest(unittest.TestCase):
    def test_waiver_picks_from_the_bottom_every_round(self) -> None:
        teams = [club(team_id) for team_id in range(1, 5)]
        state = meeting(teams, DraftOrderRule.WAIVER)
        state.auto_complete()
        for round_number in (1, 2, 3):
            picked = [s.team_id for s in state.selections if s.round_number == round_number]
            self.assertEqual(picked, [4, 3, 2, 1])

    def test_npb_snakes_from_the_second_round(self) -> None:
        state = meeting([club(team_id) for team_id in range(1, 5)])
        state.auto_complete()
        self.assertEqual([s.team_id for s in state.selections if s.round_number == 2], [4, 3, 2, 1])
        self.assertEqual([s.team_id for s in state.selections if s.round_number == 3], [1, 2, 3, 4])

    def test_the_front_of_the_waiver_gets_better_rookies(self) -> None:
        """判断21 の書き換えの根拠: 完全ウェーバーは前年の下位ほど良い新人。くじ順は順位に依らない。"""
        teams = [club(team_id) for team_id in range(1, 13)]

        def gap(rule: DraftOrderRule) -> float:
            values: dict[int, list[float]] = {team.team_id: [] for team in teams}
            for seed in range(1, 5):
                for rookie in run(teams, rule, seed=seed).draftees:
                    values[rookie.team_id].append(scouting_value(rookie.ratings))
            # 1リーグの1位から順に 1〜12。下位（7〜12）が逆順の前半
            return fmean(v for t in range(7, 13) for v in values[t]) - fmean(v for t in range(1, 7) for v in values[t])

        waiver, snake = gap(DraftOrderRule.WAIVER), gap(DraftOrderRule.RANDOM_SNAKE)
        self.assertGreater(waiver, 0.5)
        self.assertGreater(waiver, abs(snake))


class InvariantTest(unittest.TestCase):
    """会議の不変条件は `DraftState.apply` が検査する（GM の操作も同じ入口。D3）。"""

    def setUp(self) -> None:
        self.state = meeting([club(1), club(2)], DraftOrderRule.WAIVER)

    def turn(self):
        turn = self.state.next_turn()
        assert turn is not None
        return turn

    def test_only_the_club_on_the_clock_can_act(self) -> None:
        other = 2 if self.turn().team_id == 1 else 1
        with self.assertRaises(InvalidDraftAction):
            self.state.apply(DraftAction(DraftActionKind.PICK, other, 0))

    def test_a_pick_must_be_a_prospect_that_is_still_there(self) -> None:
        with self.assertRaises(InvalidDraftAction):
            self.state.apply(DraftAction(DraftActionKind.PICK, self.turn().team_id, 99_999))
        self.state.apply(DraftAction(DraftActionKind.PICK, self.turn().team_id, 0))
        with self.assertRaises(InvalidDraftAction):
            self.state.apply(DraftAction(DraftActionKind.PICK, self.turn().team_id, 0))

    def test_the_kind_must_match_the_round(self) -> None:
        with self.assertRaises(InvalidDraftAction):
            self.state.apply(DraftAction(DraftActionKind.BID, self.turn().team_id, 0))

    def test_a_club_cannot_pass_before_its_minimum(self) -> None:
        """帯の下端に届かない球団（最少 > 0）は、最少に届くまで選択を終えられない。"""
        low = club(1, size=50)
        state = meeting([low], DraftOrderRule.WAIVER)
        self.assertGreater(DraftSlot.of(low, plan_team_draft(low)).minimum, 0)
        with self.assertRaises(InvalidDraftAction):
            state.apply(DraftAction(DraftActionKind.PASS, 1))

    def test_a_club_that_passes_is_skipped_afterwards(self) -> None:
        """帯の下端に届いている球団（最少 0）は、自分の番で選択を終えられ、以後の巡は飛ばされる。"""
        state = meeting([club(1, size=62), club(2, size=62)], DraftOrderRule.WAIVER)
        turn = state.next_turn()
        assert turn is not None
        state.apply(DraftAction(DraftActionKind.PASS, turn.team_id))
        state.auto_complete()
        self.assertEqual([s for s in state.selections if s.team_id == turn.team_id], [])
        self.assertTrue(state.is_complete)

    def test_a_finished_meeting_refuses_actions(self) -> None:
        self.state.auto_complete()
        with self.assertRaises(InvalidDraftAction):
            self.state.apply(DraftAction(DraftActionKind.PICK, 1, 0))
        with self.assertRaises(InvalidDraftAction):
            self.state.ai_action()

    def test_positions_short_of_the_minimum_must_be_filled(self) -> None:
        """残りの指名で最低人数に届かなくなる位置は選べない（足りない位置はいつでも選べる）。"""
        counts = {Position.PITCHER: 15, Position.CATCHER: 0, Position.INFIELDER: 22, Position.OUTFIELDER: 22}
        short = club(1, counts=counts)
        state = meeting([short], DraftOrderRule.WAIVER)
        slot = state.slots[1]
        self.assertEqual(slot.maximum, draft.USUAL_DRAFTEES, "前提: 6人を指名する")
        pitchers = [p.key for p in state.pool.prospects if p.position is Position.PITCHER]
        catchers = [p.key for p in state.pool.prospects if p.position is Position.CATCHER]
        for key in pitchers[:3]:
            state.apply(DraftAction(DraftActionKind.PICK, 1, key))
        with self.assertRaises(InvalidDraftAction):
            state.apply(DraftAction(DraftActionKind.PICK, 1, pitchers[3]))
        state.apply(DraftAction(DraftActionKind.PICK, 1, catchers[0]))

    def test_slot_must_fit_the_yearly_maximum(self) -> None:
        with self.assertRaises(ValueError):
            DraftSlot(team_id=1, minimum=0, maximum=draft.MAX_DRAFTEES + 1, planned=(), base_counts={})


class ReplayTest(unittest.TestCase):
    """会議の状態は保存しない。行動の並びから作り直すと、同じ次の番・同じ指名になる（D3 の前提）。"""

    def test_rebuilding_from_the_actions_gives_the_same_state(self) -> None:
        teams = [club(team_id) for team_id in range(1, 9)]
        full = meeting(teams)
        applied = full.auto_complete()
        for cut in (0, 3, 8, 15, len(applied) // 2, len(applied)):
            with self.subTest(cut=cut):
                partial = meeting(teams, actions=applied[:cut])
                rest = partial.auto_complete()
                self.assertEqual(applied[:cut] + rest, applied)
                self.assertEqual(partial.selections, full.selections)
                self.assertEqual(partial.lotteries, full.lotteries)

    def test_advance_ai_stops_at_the_managed_club(self) -> None:
        state = meeting([club(team_id) for team_id in range(1, 5)], DraftOrderRule.WAIVER)
        state.advance_ai(stop_at=2)
        turn = state.next_turn()
        assert turn is not None
        self.assertEqual(turn.team_id, 2)

    def test_an_exhausted_position_gets_a_prospect_added_on_the_spot(self) -> None:
        """候補が尽きた位置は、その場で1人足して埋める（締める処理を失敗させない）。"""
        counts = {Position.PITCHER: 20, Position.CATCHER: 0, Position.INFIELDER: 20, Position.OUTFIELDER: 20}
        teams = [club(team_id, counts=counts) for team_id in (1, 2)]
        slots = [DraftSlot.of(team, plan_team_draft(team)) for team in teams]
        pool = draw_pool({Position.PITCHER: 1}, world_seed=1, year=YEAR, used_names=set())
        catchers = sum(1 for p in pool.prospects if p.position is Position.CATCHER)
        needed = sum(Counter(slot.planned)[Position.CATCHER] for slot in slots)
        self.assertGreater(needed, catchers, "前提: 捕手の候補が足りない")
        strategy = strategy_for(DraftOrderRule.WAIVER, standings([1, 2]), world_seed=1, year=YEAR)
        state = DraftState.open(strategy, slots, pool)
        actions = state.auto_complete()
        self.assertTrue(any(action.extra_position is Position.CATCHER for action in actions))
        self.assertEqual(len(state.pool), len(pool) + sum(1 for a in actions if a.extra_position is not None))
        replay = DraftState.open(strategy, slots, pool, actions)
        self.assertEqual(replay.selections, state.selections)
        self.assertEqual(replay.pool, state.pool)


class ProspectTest(unittest.TestCase):
    def test_the_pool_follows_the_planned_positions(self) -> None:
        pool = draw_pool({Position.PITCHER: 10, Position.CATCHER: 2}, world_seed=1, year=YEAR, used_names=set())
        counts = Counter(p.position for p in pool.prospects)
        self.assertEqual(counts[Position.PITCHER], 40)
        self.assertEqual(counts[Position.CATCHER], 8)
        self.assertEqual(counts[Position.OUTFIELDER], 4, "どの球団も計画していない位置にも少し置く")
        self.assertEqual(len(draw_pool({}, world_seed=1, year=YEAR, used_names=set())), 0)

    def test_the_pool_is_rebuilt_the_same_and_avoids_the_names_in_the_world(self) -> None:
        planned = {Position.PITCHER: 5, Position.INFIELDER: 3}
        first = draw_pool(planned, world_seed=3, year=YEAR, used_names=set())
        self.assertEqual(first.signature, draw_pool(planned, world_seed=3, year=YEAR, used_names=set()).signature)
        names = {p.rookie.name for p in first.prospects}
        second = draw_pool(planned, world_seed=3, year=YEAR, used_names=names)
        self.assertFalse(names & {p.rookie.name for p in second.prospects})
        self.assertNotEqual(
            first.signature, draw_pool(planned, world_seed=3, year=YEAR + 1, used_names=set()).signature
        )

    def test_prospects_are_japanese_amateurs_without_a_pick_yet(self) -> None:
        pool = draw_pool({Position.PITCHER: 5}, world_seed=1, year=YEAR, used_names=set())
        for prospect in pool.prospects:
            self.assertFalse(prospect.rookie.profile.is_foreign_player)
            self.assertIsNone(prospect.rookie.profile.draft)
            self.assertIsNot(prospect.route, draft.DraftRoute.FOREIGN)


class TasteTest(unittest.TestCase):
    """1位の好み（#302）: 球団の色（`DraftTaste`）と惚れ込み（`draft_liking`）。本指名の1巡目の入札にだけ効く。"""

    def test_a_club_has_the_same_taste_every_time_and_clubs_differ(self) -> None:
        tastes = [draft_taste(1, team_id) for team_id in range(1, 49)]
        self.assertEqual(tastes[0], draft_taste(1, 1))
        self.assertNotEqual(tastes[0], draft_taste(2, 1))
        self.assertGreater(len({taste.growth_weight for taste in tastes}), 40)
        for taste in tastes:
            self.assertAlmostEqual(sum(taste.route_bonus.values()), 0.0, places=9)
            self.assertEqual(
                set(taste.route_bonus),
                {draft.DraftRoute.HIGH_SCHOOL, draft.DraftRoute.UNIVERSITY, draft.DraftRoute.CORPORATE},
            )
            self.assertLessEqual(abs(taste.growth_weight - 1.0), TASTE_GROWTH)
            self.assertLessEqual(abs(taste.pitcher_bonus), TASTE_PITCHER)

    def test_liking_is_fixed_per_club_and_prospect_and_spreads_by_the_set_width(self) -> None:
        self.assertEqual(draft_liking(1, YEAR, 3, 10), draft_liking(1, YEAR, 3, 10))
        likings = [draft_liking(1, YEAR, team_id, key) for team_id in range(1, 49) for key in range(100)]
        self.assertAlmostEqual(fmean(likings), 0.0, delta=0.3)
        self.assertAlmostEqual(pstdev(likings), TASTE_LIKING_SD, delta=0.3)

    def test_the_taste_moves_the_evaluation_by_its_parts(self) -> None:
        pool = draw_pool({Position.PITCHER: 10, Position.INFIELDER: 10}, world_seed=1, year=YEAR, used_names=set())
        high_school = next(
            p for p in pool.prospects if p.route is draft.DraftRoute.HIGH_SCHOOL and p.position.is_pitcher
        )
        estimate = scouting_report(high_school, 1, world_seed=1, year=YEAR)
        future = future_value(high_school, estimate)
        self.assertAlmostEqual(DraftTaste().evaluate(high_school, future), future)
        taste = DraftTaste(route_bonus={draft.DraftRoute.HIGH_SCHOOL: 1.5}, growth_weight=1.25, pitcher_bonus=-2.0)
        growth = future - scouting_value(estimate)
        self.assertGreater(growth, 10)  # 高校生は伸びが大きい（重みが効く）
        self.assertAlmostEqual(
            taste.evaluate(high_school, future, liking=0.5), future + 0.25 * growth + 1.5 - 2.0 + 0.5
        )

    def test_the_taste_is_used_only_for_the_first_round_bids(self) -> None:
        """2巡目以降と、1巡目が入札でない規則（完全ウェーバー）は将来値で選ぶ（新人の分布と、前の球団ほど良い新人を保つ）。"""
        teams = [club(team_id) for team_id in range(1, 5)]
        npb = meeting(teams)
        self.assertTrue(npb._tasted())
        replay = meeting(teams)
        while (turn := replay.next_turn()) is not None and turn.round_number == 1:
            replay.apply(replay.ai_action())
        self.assertFalse(replay._tasted())
        self.assertFalse(meeting(teams, DraftOrderRule.WAIVER)._tasted())

    def test_the_taste_is_used_for_the_rebid_after_a_lost_lottery(self) -> None:
        """外れ1位の再入札（1巡目の2回目）も1位の入札なので、好みを入れる。"""
        state = meeting([club(1), club(2)])
        key = next(p.key for p in state.pool.prospects if p.position is Position.PITCHER)
        for _ in range(2):
            turn = state.next_turn()
            assert turn is not None
            state.apply(DraftAction(DraftActionKind.BID, turn.team_id, key))
        rebid = state.next_turn()
        assert rebid is not None
        self.assertEqual((rebid.round_number, rebid.attempt), (1, 2))
        self.assertTrue(state._tasted())

    def test_first_round_bids_do_not_gather_on_one_prospect(self) -> None:
        """再発防止（#302）: 8リーグ48球団の会議で、1位の入札が1人に集まるのは多くても十数球団。

        シードは好みを入れる前に集中がひどかった2つ（最多 40球団・41球団。入れた後は 9・14球団）。
        """
        leagues = tuple(tuple(range(1 + 6 * league, 7 + 6 * league)) for league in range(8))
        for seed in (1, 7):
            teams = [club(team_id, size=58 - team_id % 4) for team_id in range(1, 49)]
            slots = [DraftSlot.of(team, plan_team_draft(team)) for team in teams]
            planned = Counter(position for slot in slots for position in slot.planned)
            pool = draw_pool(planned, world_seed=seed, year=YEAR, used_names=set())
            strategy = strategy_for(DraftOrderRule.NPB, DraftStandings(leagues=leagues), world_seed=seed, year=YEAR)
            state = DraftState.open(strategy, slots, pool)
            state.auto_complete()
            first = [lot for lot in state.lotteries if lot.round_number == 1 and not lot.is_development]
            with self.subTest(seed=seed):
                self.assertLessEqual(max((len(lot.bidders) for lot in first), default=1), 18)


class ScoutingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.pool = draw_pool({Position.PITCHER: 5, Position.INFIELDER: 5}, world_seed=1, year=YEAR, used_names=set())

    def test_estimates_are_rounded_to_steps_and_hide_the_growth_type(self) -> None:
        for prospect in self.pool.prospects:
            report = scouting_report(prospect, 1, world_seed=1, year=YEAR)
            self.assertIs(report.growth, GrowthType.NORMAL)
            for name in report.LABELS:
                self.assertEqual(getattr(report, name) % SCOUT_STEP, 0)

    def test_each_club_sees_differently_but_the_same_club_always_the_same(self) -> None:
        reports = {
            team_id: [scouting_report(p, team_id, world_seed=1, year=YEAR) for p in self.pool.prospects]
            for team_id in (1, 2)
        }
        self.assertNotEqual(reports[1], reports[2])
        self.assertEqual(reports[1], [scouting_report(p, 1, world_seed=1, year=YEAR) for p in self.pool.prospects])


if __name__ == "__main__":
    unittest.main()
