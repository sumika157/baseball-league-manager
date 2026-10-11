"""開幕前の支配下の人数の帯。**帯の出典はここだけ**。純粋な関数で、DB にも Django にも触れない。

シーズンを締めた後（翌年の開幕前）の支配下は、獲得手段すべての結果としてこの帯に入るようにする
（NPB の支配下は上限70人で、各球団60人台で開幕を迎えるのが普通）。いまの獲得手段はドラフトだけなので、
ドラフト（`draft.draftee_count`）が帯までの不足・空きを埋める。戦力外（#168）・外国人の獲得（#169）・
トレード（#40）・育成の昇格（#170）を足すときは、その手段で埋まる見込みを `expected` に渡し、
ドラフトはその残りを埋める（帯の数字を手段ごとに持たない）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RegisteredBand:
    """支配下の人数の帯（下端・上端。両端を含む）。"""

    low: int
    high: int

    def gap(self, registered: int, *, limit: int | None, expected: int = 0) -> RosterGap:
        """`registered` 人（引退・退団の後の支配下）の球団が、帯に入るまでの不足と空き。

        - `limit`: リーグの支配下の上限。None は無制限。上端より低ければ上限が天井になる
        - `expected`: ドラフトより前に、ほかの獲得手段で埋まる見込みの人数（いまは手段が無いので 0）

        帯の上端（または上限）に達している・超えている球団は、不足も空きも 0（減らす手段は帯の外の話）。
        """
        after = registered + expected
        ceiling = self.high if limit is None else min(self.high, limit)
        vacancy = max(0, ceiling - after)
        return RosterGap(shortfall=min(vacancy, max(0, self.low - after)), vacancy=vacancy)


@dataclass(frozen=True)
class RosterGap:
    """帯に入るまでの人数。`shortfall` は下端までの不足、`vacancy` は上端（または上限）までの空き。"""

    shortfall: int
    vacancy: int


# 開幕前の支配下の帯（62〜66人。2026-10-07 ユーザー決定）
OPENING_REGISTERED_BAND = RegisteredBand(low=62, high=66)
