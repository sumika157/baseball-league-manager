"""ペナントの首脳陣（#210 M1: 監督）の規則の単体テスト。Django も DB も使わない。

守りたいこと:
- 就任の不変条件（最終年 ≥ 初年・同じ球団の同じ役割は同時に1人・1人1職）
- 年齢は生年月日から、その年の4月1日時点で数える
- 架空の監督は45〜65歳、傾向は0を中心に対称（−2 5%・−1 20%・0 50%・+1 20%・+2 5%）
- 退任の年齢の境（69歳までは確率、70歳は必ず、64歳は退任しない）
- 締めるときの交代: 退任した球団と監督のいない球団に、翌年から後任が就く。同じ世界なら何度計算しても同じ
"""

from collections import Counter
from datetime import date
from unittest import TestCase

from myapp.domain.exceptions import InvalidStaffTenure
from myapp.domain.pennant.staff import (
    MANDATORY_RETIREMENT_AGE,
    MAX_MANAGER_AGE,
    MIN_MANAGER_AGE,
    ManagerStyle,
    StaffAssignment,
    StaffMember,
    StaffRole,
    StaffTenure,
    age_in_year,
    draw_manager,
    ensure_valid_tenures,
    manager_retires,
    plan_manager_changes,
    plan_opening_managers,
    tactics_of,
)
from myapp.domain.simulation.randomness import make_random
from myapp.domain.simulation.tactics import TacticLevel, Tactics

from .markers import slow

M = StaffRole.MANAGER


def tenure(member_id=1, team_id=10, first=2026, last=None) -> StaffTenure:
    return StaffTenure(member_id=member_id, team_id=team_id, role=M, first_year=first, last_year=last)


def member(member_id=1, birth=date(1970, 6, 1), style=None) -> StaffMember:
    return StaffMember(
        name=f"監督{member_id}", name_kana="カントク", birth_date=birth, style=style or ManagerStyle(), id=member_id
    )


class TenureTest(TestCase):
    def test_last_year_before_first_year_is_rejected(self):
        with self.assertRaises(InvalidStaffTenure):
            tenure(first=2027, last=2026)
        tenure(first=2026, last=2026)  # 1年だけの就任は成り立つ

    def test_covers_and_close(self):
        open_ended = tenure(first=2026)
        self.assertFalse(open_ended.covers(2025))
        self.assertTrue(open_ended.covers(2040))
        self.assertTrue(open_ended.is_current)
        closed = open_ended.close(2028)
        self.assertEqual(closed.last_year, 2028)
        self.assertTrue(closed.covers(2028))
        self.assertFalse(closed.covers(2029))
        self.assertFalse(closed.is_current)
        with self.assertRaises(InvalidStaffTenure):
            closed.close(2029)


class EnsureValidTenuresTest(TestCase):
    def test_two_managers_of_the_same_team_in_the_same_year_are_rejected(self):
        with self.assertRaises(InvalidStaffTenure):
            ensure_valid_tenures([tenure(1, 10, 2026, 2028), tenure(2, 10, 2028)])

    def test_back_to_back_managers_are_allowed(self):
        ensure_valid_tenures([tenure(1, 10, 2026, 2027), tenure(2, 10, 2028)])

    def test_one_person_cannot_hold_two_posts_at_once(self):
        with self.assertRaises(InvalidStaffTenure):
            ensure_valid_tenures([tenure(1, 10, 2026), tenure(1, 11, 2030)])
        ensure_valid_tenures([tenure(1, 10, 2026, 2029), tenure(1, 11, 2030)])


class AgeTest(TestCase):
    def test_age_is_counted_on_april_first(self):
        self.assertEqual(age_in_year(date(1970, 4, 1), 2026), 56)
        self.assertEqual(age_in_year(date(1970, 4, 2), 2026), 55)
        self.assertEqual(member(birth=date(1970, 4, 2)).age_in(2027), 56)


