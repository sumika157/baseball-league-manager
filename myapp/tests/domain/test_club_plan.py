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
    ClubLimits,
    ClubPlan,
    LineupChoice,
    PlanSection,
    members_of,
    resolve_club,
    strictest_game_limit,
)
from myapp.domain.simulation.manager import (
    ClubRoster,
    ForeignQuota,
    choose_active_roster,
    choose_lineup,
    plan_pitching_staff,
)
from myapp.domain.value_objects import FieldingPosition

from .test_ai_manager import full_pool

FP = FieldingPosition
LIMITS = ClubLimits()


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
        with self.assertRaisesRegex(InvalidClubPlan, "投手1人以上"):
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
        self.plan.set_rotation([105, 101, 102], roster=self.members, active_ids=self.active)
        self.plan.set_closer(103, roster=self.members, active_ids=self.active)
        self.assertEqual(self.plan.rotation, (105, 101, 102))
        self.assertEqual(self.plan.closer_id, 103)

    def test_the_rotation_has_at_most_six(self):
        with self.assertRaisesRegex(InvalidClubPlan, "6人まで"):
            self.plan.set_rotation(range(101, 108), roster=self.members, active_ids=self.active)
        with self.assertRaisesRegex(InvalidClubPlan, "1人もいません"):
            self.plan.set_rotation([], roster=self.members, active_ids=self.active)

    def test_a_batter_cannot_be_in_the_rotation(self):
        with self.assertRaisesRegex(InvalidClubPlan, "投手だけ"):
            self.plan.set_rotation([101, 1], roster=self.members, active_ids=self.active)

    def test_a_pitcher_outside_the_active_roster_is_refused(self):
        benched = next(pid for pid in self.members if pid > 100 and pid not in self.active)
        with self.assertRaisesRegex(InvalidClubPlan, "1軍に登録されていない"):
            self.plan.set_rotation([101, benched], roster=self.members, active_ids=self.active)
        with self.assertRaisesRegex(InvalidClubPlan, "1軍に登録されていない"):
            self.plan.set_closer(benched, roster=self.members, active_ids=self.active)

    def test_a_duplicate_in_the_rotation_is_refused(self):
        with self.assertRaisesRegex(InvalidClubPlan, "重複"):
            self.plan.set_rotation([101, 101], roster=self.members, active_ids=self.active)

    def test_a_batter_cannot_close(self):
        with self.assertRaisesRegex(InvalidClubPlan, "投手だけ"):
            self.plan.set_closer(1, roster=self.members, active_ids=self.active)

    def test_the_closer_and_the_rotation_cannot_share_a_pitcher(self):
        self.plan.set_rotation([101, 102], roster=self.members, active_ids=self.active)
        with self.assertRaisesRegex(InvalidClubPlan, "抑えにできません"):
            self.plan.set_closer(101, roster=self.members, active_ids=self.active)
        other = ClubPlan(team_id=1)
        other.set_closer(101, roster=self.members, active_ids=self.active)
        with self.assertRaisesRegex(InvalidClubPlan, "ローテーションに入れられません"):
            other.set_rotation([101, 102], roster=self.members, active_ids=self.active)


