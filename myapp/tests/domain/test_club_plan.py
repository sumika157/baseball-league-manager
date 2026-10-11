"""球団の編成（`ClubPlan`）の単体テスト。Django も DB も使わない。

守りたいこと:
- 上書きを決めるときに、不変条件（1軍29人まで・外国人の登録枠・在籍している選手だけ・オーダー9人で
  守備位置が9つ・捕手の枠は捕手・ローテーションは1軍の投手）を検査する
- 検査を通らなかった上書きは入らない（途中まで書き換えない）
- 決めたあとにロスターが変わって使えなくなった上書きは、例外にせず**その区画だけ自動に落ち**、理由を返す
- 上書きが無ければ、AI 監督の自動編成と同じになる
"""

from unittest import TestCase

from myapp.domain.exceptions import DomainError, ForeignPlayerQuotaExceeded, InvalidClubPlan, InvalidRoster
from myapp.domain.pennant.club_plan import (
    LINEUP_POSITION_ORDER,
    LINEUP_POSITIONS,
    ClubLimits,
    ClubMember,
    ClubPlan,
    LineupChoice,
    PlanSection,
    arrange_lineup,
    members_of,
    resolve_club,
    strictest_game_limit,
    unfilled_position,
)
from myapp.domain.simulation.manager import (
    MIN_ACTIVE_PITCHERS,
    MIN_ROTATION_SIZE,
    ROTATION_SIZE,
    ClubRoster,
    ForeignQuota,
    can_play,
    choose_active_roster,
    choose_lineup,
    plan_pitching_staff,
)
from myapp.domain.value_objects import FieldingPosition, Position

from .test_ai_manager import full_pool

FP = FieldingPosition
LIMITS = ClubLimits()
# 手動のローテーション（下限の5人。1軍にいる投手）
ROTATION = [110, 111, 112, 113, 114]
# 守備位置の枠を満たす野手9人（捕1・内4・外3・指名打者1）と、もう1人（外野手）
NINE = [1, 4, 5, 6, 7, 12, 13, 14, 19]
TEN = [*NINE, 15]


def ids(roster):
    return {b.player_id for b in roster.batters} | {p.player_id for p in roster.pitchers}


class ClubPlanCase(TestCase):
    def setUp(self):
        # 野手20人（1〜3 捕手・4〜11 内野・12〜18 外野・19〜20 指名打者）と投手19人（101〜119）。能力は若い番号が高い
        self.pool = full_pool()
        self.members = members_of(self.pool)
        self.auto = choose_active_roster(self.pool)
        self.active = ids(self.auto)
        self.plan = ClubPlan(team_id=1)

    def auto_lineup(self) -> list[LineupChoice]:
        return [LineupChoice(s.batter.player_id, s.position) for s in choose_lineup(self.auto.batters, ForeignQuota())]

    def set_lineup(self, choices, plan=None, limits=LIMITS):
        (plan or self.plan).set_lineup(choices, roster=self.members, active_ids=self.active, limits=limits)


class ActiveRosterInvariantTest(ClubPlanCase):
    def test_a_valid_registration_is_kept(self):
        chosen = sorted(self.active)
        self.plan.set_active(chosen, roster=self.members, limits=LIMITS)
        self.assertEqual(self.plan.active_ids, tuple(chosen))

    def test_more_than_29_players_are_refused(self):
        everyone = sorted(self.members)[:30]
        with self.assertRaisesRegex(InvalidClubPlan, "29人まで"):
            self.plan.set_active(everyone, roster=self.members, limits=LIMITS)
        self.assertIsNone(self.plan.active_ids, "弾かれた上書きは入らない")

    def test_a_player_who_is_not_on_the_team_is_refused(self):
        with self.assertRaisesRegex(InvalidClubPlan, "在籍していない"):
            self.plan.set_active([*sorted(self.active)[:-1], 9999], roster=self.members, limits=LIMITS)

    def test_a_duplicate_is_refused(self):
        chosen = sorted(self.active)
        with self.assertRaisesRegex(InvalidClubPlan, "重複"):
            self.plan.set_active([*chosen, chosen[0]], roster=self.members, limits=LIMITS)

    def test_the_foreign_roster_limit_is_enforced(self):
        pool = full_pool(foreign_ids={1, 4, 5, 101, 102})
        members = members_of(pool)
        chosen = [1, 4, 5, 101, 102, *range(6, 15), *range(103, 114)]
        limits = ClubLimits(foreign_roster_limit=4)
        with self.assertRaises(ForeignPlayerQuotaExceeded):
            self.plan.set_active(chosen, roster=members, limits=limits)
        self.plan.set_active(chosen, roster=members, limits=ClubLimits(foreign_roster_limit=5))

    def test_a_roster_that_cannot_field_a_lineup_is_refused(self):
        pitchers_only = [pid for pid in self.members if pid > 100]
        with self.assertRaisesRegex(InvalidClubPlan, "野手9人以上"):
            self.plan.set_active(pitchers_only[:14], roster=self.members, limits=LIMITS)
        batters_only = [pid for pid in self.members if pid <= 100]
        with self.assertRaisesRegex(InvalidClubPlan, r"投手\d+人以上"):
            self.plan.set_active(batters_only[:15], roster=self.members, limits=LIMITS)


