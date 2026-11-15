"""ペナントの球団の年俸の総額の上限（#40 K1b・#394）。Django も DB も使わない。

守りたいこと（#394 の計画と 2026-11-15 のユーザーの決定）:
- 上限 ＝ 前年の収入（補填を含まない）× 割合。1,000万円の単位に切り捨てる
- 前年の収入が無い年は、標準の球団の収入の見込みから作り、見込みだと分かるようにする
- 超えた分は「上限を超えた分」（ちょうどは超えていない）
"""

from unittest import TestCase, mock

from myapp.domain.pennant import payroll_cap as payroll_cap_module
from myapp.domain.pennant.contract import MAN
from myapp.domain.pennant.finance import OKU
from myapp.domain.pennant.payroll_cap import (
    PAYROLL_CAP_RATE,
    PayrollCap,
    payroll_cap,
    payroll_overrun,
)


class PayrollCapTest(TestCase):
    def test_the_cap_is_the_previous_revenue_times_the_rate(self):
        cap = payroll_cap(125 * OKU, expected_revenue=999 * OKU)
        self.assertEqual(cap, PayrollCap(yen=round(125 * PAYROLL_CAP_RATE * 10) * 1_000 * MAN, estimated=False))
        self.assertFalse(cap.estimated)

    @mock.patch.object(payroll_cap_module, "PAYROLL_CAP_RATE", 0.32)
    def test_the_cap_is_rounded_down_to_ten_million_yen(self):
        """割合の値によらない丸めの規則を、割合 0.32 で確かめる（浮動小数の 0.32 でもちょうどの額が1段下に
        落ちない）。"""
        # 100億円 × 0.32 ＝ 32億円ちょうど、100億円＋1円 でも 32億円（端数は切り捨て）
        self.assertEqual(payroll_cap(100 * OKU, expected_revenue=0).yen, 32 * OKU)
        self.assertEqual(payroll_cap(100 * OKU + 1, expected_revenue=0).yen, 32 * OKU)
        # 100億3,125万円 × 0.32 ＝ 32億1,000万円（1,000万円未満は出ない）
        self.assertEqual(payroll_cap(100 * OKU + 3_125 * MAN, expected_revenue=0).yen, 32 * OKU + 1_000 * MAN)
        self.assertEqual(payroll_cap(100 * OKU + 3_124 * MAN, expected_revenue=0).yen, 32 * OKU)

    def test_without_a_previous_revenue_the_expected_revenue_is_used(self):
        cap = payroll_cap(None, expected_revenue=130 * OKU)
        self.assertEqual(cap, PayrollCap(yen=payroll_cap(130 * OKU, expected_revenue=0).yen, estimated=True))
        # 前年の収入が0でも「無い」とは違う（見込みに回さない）
        self.assertEqual(payroll_cap(0, expected_revenue=130 * OKU), PayrollCap(yen=0, estimated=False))

    def test_a_richer_club_gets_a_higher_cap(self):
        self.assertLess(payroll_cap(110 * OKU, expected_revenue=0).yen, payroll_cap(150 * OKU, expected_revenue=0).yen)

    def test_the_overrun_and_the_room(self):
        cap = PayrollCap(yen=40 * OKU, estimated=False)
        self.assertEqual(payroll_overrun(40 * OKU, cap), 0, "ちょうどは超えていない")
        self.assertEqual(payroll_overrun(40 * OKU + MAN, cap), MAN)
        self.assertEqual(payroll_overrun(35 * OKU, cap), 0)
        self.assertEqual(cap.room(35 * OKU), 5 * OKU)
        self.assertEqual(cap.room(41 * OKU), -OKU)
        self.assertTrue(cap.fits(40 * OKU), "ちょうどは収まる")
        self.assertFalse(cap.fits(40 * OKU + 1))

    def test_negative_values_are_rejected(self):
        with self.assertRaises(ValueError):
            PayrollCap(yen=-1, estimated=False)
        with self.assertRaises(ValueError):
            payroll_cap(-1, expected_revenue=0)