class DrawManagerTest(TestCase):
    def test_age_is_within_the_range(self):
        names: set[str] = set()
        ages = {draw_manager(make_random(seed), year=2026, used_names=names).age_in(2026) for seed in range(300)}
        self.assertEqual(min(ages), MIN_MANAGER_AGE)
        self.assertEqual(max(ages), MAX_MANAGER_AGE)

    def test_names_are_not_reused(self):
        names = {"既存の名前"}
        drawn = [draw_manager(make_random(seed), year=2026, used_names=names).name for seed in range(50)]
        self.assertEqual(len(set(drawn)), 50)
        self.assertNotIn("既存の名前", drawn)

    @slow
    def test_styles_are_symmetric_around_standard(self):
        counts: Counter = Counter()
        draws = 4000
        names: set[str] = set()
        for seed in range(draws):
            style = draw_manager(make_random(seed), year=2026, used_names=names).style
            counts.update([style.starter_leash, style.pinch_hitting, style.base_stealing, style.bunting])
        total = draws * 4
        expected = {-2: 0.05, -1: 0.20, 0: 0.50, 1: 0.20, 2: 0.05}
        for level, share in expected.items():
            self.assertAlmostEqual(counts[level] / total, share, delta=0.015)
        self.assertAlmostEqual(counts[-2] / total, counts[2] / total, delta=0.01)
        self.assertAlmostEqual(counts[-1] / total, counts[1] / total, delta=0.015)

    def test_style_becomes_tactics_as_is(self):
        style = ManagerStyle(starter_leash=2, pinch_hitting=-1, base_stealing=0, bunting=1)
        self.assertEqual(tactics_of(style), Tactics(starter_leash=2, pinch_hitting=-1, base_stealing=0, bunting=1))
        self.assertEqual(tactics_of(ManagerStyle()), Tactics.STANDARD)
        self.assertIsInstance(style.starter_leash, TacticLevel)


class RetirementTest(TestCase):
    def test_age_boundaries(self):
        self.assertFalse(manager_retires(64, 0.0))
        self.assertTrue(manager_retires(65, 0.0))
        self.assertFalse(manager_retires(65, 0.999))
        self.assertFalse(manager_retires(69, 0.999))
        self.assertTrue(manager_retires(MANDATORY_RETIREMENT_AGE, 0.999))
        self.assertTrue(manager_retires(75, 0.999))


class PlanOpeningManagersTest(TestCase):
    def test_every_team_gets_a_manager_from_the_opening_year(self):
        appointments = plan_opening_managers({11: 1, 12: 2, 13: 3}, year=2026, world_seed=7, used_names=set())
        self.assertEqual([a.team_id for a in appointments], [11, 12, 13])
        self.assertTrue(all(a.first_year == 2026 and a.role is M for a in appointments))
        self.assertEqual(len({a.member.name for a in appointments}), 3)

    def test_the_same_seed_and_source_teams_give_the_same_managers(self):
        first = plan_opening_managers({11: 1, 12: 2}, year=2026, world_seed=7, used_names=set())
        again = plan_opening_managers({21: 1, 22: 2}, year=2026, world_seed=7, used_names=set())
        self.assertEqual([a.member for a in first], [a.member for a in again])


class PlanManagerChangesTest(TestCase):
    YEAR = 2030

    def serving(self, birth: date, team_id=10) -> StaffAssignment:
        return StaffAssignment(member=member(1, birth), tenure=tenure(1, team_id, 2026))

    def test_a_manager_who_will_be_seventy_retires_and_a_successor_starts_next_year(self):
        # 2031年4月1日に70歳
        changes = plan_manager_changes(
            [self.serving(date(1961, 3, 1))], [10], year=self.YEAR, world_seed=1, used_names=set()
        )
        self.assertEqual(changes.retirements, (tenure(1, 10, 2026, self.YEAR),))
        self.assertEqual(len(changes.appointments), 1)
        appointment = changes.appointments[0]
        self.assertEqual((appointment.team_id, appointment.first_year), (10, self.YEAR + 1))
        self.assertEqual(changes.count, 1)

    def test_a_young_manager_stays(self):
        changes = plan_manager_changes(
            [self.serving(date(1980, 1, 1))], [10], year=self.YEAR, world_seed=1, used_names=set()
        )
        self.assertEqual(changes.retirements, ())
        self.assertEqual(changes.appointments, ())

    def test_a_team_without_a_manager_gets_one_from_next_year(self):
        changes = plan_manager_changes([], [10, 11], year=self.YEAR, world_seed=1, used_names=set())
        self.assertEqual(changes.retirements, ())
        self.assertEqual([(a.team_id, a.first_year) for a in changes.appointments], [(10, 2031), (11, 2031)])

    def test_the_result_is_reproducible(self):
        serving = [
            StaffAssignment(member=member(i, date(1964, 1, 1)), tenure=tenure(i, 10 + i, 2026)) for i in range(3)
        ]
        first = plan_manager_changes(serving, [10, 11, 12], year=self.YEAR, world_seed=3, used_names=set())
        again = plan_manager_changes(serving, [10, 11, 12], year=self.YEAR, world_seed=3, used_names=set())
        self.assertEqual(first, again)