class LineupInvariantTest(ClubPlanCase):
    def test_a_valid_lineup_is_kept_in_batting_order(self):
        choices = self.auto_lineup()
        self.set_lineup(choices)
        self.assertEqual(self.plan.lineup, tuple(choices))

    def test_the_lineup_needs_nine_players(self):
        with self.assertRaisesRegex(InvalidClubPlan, "9人"):
            self.set_lineup(self.auto_lineup()[:8])

    def test_a_duplicate_player_is_refused(self):
        choices = self.auto_lineup()
        choices[1] = LineupChoice(choices[0].player_id, choices[1].position)
        with self.assertRaisesRegex(InvalidClubPlan, "重複"):
            self.set_lineup(choices)

    def test_the_nine_positions_must_all_be_filled(self):
        choices = self.auto_lineup()
        choices[1] = LineupChoice(choices[1].player_id, choices[0].position)
        with self.assertRaisesRegex(InvalidClubPlan, "守備位置"):
            self.set_lineup(choices)

    def test_only_a_registered_catcher_can_catch(self):
        choices = self.auto_lineup()
        catcher_index = next(i for i, c in enumerate(choices) if c.position is FP.CATCHER)
        outfielder = next(pid for pid in self.active if 12 <= pid <= 18 and pid not in {c.player_id for c in choices})
        choices[catcher_index] = LineupChoice(outfielder, FP.CATCHER)
        with self.assertRaisesRegex(InvalidClubPlan, "捕手"):
            self.set_lineup(choices)

    def test_a_player_outside_the_active_roster_is_refused(self):
        benched = next(pid for pid in self.members if pid <= 100 and pid not in self.active)
        choices = self.auto_lineup()
        choices[8] = LineupChoice(benched, choices[8].position)
        with self.assertRaisesRegex(InvalidClubPlan, "1軍に登録されていない"):
            self.set_lineup(choices)

    def test_a_pitcher_cannot_bat(self):
        choices = self.auto_lineup()
        choices[8] = LineupChoice(101, choices[8].position)
        with self.assertRaisesRegex(InvalidClubPlan, "投手"):
            self.set_lineup(choices)

    def test_the_foreign_game_limit_is_enforced(self):
        pool = full_pool(foreign_ids={1, 4, 5})
        members = members_of(pool)
        roster = choose_active_roster(pool)
        lineup = [LineupChoice(s.batter.player_id, s.position) for s in choose_lineup(roster.batters, ForeignQuota())]
        plan = ClubPlan(team_id=1)
        foreign_in_lineup = sum(members[c.player_id].is_foreign for c in lineup)
        self.assertGreaterEqual(foreign_in_lineup, 2)
        with self.assertRaises(ForeignPlayerQuotaExceeded):
            plan.set_lineup(
                lineup,
                roster=members,
                active_ids=ids(roster),
                limits=ClubLimits(foreign_game_limit=foreign_in_lineup - 1),
            )


class PitchingInvariantTest(ClubPlanCase):
    def test_a_valid_rotation_and_closer_are_kept(self):
        self.plan.set_rotation([105, 101, 102, 103, 104], roster=self.members, active_ids=self.active)
        self.plan.set_closer(106, roster=self.members, active_ids=self.active)
        self.assertEqual(self.plan.rotation, (105, 101, 102, 103, 104))
        self.assertEqual(self.plan.closer_id, 106)

    def test_the_rotation_has_at_most_six(self):
        with self.assertRaisesRegex(InvalidClubPlan, "6人まで"):
            self.plan.set_rotation(range(101, 108), roster=self.members, active_ids=self.active)
        with self.assertRaisesRegex(InvalidClubPlan, "5人以上"):
            self.plan.set_rotation([], roster=self.members, active_ids=self.active)

    def test_a_batter_cannot_be_in_the_rotation(self):
        with self.assertRaisesRegex(InvalidClubPlan, "投手だけ"):
            self.plan.set_rotation([101, 102, 103, 104, 1], roster=self.members, active_ids=self.active)

    def test_a_pitcher_outside_the_active_roster_is_refused(self):
        benched = next(pid for pid in self.members if pid > 100 and pid not in self.active)
        with self.assertRaisesRegex(InvalidClubPlan, "1軍に登録されていない"):
            self.plan.set_rotation([101, 102, 103, 104, benched], roster=self.members, active_ids=self.active)
        with self.assertRaisesRegex(InvalidClubPlan, "1軍に登録されていない"):
            self.plan.set_closer(benched, roster=self.members, active_ids=self.active)

    def test_a_duplicate_in_the_rotation_is_refused(self):
        with self.assertRaisesRegex(InvalidClubPlan, "重複"):
            self.plan.set_rotation([101, 101, 102, 103, 104], roster=self.members, active_ids=self.active)

    def test_a_batter_cannot_close(self):
        with self.assertRaisesRegex(InvalidClubPlan, "投手だけ"):
            self.plan.set_closer(1, roster=self.members, active_ids=self.active)

    def test_the_closer_and_the_rotation_cannot_share_a_pitcher(self):
        self.plan.set_rotation(range(101, 106), roster=self.members, active_ids=self.active)
        with self.assertRaisesRegex(InvalidClubPlan, "抑えにできません"):
            self.plan.set_closer(101, roster=self.members, active_ids=self.active)
        other = ClubPlan(team_id=1)
        other.set_closer(101, roster=self.members, active_ids=self.active)
        with self.assertRaisesRegex(InvalidClubPlan, "ローテーションに入れられません"):
            other.set_rotation(range(101, 106), roster=self.members, active_ids=self.active)


