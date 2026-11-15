"""ペナントの球団の年俸の総額の上限（#40 K1b）。**純粋な値と規則**で、DB にも Django にも触れない。

上限は**保存せず導く**（#394 の計画）。ある年の上限 ＝ 前年の収入（親会社の補填を含まない。
`finance.ClubFinance.revenue`）× `PAYROLL_CAP_RATE`。「稼いだ球団ほど払える」を編成に効かせるための数で、
**上限の唯一の出典**はこのモジュール。

- 前年の収入が無い年（開幕年・人気を記録する前の年の翌年）は、標準の球団の収入の見込み
  （`finance.expected_revenue`）から作る（`PayrollCap.estimated`）
- 上限を超えた分（`payroll_overrun`）は親会社が補填する（黒字でも払う。2026-11-15 ユーザー決定。
  `finance.parent_subsidy`）
- 首脳陣の年俸（#210 M3）は上限に含めない
"""

from __future__ import annotations

from dataclasses import dataclass

from .contract import MAN

# 上限 ＝ 前年の収入 × この割合（#394。毎年1〜3割の球団が上限に当たる強さの目安。実測は Wiki
# 「ペナントモードの仕組み」の
# 「年俸の上限（K1b）」）
PAYROLL_CAP_RATE = 0.32
# 上限の丸めの単位（1,000万円。切り捨て。画面の表記＝億円の小数1桁と同じ細かさで、表記と検査の額がずれない）
CAP_STEP = 1_000 * MAN


@dataclass(frozen=True)
class PayrollCap:
    """ある球団 × 年の年俸の総額の上限（円）。`estimated` は前年の収入が無く、標準の球団の見込みから作った値。"""

    yen: int
    estimated: bool

    def __post_init__(self) -> None:
        if self.yen < 0:
            raise ValueError("年俸の上限は0以上にしてください。")

    def room(self, payroll: int) -> int:
        """上限までの残り（円）。超えていれば負。"""
        return self.yen - payroll


def payroll_cap(previous_revenue: int | None, *, expected_revenue: int) -> PayrollCap:
    """ある年の年俸の総額の上限。**上限の唯一の出典**。

    `previous_revenue` は前年の収入（補填を含まない）。無ければ（None）`expected_revenue`（標準の球団の収入の
    見込み）から作り、`estimated` を立てる。収入 × `PAYROLL_CAP_RATE` を 1,000万円の単位に切り捨てる。
    """
    estimated = previous_revenue is None
    base = expected_revenue if previous_revenue is None else previous_revenue
    if base < 0:
        raise ValueError("収入は0以上にしてください。")
    # 円に丸めてから切り捨てる（割合の浮動小数の誤差で、ちょうどの額が1段下に落ちないように）
    return PayrollCap(yen=round(base * PAYROLL_CAP_RATE) // CAP_STEP * CAP_STEP, estimated=estimated)


def payroll_overrun(payroll: int, cap: PayrollCap) -> int:
    """年俸の合計 `payroll` が上限を超えた分（円。超えていなければ0。ちょうどは0）。"""
    return max(0, payroll - cap.yen)