class ClearTest(ClubPlanCase):
    def test_clearing_a_section_returns_it_to_automatic(self):
        self.plan.set_active(sorted(self.active), roster=self.members, limits=LIMITS)
        self.set_lineup(self.auto_lineup())
        self.plan.set_rotation([101], roster=self.members, active_ids=self.active)
        self.plan.set_closer(102, roster=self.members, active_ids=self.active)
        self.assertFalse(self.plan.is_empty)

        self.plan.clear_active()
        self.plan.clear_lineup()
        self.plan.clear_rotation()
        self.plan.clear_closer()

        self.assertTrue(self.plan.is_empty)


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
        self.plan.set_rotation([110, 111], roster=self.members, active_ids=self.active | {110, 111})
        self.plan.set_closer(101, roster=self.members, active_ids=self.active)

        resolved = resolve_club(self.plan, self.pool, LIMITS)

        assert resolved.orders is not None and resolved.orders.staff is not None
        staff = resolved.orders.staff
        self.assertEqual([p.player_id for p in staff.rotation], [110, 111])
        assert staff.closer is not None
        self.assertEqual(staff.closer.player_id, 101)
        # 残りの投手は、自動と同じ規則（抑える力の順）で救援に回る
        relievers = [p.player_id for p in staff.bullpen]
        self.assertEqual(len(relievers), len(set(relievers)))
        self.assertNotIn(110, relievers)
        self.assertEqual(set(relievers) | {110, 111}, {p.player_id for p in resolved.roster.pitchers})

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
        self.plan.set_rotation([110, 111], roster=self.members, active_ids=self.active | {110, 111})

        resolved = resolve_club(self.plan, self.pool, LIMITS)

        self.assertEqual([f.section for f in resolved.fallbacks], [PlanSection.LINEUP])
        assert resolved.orders is not None
        self.assertIsNone(resolved.orders.lineup)
        assert resolved.orders.staff is not None
        self.assertEqual([p.player_id for p in resolved.orders.staff.rotation], [110, 111])

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
        self.plan.rotation = (101, 102)
        self.plan.closer_id = 101

        resolved = resolve_club(self.plan, self.pool, LIMITS)

        self.assertEqual([f.section for f in resolved.fallbacks], [PlanSection.CLOSER])
        assert resolved.orders is not None and resolved.orders.staff is not None
        self.assertEqual([p.player_id for p in resolved.orders.staff.rotation], [101, 102])

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
            self.plan.set_active([*range(1, 10), *range(101, 111)], roster=members, limits=limits)
        self.plan.set_active([*range(1, 11), *range(101, 111)], roster=members, limits=limits)

    def test_a_foreign_pitcher_who_may_start_takes_one_game_slot(self):
        members = members_of(full_pool(foreign_ids={4, 5, 6, 7, 101}))
        limits = ClubLimits(foreign_roster_limit=5, foreign_game_limit=3)
        with self.assertRaisesRegex(ForeignPlayerQuotaExceeded, "スタメン9人を組めません"):
            self.plan.set_active([*range(1, 11), *range(101, 111)], roster=members, limits=limits)

    def test_a_roster_without_a_catcher_is_refused(self):
        with self.assertRaisesRegex(InvalidClubPlan, "捕手"):
            self.plan.set_active([*range(4, 13), *range(101, 111)], roster=self.members, limits=LIMITS)

    def test_the_only_active_pitcher_cannot_be_the_closer(self):
        active = [*range(1, 10), 101]
        self.plan.set_active(active, roster=self.members, limits=LIMITS)
        with self.assertRaisesRegex(InvalidClubPlan, "先発できる"):
            self.plan.set_closer(101, roster=self.members, active_ids=active)

    def test_a_closer_that_would_empty_the_rotation_falls_back(self):
        plan = ClubPlan(team_id=1, active_ids=(*range(1, 10), 101), closer_id=101)
        resolved = resolve_club(plan, self.pool, LIMITS)
        self.assertEqual([f.section for f in resolved.fallbacks], [PlanSection.CLOSER])
        staff = plan_pitching_staff(resolved.roster.pitchers)
        self.assertEqual([p.player_id for p in staff.rotation], [101], "抑えを外さず、唯一の投手が先発できる")

    def test_a_full_foreign_lineup_with_an_all_foreign_rotation_falls_back(self):
        lineup = self.auto_lineup()
        foreign = {choice.player_id for choice in lineup[:3]}
        pool = full_pool(foreign_ids={*foreign, 101, 102})
        limits = ClubLimits(foreign_game_limit=3)

        blocked = resolve_club(ClubPlan(team_id=1, lineup=tuple(lineup), rotation=(101, 102)), pool, limits)
        self.assertEqual([f.section for f in blocked.fallbacks], [PlanSection.LINEUP])
        self.assertIn("先発を立てられません", blocked.fallbacks[0].reason)
        assert blocked.orders is not None
        self.assertIsNone(blocked.orders.lineup)

        kept = resolve_club(ClubPlan(team_id=1, lineup=tuple(lineup), rotation=(101, 103)), pool, limits)
        self.assertEqual(kept.fallbacks, ())
        assert kept.orders is not None and kept.orders.lineup is not None
        self.assertEqual([s.batter.player_id for s in kept.orders.lineup], [c.player_id for c in lineup])

    def test_the_game_limit_is_the_strictest_in_the_world(self):
        # 交流戦はホーム球団のリーグの枠で行うので、出場枠の違うリーグがあれば厳しい方で検査する
        self.assertEqual(strictest_game_limit([4, None, 3]), 3)
        self.assertIsNone(strictest_game_limit([None, None]))
        members = members_of(full_pool(foreign_ids={4, 5, 6, 7}))
        roster = [*range(1, 10), *range(101, 111)]
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