class ClearTest(ClubPlanCase):
    def test_clearing_a_section_returns_it_to_automatic(self):
        self.plan.set_active(sorted(self.active), roster=self.members, limits=LIMITS)
        self.set_lineup(self.auto_lineup())
        self.plan.set_rotation(range(101, 106), roster=self.members, active_ids=self.active)
        self.plan.set_closer(106, roster=self.members, active_ids=self.active)
        self.assertFalse(self.plan.is_empty)

        self.plan.clear_active()
        self.plan.clear_lineup()
        self.plan.clear_rotation()
        self.plan.clear_closer()

        self.assertTrue(self.plan.is_empty)


class ReleaseTest(ClubPlanCase):
    """引退した選手を含む区画だけを自動に戻す（シーズンを締めるとき）。"""

    def full_plan(self) -> ClubPlan:
        plan = ClubPlan(team_id=1)
        plan.set_active(sorted(self.active), roster=self.members, limits=LIMITS)
        self.set_lineup(self.auto_lineup(), plan)
        starters = sorted(p.player_id for p in self.auto.pitchers)
        plan.set_rotation(starters[:5], roster=self.members, active_ids=self.active)
        plan.set_closer(starters[5], roster=self.members, active_ids=self.active)
        return plan

    def test_a_retired_player_only_in_the_active_roster_releases_every_section(self):
        """1軍登録を戻すときは、依存するオーダー・ローテーション・抑えも戻す。"""
        plan = self.full_plan()
        assert plan.active_ids is not None and plan.lineup and plan.rotation
        in_use = {c.player_id for c in plan.lineup} | set(plan.rotation) | {plan.closer_id}
        retired = next(i for i in plan.active_ids if i not in in_use)

        released, sections = plan.release([retired])

        self.assertEqual(sections, tuple(PlanSection))
        self.assertTrue(released.is_empty)

    def test_a_retired_starter_releases_the_rotation_and_the_active_roster_and_what_depends_on_it(self):
        plan = self.full_plan()
        assert plan.rotation is not None

        released, sections = plan.release([plan.rotation[0]])

        self.assertEqual(sections, tuple(PlanSection))
        self.assertTrue(released.is_empty)

    def test_only_a_retired_lineup_player_outside_the_active_roster_releases_only_the_lineup(self):
        plan = self.full_plan()
        assert plan.lineup is not None
        # 1軍登録の外の選手がオーダーにいる（登録が変わった後に残った上書き）状況を作る
        outsider = plan.lineup[0].player_id
        plan.active_ids = tuple(i for i in plan.active_ids or () if i != outsider)

        released, sections = plan.release([outsider])

        self.assertEqual(sections, (PlanSection.LINEUP,))
        self.assertIsNone(released.lineup)
        self.assertEqual(released.active_ids, plan.active_ids)
        self.assertEqual(released.rotation, plan.rotation)
        self.assertEqual(released.closer_id, plan.closer_id)

    def test_a_retired_closer_releases_only_the_closer(self):
        plan = self.full_plan()
        assert plan.closer_id is not None
        # 抑えは1軍の投手だが、ローテーションには入らない。1軍登録からは外れた選手だけにして区画を絞る
        plan.active_ids = tuple(i for i in plan.active_ids or () if i != plan.closer_id)

        released, sections = plan.release([plan.closer_id])

        self.assertEqual(sections, (PlanSection.CLOSER,))
        self.assertIsNone(released.closer_id)
        self.assertEqual(released.active_ids, plan.active_ids)
        self.assertEqual(released.rotation, plan.rotation)
        self.assertEqual(released.lineup, plan.lineup)

    def test_nobody_in_the_plan_changes_nothing(self):
        plan = self.full_plan()

        released, sections = plan.release([99999])

        self.assertEqual(sections, ())
        self.assertEqual(released, plan)

    def test_the_original_plan_is_not_changed(self):
        plan = self.full_plan()
        before = (plan.active_ids, plan.lineup, plan.rotation, plan.closer_id)
        assert plan.rotation is not None

        plan.release(plan.rotation)

        self.assertEqual((plan.active_ids, plan.lineup, plan.rotation, plan.closer_id), before)

    def test_an_automatic_plan_has_nothing_to_release(self):
        released, sections = ClubPlan(team_id=1).release([1, 2, 3])

        self.assertEqual(sections, ())
        self.assertTrue(released.is_empty)


