"""ペナントの球団の年俸の総額の上限（#40 K1b・#394）の読み口と画面。

決定（2026-11-15。#394 のコメント「ユーザーの決定」）と計画から:
- 上限 ＝ 前年の収入（補填を含まない）× 割合。開幕年（前年の収入が無い）は標準の球団の見込みから
- 前年の人気の行が無い年は「収入が無い」として見込みに回す
- 超えた分は黒字でも親会社が払う。経営の画面は補填を「上限超過」と「残る赤字」の2行で見せる
- GM ホームの「年俸」のカードに上限と残り（超えていれば超過）を出す

世界は `test_pennant_finance.FinanceCase`（2リーグ×3球団・収容人数3万人）。
"""

from __future__ import annotations

from unittest import mock

from django.urls import reverse

from myapp.application.pennant_payroll_cap import cap_of
from myapp.domain.pennant import payroll_cap as payroll_cap_module
from myapp.infrastructure import orm_models
from myapp.presentation.views import build_club_finance_service, build_pennant_home_service

from .test_pennant_finance import CAPACITY, FinanceCase
from .world_case import YEAR


class PayrollCapTest(FinanceCase):
    def test_the_opening_year_uses_the_expected_revenue(self):
        """開幕年は前年の収入が無いので、標準の球団の見込みから（収容人数で決まる）。"""
        rows = build_club_finance_service(self.world_id).year_rows(self.context(), YEAR, self.teams)
        expected = cap_of(None, CAPACITY)
        self.assertTrue(expected.estimated)
        for team_id, row in rows.items():
            with self.subTest(team_id=team_id):
                assert row.figures is not None
                self.assertEqual(row.figures.payroll_cap.amount, expected.yen)
                self.assertTrue(row.figures.cap_estimated)

    def test_the_next_year_uses_the_revenue_of_the_closed_year(self):
        self.finish_regular_season()
        self.close()

        service = build_club_finance_service(self.world_id)
        closed = service.year_rows(self.context(), YEAR, self.teams)
        following = service.year_rows(self.context(), YEAR + 1, self.teams)
        for team_id in self.teams:
            with self.subTest(team_id=team_id):
                past, now = closed[team_id].figures, following[team_id].figures
                assert past is not None and now is not None
                self.assertEqual(now.payroll_cap.amount, cap_of(past.revenue.amount, CAPACITY).yen)
                self.assertFalse(now.cap_estimated)
        # 経営の画面（球団ひとつの年ごとの表）も同じ値（同じ関数で導く）
        view = service.view(self.context())
        assert view is not None and view.current.figures is not None
        self.assertEqual(view.current.figures.payroll_cap, following[self.teams[0]].figures.payroll_cap)
        (past_row,) = view.history
        assert past_row.figures is not None
        self.assertTrue(past_row.figures.cap_estimated, "開幕年の上限は見込みのまま")

    def test_a_previous_year_without_popularity_falls_back_to_the_expected_revenue(self):
        """前年の人気の行が無い（人気を記録する前の年）なら、その年の収入は無いとして見込みから。"""
        self.finish_regular_season()
        self.close()
        orm_models.PennantClubPopularity.objects.filter(team__league__world_id=self.world_id, year=YEAR).delete()

        rows = build_club_finance_service(self.world_id).year_rows(self.context(), YEAR + 1, self.teams)
        figures = rows[self.teams[0]].figures
        assert figures is not None
        self.assertEqual(figures.payroll_cap.amount, cap_of(None, CAPACITY).yen)
        self.assertTrue(figures.cap_estimated)

    def test_an_overrun_is_paid_by_the_parent_company_even_in_the_black(self):
        """上限を低くすると、超えた分が補填の1行目になり、補填後の収支に上乗せされる。"""
        self.finish_regular_season()
        with mock.patch.object(payroll_cap_module, "PAYROLL_CAP_RATE", 0.001):
            row = build_club_finance_service(self.world_id).year_rows(self.context(), YEAR, self.teams)[self.teams[0]]
            content = self.client.get(self.url()).content.decode()
        f = row.figures
        assert f is not None
        self.assertEqual(f.overrun_subsidy.amount, f.payroll.amount - f.payroll_cap.amount)
        self.assertGreater(f.overrun_subsidy.amount, 0)
        self.assertEqual(f.deficit_subsidy.amount, max(0, -(f.operating_balance.amount + f.overrun_subsidy.amount)))
        self.assertEqual(f.subsidy.amount, f.overrun_subsidy.amount + f.deficit_subsidy.amount)
        self.assertEqual(f.balance.amount, f.operating_balance.amount + f.subsidy.amount)
        self.assertIn("親会社の補填（年俸の上限を超えた分）", content)
        self.assertIn("親会社の補填（残る赤字）", content)
        self.assertIn(f.overrun_subsidy.label, content)


class PayrollCardTest(FinanceCase):
    def test_the_home_card_shows_the_cap_and_the_room(self):
        home = build_pennant_home_service(self.world_id).get_home(self.context(), include_advance=False)
        payroll = home.payroll
        assert payroll is not None
        cap = cap_of(None, CAPACITY)
        self.assertEqual((payroll.cap.amount, payroll.cap_estimated), (cap.yen, True))
        self.assertEqual(payroll.room.amount, max(0, cap.yen - payroll.total.amount))
        content = self.client.get(reverse("pennant_world", args=[self.world_id])).content.decode()
        self.assertIn("年俸の上限（見込み）", content)
        self.assertIn(f"残り {payroll.room.label}", content)

    def test_the_home_card_shows_the_overrun(self):
        with mock.patch.object(payroll_cap_module, "PAYROLL_CAP_RATE", 0.001):
            home = build_pennant_home_service(self.world_id).get_home(self.context(), include_advance=False)
            content = self.client.get(reverse("pennant_world", args=[self.world_id])).content.decode()
        payroll = home.payroll
        assert payroll is not None
        self.assertEqual(payroll.overrun.amount, payroll.total.amount - payroll.cap.amount)
        self.assertGreater(payroll.overrun.amount, 0)
        self.assertEqual(payroll.room.amount, 0)
        self.assertIn(f"{payroll.overrun.label} 超過", content)
