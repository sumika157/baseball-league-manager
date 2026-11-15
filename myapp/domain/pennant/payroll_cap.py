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

from ..exceptions import InvalidSigning
from .contract import MAN, salary_label

# 上限 ＝ 前年の収入 × この割合（#394。毎年1〜3割の球団が上限に当たる強さの目安。実測は Wiki
# 「ペナントモードの仕組み」の
# 「年俸の上限（K1b）」）
PAYROLL_CAP_RATE = 0.30
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

    def fits(self, payroll: int) -> bool:
        """年俸の合計 `payroll` が上限に収まるか（ちょうどは収まる）。**収まるかの判定の唯一の出典**（獲得の検査・
        AI の候補の絞り込み・画面の「上限を超える」が通る）。"""
        return payroll <= self.yen


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


def room_factor(*, budget: int, at_zero: int, at_full: int) -> float:
    """AI の契約更改の上げ幅に掛ける倍率（0〜1。`salary.ai_renewal_offer` の `room_factor`）。

    `budget` は更改に使える額（翌年の上限 − 新人の取り置き）、`at_zero`・`at_full` は倍率0・1で更改したときの
    年俸の合計。予算が倍率1の合計以上なら1（K1a と同じ更改）、倍率0の合計以下なら0（上げない。下げは減額制限の
    とおり）、間は一次で補う（上げ幅は倍率に比例するので、合計がおおむね予算に収まる）。1を超えない（余裕で上げ幅を
    相場より大きくしない。#394 の見送り）。
    """
    if budget >= at_full:
        return 1.0
    if budget <= at_zero or at_full <= at_zero:
        return 0.0
    return (budget - at_zero) / (at_full - at_zero)


def ensure_within_cap(payroll: int, cap: PayrollCap) -> None:
    """獲得の後の年俸の合計 `payroll` が上限に収まるか。超えたら `InvalidSigning`（ちょうどは通す）。

    止めるのは年俸の増える獲得だけで、更改は上限を超えても減額制限のまま通す（#394 の計画）。
    """
    if not cap.fits(payroll):
        raise InvalidSigning(
            f"年俸の合計が上限（{salary_label(cap.yen)}）を超えます（獲得すると{salary_label(payroll)}）。"
        )