class ResolveTest(ClubPlanCase):
    def test_without_a_plan_everything_is_automatic(self):
        for plan in (None, ClubPlan(team_id=1)):
            resolved = resolve_club(plan, self.pool, LIMITS)
            self.assertEqual(resolved.roster, self.auto)
            self.assertIsNone(resolved.orders)
            self.assertEqual(resolved.fallbacks, ())

    def test_a_manual_registration_becomes_the_active_roster(self):
        chosen = [*range(1, 16), *range(101, 115)]
        chosen[14] = 16  # 自動なら入らない野手に替える
        self.plan.set_active(chosen, roster=self.members, limits=LIMITS)

        resolved = resolve_club(self.plan, self.pool, LIMITS)

        self.assertEqual(ids(resolved.roster), set(chosen))
        self.assertEqual(resolved.fallbacks, ())

    def test_a_manual_lineup_is_passed_on_in_that_order(self):
        choices = list(reversed(self.auto_lineup()))
        self.set_lineup(choices)

        resolved = resolve_club(self.plan, self.pool, LIMITS)

        assert resolved.orders is not None and resolved.orders.lineup is not None
        self.assertEqual([LineupChoice(s.batter.player_id, s.position) for s in resolved.orders.lineup], choices)
        self.assertIsNone(resolved.orders.staff, "オーダーだけ手動なら、投手陣は自動")

    def test_a_manual_rotation_and_closer_build_the_staff(self):
        self.plan.set_rotation(ROTATION, roster=self.members, active_ids=self.active)
        self.plan.set_closer(101, roster=self.members, active_ids=self.active)

        resolved = resolve_club(self.plan, self.pool, LIMITS)

        assert resolved.orders is not None and resolved.orders.staff is not None
        staff = resolved.orders.staff
        self.assertEqual([p.player_id for p in staff.rotation], ROTATION)
        assert staff.closer is not None
        self.assertEqual(staff.closer.player_id, 101)
        # 残りの投手は、自動と同じ規則（抑える力の順）で救援に回る
        relievers = [p.player_id for p in staff.bullpen]
        self.assertEqual(len(relievers), len(set(relievers)))
        self.assertNotIn(110, relievers)
        self.assertEqual(set(relievers) | set(ROTATION), {p.player_id for p in resolved.roster.pitchers})

    def test_a_manual_closer_alone_leaves_the_rotation_automatic_without_the_closer(self):
        best_starter = plan_pitching_staff(self.auto.pitchers).rotation[0].player_id
        self.plan.set_closer(best_starter, roster=self.members, active_ids=self.active)

        resolved = resolve_club(self.plan, self.pool, LIMITS)

        assert resolved.orders is not None and resolved.orders.staff is not None
        staff = resolved.orders.staff
        self.assertNotIn(best_starter, [p.player_id for p in staff.rotation])
        self.assertEqual(len(staff.rotation), 6)
        assert staff.closer is not None
        self.assertEqual(staff.closer.player_id, best_starter)

    def test_a_lineup_with_a_player_dropped_from_the_roster_falls_back_with_a_reason(self):
        choices = self.auto_lineup()
        self.set_lineup(choices)
        dropped = choices[3].player_id
        # 1軍登録を手動にして、オーダーにいる選手を外す
        chosen = sorted(self.active - {dropped})
        chosen.append(next(pid for pid in self.members if pid <= 100 and pid not in self.active))
        self.plan.set_active(chosen, roster=self.members, limits=LIMITS)

        resolved = resolve_club(self.plan, self.pool, LIMITS)

        self.assertIsNone(resolved.orders, "オーダーは自動に落ちる")
        self.assertEqual([f.section for f in resolved.fallbacks], [PlanSection.LINEUP])
        self.assertIn("1軍に登録されていない", resolved.fallbacks[0].reason)
        self.assertNotIn(dropped, ids(resolved.roster), "1軍登録の上書きは生きている")

    def test_sections_fall_back_independently(self):
        """オーダーが使えなくても、ローテーションと抑えの上書きは生きる。"""
        self.plan.lineup = tuple(
            self.auto_lineup()[:5]
        )  # 保存された値は検査を通っていない（ロスターが変わった後など）
        self.plan.set_rotation(ROTATION, roster=self.members, active_ids=self.active)

        resolved = resolve_club(self.plan, self.pool, LIMITS)

        self.assertEqual([f.section for f in resolved.fallbacks], [PlanSection.LINEUP])
        assert resolved.orders is not None
        self.assertIsNone(resolved.orders.lineup)
        assert resolved.orders.staff is not None
        self.assertEqual([p.player_id for p in resolved.orders.staff.rotation], ROTATION)

    def test_an_unusable_registration_falls_back_to_the_ai_registration(self):
        self.plan.active_ids = tuple(range(1, 40))  # 29人を超える・球団にいない選手を含む

        resolved = resolve_club(self.plan, self.pool, LIMITS)

        self.assertEqual(resolved.roster, self.auto)
        self.assertEqual([f.section for f in resolved.fallbacks], [PlanSection.ACTIVE])

    def test_a_player_who_left_the_team_makes_the_section_fall_back(self):
        self.plan.rotation = (101, 9999)
        self.plan.closer_id = 8888

        resolved = resolve_club(self.plan, self.pool, LIMITS)

        self.assertEqual({f.section for f in resolved.fallbacks}, {PlanSection.ROTATION, PlanSection.CLOSER})
        self.assertIsNone(resolved.orders)

    def test_a_closer_who_is_also_in_the_rotation_falls_back_only_for_the_closer(self):
        self.plan.rotation = (101, 102, 103, 104, 105)
        self.plan.closer_id = 101

        resolved = resolve_club(self.plan, self.pool, LIMITS)

        self.assertEqual([f.section for f in resolved.fallbacks], [PlanSection.CLOSER])
        assert resolved.orders is not None and resolved.orders.staff is not None
        self.assertEqual([p.player_id for p in resolved.orders.staff.rotation], [101, 102, 103, 104, 105])

    def test_resolving_never_raises_for_a_broken_plan(self):
        broken = ClubPlan(
            team_id=1,
            active_ids=(1, 1, 1),
            lineup=(LineupChoice(1, FP.CATCHER),),
            rotation=(1,),
            closer_id=1,
        )
        try:
            resolved = resolve_club(broken, self.pool, LIMITS)
        except DomainError as error:  # pragma: no cover - 失敗したときに理由を読めるように
            self.fail(f"上書きが壊れていても進行は止めない: {error}")
        self.assertEqual({f.section for f in resolved.fallbacks}, set(PlanSection))


class PlanThatWouldStopTheSeasonTest(ClubPlanCase):
    """GM の操作だけで「進める」が例外で止まる組み合わせを、決めるときに弾くか、その日だけ自動に落とす（再発防止）。"""

    def test_a_roster_whose_lineup_cannot_fit_the_foreign_game_limit_is_refused(self):
        # 野手9人のうち外国人4人。登録枠（5）は満たすが、出場枠（3）ではスタメン9人を組めない
        members = members_of(full_pool(foreign_ids={4, 5, 6, 7}))
        limits = ClubLimits(foreign_roster_limit=5, foreign_game_limit=3)
        with self.assertRaisesRegex(ForeignPlayerQuotaExceeded, "スタメン9人を組めません"):
            self.plan.set_active([*NINE, *range(101, 111)], roster=members, limits=limits)
        self.plan.set_active([*TEN, *range(101, 111)], roster=members, limits=limits)

    def test_a_foreign_pitcher_who_may_start_takes_one_game_slot(self):
        members = members_of(full_pool(foreign_ids={4, 5, 6, 7, 101}))
        limits = ClubLimits(foreign_roster_limit=5, foreign_game_limit=3)
        with self.assertRaisesRegex(ForeignPlayerQuotaExceeded, "スタメン9人を組めません"):
            self.plan.set_active([*TEN, *range(101, 111)], roster=members, limits=limits)

    def test_a_roster_without_a_catcher_is_refused(self):
        with self.assertRaisesRegex(InvalidClubPlan, "捕手"):
            self.plan.set_active([*range(4, 13), *range(101, 111)], roster=self.members, limits=LIMITS)

    def test_the_only_active_pitcher_cannot_be_the_closer(self):
        active = [*range(1, 10), 101]
        with self.assertRaisesRegex(InvalidClubPlan, "先発できる"):
            self.plan.set_closer(101, roster=self.members, active_ids=active)

    def test_a_registration_with_too_few_pitchers_falls_back(self):
        plan = ClubPlan(team_id=1, active_ids=(*range(1, 10), 101), closer_id=101)
        resolved = resolve_club(plan, self.pool, LIMITS)
        # 投手が下限に満たない1軍登録は使えない（保存済みの古い上書き）ので、AI の1軍に戻る
        self.assertEqual([f.section for f in resolved.fallbacks], [PlanSection.ACTIVE])
        self.assertEqual(resolved.roster, self.auto)

    def test_a_full_foreign_lineup_with_an_all_foreign_rotation_falls_back(self):
        lineup = self.auto_lineup()
        foreign = {choice.player_id for choice in lineup[:3]}
        pool = full_pool(foreign_ids={*foreign, 101, 102, 103, 104, 105})
        limits = ClubLimits(foreign_game_limit=3)

        blocked = resolve_club(
            ClubPlan(team_id=1, lineup=tuple(lineup), rotation=(101, 102, 103, 104, 105)), pool, limits
        )
        self.assertEqual([f.section for f in blocked.fallbacks], [PlanSection.LINEUP])
        self.assertIn("先発を立てられません", blocked.fallbacks[0].reason)
        assert blocked.orders is not None
        self.assertIsNone(blocked.orders.lineup)

        kept = resolve_club(
            ClubPlan(team_id=1, lineup=tuple(lineup), rotation=(101, 102, 103, 104, 106)), pool, limits
        )
        # オーダーは生きる（外国人の抑えが投げられない注意は別。下のテストで見る）
        self.assertNotIn(PlanSection.LINEUP, [f.section for f in kept.fallbacks])
        assert kept.orders is not None and kept.orders.lineup is not None
        self.assertEqual([s.batter.player_id for s in kept.orders.lineup], [c.player_id for c in lineup])

    def test_the_game_limit_is_the_strictest_in_the_world(self):
        # 交流戦はホーム球団のリーグの枠で行うので、出場枠の違うリーグがあれば厳しい方で検査する
        self.assertEqual(strictest_game_limit([4, None, 3]), 3)
        self.assertIsNone(strictest_game_limit([None, None]))
        members = members_of(full_pool(foreign_ids={4, 5, 6, 7}))
        roster = [*NINE, *range(101, 111)]
        self.plan.set_active(roster, roster=members, limits=ClubLimits(foreign_game_limit=4))
        with self.assertRaises(ForeignPlayerQuotaExceeded):
            self.plan.set_active(
                roster, roster=members, limits=ClubLimits(foreign_game_limit=strictest_game_limit([4, 3]))
            )

    def test_a_club_without_pitchers_does_not_raise_while_resolving(self):
        lineup = self.auto_lineup()
        foreign = {choice.player_id for choice in lineup[:3]}
        source = full_pool(foreign_ids=foreign)
        pool = ClubRoster(team_id=source.team_id, name=source.name, batters=source.batters, pitchers=())
        try:
            resolve_club(ClubPlan(team_id=1, lineup=tuple(lineup)), pool, ClubLimits(foreign_game_limit=3))
        except InvalidRoster as error:  # pragma: no cover - 失敗したときに理由を読めるように
            self.fail(f"投手のいない球団でも、当てはめは例外にしない（試合を組めないのはエンジンが知らせる）: {error}")


class WeakerManualPlanIsRefusedTest(ClubPlanCase):
    """手動の編成にも AI と同じ検査を当てる（ローテの下限・投手の下限・守備位置・外国人の抑え）。"""

    def test_a_rotation_below_the_minimum_is_refused(self):
        for size in (1, 4):
            with self.assertRaisesRegex(InvalidClubPlan, f"{MIN_ROTATION_SIZE}人以上"):
                self.plan.set_rotation(range(101, 101 + size), roster=self.members, active_ids=self.active)
        self.assertIsNone(self.plan.rotation, "弾かれた上書きは入らない")
        self.plan.set_rotation(range(101, 101 + MIN_ROTATION_SIZE), roster=self.members, active_ids=self.active)

    def test_the_minimum_rotation_is_one_less_than_the_ai_rotation(self):
        """下限は AI のローテーションの人数と同じ出典から導く（別の数を持たない）。"""
        self.assertEqual(MIN_ROTATION_SIZE, ROTATION_SIZE - 1)

    def test_an_active_roster_with_too_few_pitchers_is_refused(self):
        batters = list(range(1, 1 + 29 - MIN_ACTIVE_PITCHERS + 1))
        pitchers = list(range(101, 101 + MIN_ACTIVE_PITCHERS - 1))
        with self.assertRaisesRegex(InvalidClubPlan, f"投手{MIN_ACTIVE_PITCHERS}人以上"):
            self.plan.set_active([*batters[:15], *pitchers], roster=self.members, limits=LIMITS)
        self.plan.set_active(
            [*batters[:15], *range(101, 101 + MIN_ACTIVE_PITCHERS)], roster=self.members, limits=LIMITS
        )

    def test_a_player_cannot_play_a_position_his_registration_does_not_allow(self):
        choices = self.auto_lineup()
        shortstop = next(i for i, c in enumerate(choices) if c.position is FP.SHORTSTOP)
        outfielder = next(pid for pid in self.active if 12 <= pid <= 18 and pid not in {c.player_id for c in choices})
        choices[shortstop] = LineupChoice(outfielder, FP.SHORTSTOP)
        with self.assertRaisesRegex(InvalidClubPlan, "遊撃手を守れません"):
            self.set_lineup(choices)

    def test_a_catcher_cannot_play_the_field(self):
        choices = self.auto_lineup()
        first_base = next(i for i, c in enumerate(choices) if c.position is FP.FIRST_BASE)
        spare_catcher = next(pid for pid in self.active if pid <= 3 and pid not in {c.player_id for c in choices})
        choices[first_base] = LineupChoice(spare_catcher, FP.FIRST_BASE)
        with self.assertRaisesRegex(InvalidClubPlan, "一塁手を守れません"):
            self.set_lineup(choices)

    def test_anyone_can_be_the_designated_hitter_and_this_pools_ai_lineup_is_accepted(self):
        choices = self.auto_lineup()
        self.set_lineup(choices)  # この登録候補では、AI のオーダーも手動の規則を満たす（一般には満たさないことがある）
        dh = next(i for i, c in enumerate(choices) if c.position is FP.DESIGNATED_HITTER)
        spare_catcher = next(pid for pid in self.active if pid <= 3 and pid not in {c.player_id for c in choices})
        choices[dh] = LineupChoice(spare_catcher, FP.DESIGNATED_HITTER)
        self.set_lineup(choices)

    def test_a_foreign_closer_that_cannot_pitch_in_the_game_limit_is_noted(self):
        lineup = self.auto_lineup()
        foreign_batters = {choice.player_id for choice in lineup[:3]}
        pool = full_pool(foreign_ids={*foreign_batters, 101})
        limits = ClubLimits(foreign_game_limit=3)
        plan = ClubPlan(team_id=1, lineup=tuple(lineup), closer_id=101)

        resolved = resolve_club(plan, pool, limits)

        self.assertEqual([f.section for f in resolved.fallbacks], [PlanSection.CLOSER])
        self.assertIn("外国人の抑え", resolved.fallbacks[0].reason)
        self.assertIn("投げられません", resolved.fallbacks[0].reason)
        assert resolved.orders is not None and resolved.orders.staff is not None
        assert resolved.orders.staff.closer is not None
        self.assertEqual(resolved.orders.staff.closer.player_id, 101, "上書きは外さず、知らせるだけ")
        assert resolved.orders.lineup is not None, "オーダーは自動に落とさない"

    def test_a_domestic_closer_or_a_free_game_slot_is_not_noted(self):
        lineup = self.auto_lineup()
        foreign_batters = {choice.player_id for choice in lineup[:3]}
        pool = full_pool(foreign_ids={*foreign_batters, 101})
        domestic = resolve_club(
            ClubPlan(team_id=1, lineup=tuple(lineup), closer_id=102), pool, ClubLimits(foreign_game_limit=3)
        )
        self.assertEqual(domestic.fallbacks, ())
        room = resolve_club(
            ClubPlan(team_id=1, lineup=tuple(lineup), closer_id=101), pool, ClubLimits(foreign_game_limit=4)
        )
        self.assertEqual(room.fallbacks, ())

    def test_a_saved_short_rotation_falls_back_to_automatic_with_a_reason(self):
        """下限を設ける前に保存された手動の編成は、その区画だけ自動に戻して理由を返す。"""
        plan = ClubPlan(team_id=1, rotation=(101, 102), closer_id=103)

        resolved = resolve_club(plan, self.pool, LIMITS)

        self.assertEqual([f.section for f in resolved.fallbacks], [PlanSection.ROTATION])
        self.assertIn(f"{MIN_ROTATION_SIZE}人以上", resolved.fallbacks[0].reason)
        assert resolved.orders is not None and resolved.orders.staff is not None
        self.assertEqual(len(resolved.orders.staff.rotation), ROTATION_SIZE, "ローテーションは AI が決める")
        assert resolved.orders.staff.closer is not None
        self.assertEqual(resolved.orders.staff.closer.player_id, 103, "抑えの上書きは生きる")

    def test_a_saved_lineup_with_a_wrong_position_falls_back(self):
        choices = self.auto_lineup()
        shortstop = next(i for i, c in enumerate(choices) if c.position is FP.SHORTSTOP)
        outfielder = next(pid for pid in self.active if 12 <= pid <= 18 and pid not in {c.player_id for c in choices})
        choices[shortstop] = LineupChoice(outfielder, FP.SHORTSTOP)

        resolved = resolve_club(ClubPlan(team_id=1, lineup=tuple(choices)), self.pool, LIMITS)

        self.assertEqual([f.section for f in resolved.fallbacks], [PlanSection.LINEUP])
        self.assertIn("守れません", resolved.fallbacks[0].reason)


class FieldingPositionsInTheActiveRosterTest(ClubPlanCase):
    """1軍登録の段階で、守備位置（捕1・内4・外3）を野手で埋められることを保証する（手動のオーダーが組めない行き止まりを作らない）。"""

    def test_an_active_roster_without_enough_outfielders_is_refused(self):
        no_outfield = [1, 2, 4, 5, 6, 7, 8, 9, 10, 11, 12]  # 外野手が1人だけ
        with self.assertRaisesRegex(InvalidClubPlan, "守れる野手がいない"):
            self.plan.set_active([*no_outfield, *range(101, 109)], roster=self.members, limits=LIMITS)
        self.plan.set_active([*NINE, *range(101, 109)], roster=self.members, limits=LIMITS)

    def test_the_designated_hitter_registration_can_fill_first_base_and_the_corners(self):
        # 指名打者の登録（19・20）は一塁・左翼・右翼に就ける。外野手2人 + 指名打者の登録で外野の3枠を満たす
        batters = [1, 4, 5, 6, 7, 12, 13, 19, 20]
        self.plan.set_active([*batters, *range(101, 109)], roster=self.members, limits=LIMITS)

    def test_the_gap_is_named(self):
        self.assertEqual(
            unfilled_position(m for pid, m in self.members.items() if pid in {1, 4, 5, 6, 7}), FP.LEFT_FIELD
        )
        self.assertIsNone(unfilled_position(self.members[pid] for pid in NINE))

    def nine_choices(self):
        return [LineupChoice(pid, pos) for pid, pos in zip(NINE, LINEUP_POSITION_ORDER, strict=True)]

    def test_arrange_lineup_reassigns_only_when_needed(self):
        choices = self.nine_choices()
        self.assertEqual(arrange_lineup(choices, self.members), choices, "すでに就ける位置ならそのまま")
        wrong = [
            LineupChoice(c.player_id, FP.DESIGNATED_HITTER if c.position is FP.SHORTSTOP else c.position)
            for c in choices
        ]
        wrong[-1] = LineupChoice(wrong[-1].player_id, FP.SHORTSTOP)  # 指名打者の登録が遊撃
        fitted = arrange_lineup(wrong, self.members)
        assert fitted is not None
        self.assertEqual([c.player_id for c in fitted], NINE, "選手と打順は変えない")
        for choice in fitted:
            self.assertTrue(can_play(self.members[choice.player_id].position, choice.position))
        self.assertEqual({c.position for c in fitted}, LINEUP_POSITIONS)

    def test_arrange_lineup_does_not_move_players_who_already_stand_correctly(self):
        choices = self.nine_choices()
        # 捕手(1) と 一塁(4) を入れ替えた誤りだけを直す。ほかの7人は動かさない
        wrong = [LineupChoice(c.player_id, c.position) for c in choices]
        wrong[0], wrong[1] = LineupChoice(1, FP.FIRST_BASE), LineupChoice(4, FP.CATCHER)

        fitted = arrange_lineup(wrong, self.members)

        assert fitted is not None
        self.assertEqual({c.player_id: c.position for c in fitted[2:]}, {c.player_id: c.position for c in choices[2:]})
        self.assertEqual(fitted[0], LineupChoice(1, FP.CATCHER))
        self.assertEqual(fitted[1].player_id, 4)
        self.assertTrue(can_play(self.members[4].position, fitted[1].position))

    def test_arrange_lineup_gives_up_when_the_nine_and_the_bench_cannot_cover_the_field(self):
        no_outfield = [1, 4, 5, 6, 7, 8, 9, 10, 11]
        choices = [LineupChoice(pid, pos) for pid, pos in zip(no_outfield, LINEUP_POSITION_ORDER, strict=True)]
        self.assertIsNone(arrange_lineup(choices, self.members))

    def test_arrange_lineup_brings_in_a_bench_player_for_a_missing_position(self):
        no_outfield = [1, 4, 5, 6, 7, 8, 9, 10, 11]
        choices = [LineupChoice(pid, pos) for pid, pos in zip(no_outfield, LINEUP_POSITION_ORDER, strict=True)]
        bench = [self.members[pid] for pid in (12, 13, 14)]

        fitted = arrange_lineup(choices, self.members, bench=bench)

        assert fitted is not None
        self.assertEqual({c.position for c in fitted}, LINEUP_POSITIONS)
        self.assertTrue({12, 13, 14} <= {c.player_id for c in fitted}, "外野の3枠を控えが埋める")
        for choice in fitted:
            self.assertTrue(can_play(self.members[choice.player_id].position, choice.position))

    def test_arrange_lineup_brings_in_a_bench_player_who_cannot_play_the_first_gap_directly(self):
        """空いた位置を直接守れない控えでも、入れ替えて割り当てが増えるならよい（誰かが動いて空きを埋める）。"""
        kinds = {
            **dict.fromkeys((1, 2, 3), Position.CATCHER),
            **dict.fromkeys((4, 5, 9), Position.INFIELDER),
            **dict.fromkeys((7, 8, 10), Position.OUTFIELDER),
            6: Position.DESIGNATED_HITTER,
        }
        roster = {pid: ClubMember(pid, f"選手{pid}", kind) for pid, kind in kinds.items()}
        nine = [1, 4, 5, 9, 7, 8, 10, 2, 3]  # 内野手が3人しかおらず遊撃が空く。控えの指名打者登録は遊撃を直接守れない
        choices = [LineupChoice(pid, pos) for pid, pos in zip(nine, LINEUP_POSITION_ORDER, strict=True)]

        fitted = arrange_lineup(choices, roster, bench=[roster[6]])

        assert fitted is not None
        self.assertIn(6, [c.player_id for c in fitted])
        self.assertEqual({c.position for c in fitted}, LINEUP_POSITIONS)
        for choice in fitted:
            self.assertTrue(can_play(roster[choice.player_id].position, choice.position))

    def test_arrange_lineup_succeeds_without_foreigners_even_with_a_game_limit(self):
        """出場枠が原因でないとき（外国人がいない）は、枠があっても組める。"""
        no_outfield = [1, 4, 5, 6, 7, 8, 9, 10, 11]
        choices = [LineupChoice(pid, pos) for pid, pos in zip(no_outfield, LINEUP_POSITION_ORDER, strict=True)]
        bench = [self.members[pid] for pid in (12, 13, 14)]

        self.assertIsNotNone(arrange_lineup(choices, self.members, bench=bench, foreign_game_limit=0))

    def test_arrange_lineup_respects_the_foreign_game_limit_when_bringing_in_the_bench(self):
        members = members_of(full_pool(foreign_ids={12, 13, 14}))
        roster = dict(members)
        no_outfield = [1, 4, 5, 6, 7, 8, 9, 10, 11]
        choices = [LineupChoice(pid, pos) for pid, pos in zip(no_outfield, LINEUP_POSITION_ORDER, strict=True)]
        bench = [roster[pid] for pid in (12, 13, 14)]

        self.assertIsNone(arrange_lineup(choices, roster, bench=bench, foreign_game_limit=2), "外野3人が全員外国人")
        self.assertIsNotNone(arrange_lineup(choices, roster, bench=bench, foreign_game_limit=3))


class NoticeKindTest(ClubPlanCase):
    def test_a_fallback_says_it_fell_back_and_a_closer_advisory_does_not(self):
        lineup = self.auto_lineup()
        foreign_batters = {choice.player_id for choice in lineup[:3]}
        pool = full_pool(foreign_ids={*foreign_batters, 101})
        limits = ClubLimits(foreign_game_limit=3)

        manual = resolve_club(ClubPlan(team_id=1, lineup=tuple(lineup), closer_id=101), pool, limits)
        auto = resolve_club(
            ClubPlan(team_id=1, lineup=tuple(lineup)),
            full_pool(foreign_ids={*foreign_batters, *range(107, 120)}),
            limits,
        )
        fell = resolve_club(ClubPlan(team_id=1, rotation=(101, 102)), pool, limits)

        (advice,) = manual.fallbacks
        self.assertFalse(advice.falls_back)
        self.assertIn("手動で指定した", advice.reason)
        self.assertIn("手動で指定するか", advice.reason, "直し方を添える")
        self.assertTrue(all(not f.falls_back for f in auto.fallbacks if f.section is PlanSection.CLOSER))
        self.assertTrue(any("自動で選ばれた" in f.reason for f in auto.fallbacks))
        self.assertTrue(all(f.falls_back for f in fell.fallbacks))

    def test_a_closer_with_no_other_starter_is_dropped_to_automatic(self):
        """抑えのほかに先発できる投手がいない（登録候補の投手が1人）とき、抑えの上書きは自動に落ちる。"""
        from myapp.domain.simulation.manager import ClubRoster

        pool = full_pool()
        lone = ClubRoster(pool.team_id, pool.name, pool.batters, pool.pitchers[:1])
        lone_id = lone.pitchers[0].player_id

        resolved = resolve_club(ClubPlan(team_id=1, closer_id=lone_id), lone, LIMITS)

        self.assertEqual([f.section for f in resolved.fallbacks], [PlanSection.CLOSER])
        self.assertTrue(resolved.fallbacks[0].falls_back)
        self.assertIn("先発できる", resolved.fallbacks[0].reason)
