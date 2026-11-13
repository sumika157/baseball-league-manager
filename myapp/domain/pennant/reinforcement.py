"""シーズン中の補強の窓（#207）と、期中の支配下登録の記録（#170 P3a）。

**純粋な値と関数**で、DB にも Django にも触れない。

規則（2026-10-12 ユーザーと決定。Issue #207。始まりは 2026-11-12 ユーザー決定 Q4 A。Issue #170）:

- **シーズン中に支配下を増やす・入れ替える（補強する）のは、シーズンを締めた後（開幕前）から7月31日まで**
  （NPB の野球協約: シーズン中に支配下選手を新しく登録できるのは7月31日まで）。期限の日付の出典は
  `ReinforcementWindow.DEADLINE` だけ。
- 判定は世界の「次の公式戦の日」（未消化の対戦の最初の日）で行う。補強した選手は次の試合から出られるので、
  その日が期限の日以前なら補強できる。公式戦の対戦が残っていない（シーズン終了・ポストシーズン・締める前）なら
  補強できない。**締める処理（公式戦の対戦が残っていないときだけ動く）とは時期が重ならない。**
- **補強の手段はすべてこの窓を通す**: 期中の昇格（#170 P3a）・期中の外国人の獲得（#169 F3）・トレード（#40）。
  期限を過ぎたら `ReinforcementClosed`（日本語）で断り、画面は期限の後は補強の導線を出さない。
- **オフは対象外**: 締める処理の中のドラフト・戦力外・オフの昇格・オフの外国人の獲得は、この窓を通さない。
- **実データには適用しない**（管理画面では過去の年の在籍も入力する）。窓を守るのはペナントの世界の操作だけ。

期中の登録の日付は保存する（`MidseasonRegistration`。CLAUDE.md の「同じ事実の出典を2つ作らない」には当たらない:
在籍は年単位で、オフの昇格（締めた年 Y に `promoted_year = Y+1` で書く）と Y+1 年の期中の昇格を在籍からは
区別できないため、登録日だけを足す）。登録日の年と在籍の昇格の年が一致することは
`ensure_registration_matches_stint` が照合する。
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import ClassVar

from ..exceptions import InvalidContract, ReinforcementClosed


class ReinforcementWindow:
    """シーズン中の補強の窓（締めた後〜7月31日）。状態を持たない規則の置き場所。"""

    # 期限（月・日）。**期限の日付の出典はここだけ**（画面の文言・AI の判定日・検査はここから作る）
    DEADLINE: ClassVar[tuple[int, int]] = (7, 31)
    # AI が期中の補強を決める日（月・日。#170 Q2 A の 6/1・7/1。#169 F3 の期中の獲得も同じ日に乗せる）
    REVIEW_DAYS: ClassVar[tuple[tuple[int, int], ...]] = ((6, 1), (7, 1))

    @classmethod
    def deadline(cls, year: int) -> datetime.date:
        """`year` 年の補強の期限の日（この日の試合までに登録すれば、この日から出られる）。"""
        month, day = cls.DEADLINE
        return datetime.date(year, month, day)

    @classmethod
    def is_open(cls, *, next_regular_game_on: datetime.date | None) -> bool:
        """いま補強できるか。次の公式戦の日がその年の期限の日以前なら開いている。

        公式戦の対戦が残っていない（シーズン終了・ポストシーズン）なら閉じている。締めた直後（翌年の開幕前）は、
        次の公式戦が翌年の開幕日なので開いている。
        """
        return next_regular_game_on is not None and next_regular_game_on <= cls.deadline(next_regular_game_on.year)

    @classmethod
    def ensure_open(cls, *, next_regular_game_on: datetime.date | None) -> None:
        """補強できなければ `ReinforcementClosed`。文言の出典はここだけ。"""
        if cls.is_open(next_regular_game_on=next_regular_game_on):
            return
        month, day = cls.DEADLINE
        if next_regular_game_on is None:
            raise ReinforcementClosed(
                "公式戦が残っていないので、いまは補強できません"
                f"（補強できるのは、シーズンを締めた後から{month}月{day}日までです）。"
            )
        raise ReinforcementClosed(
            f"{next_regular_game_on.year}年の補強の期限（{month}月{day}日）を過ぎたので、"
            "シーズンを締めるまで補強できません。"
        )

    @classmethod
    def review_dates(cls, year: int) -> tuple[datetime.date, ...]:
        """`year` 年に AI が期中の補強を決める日（古い順）。どれも期限の日以前。"""
        return tuple(datetime.date(year, month, day) for month, day in cls.REVIEW_DAYS)


@dataclass(frozen=True)
class MidseasonRegistration:
    """期中の支配下登録ひとつ（球団・選手・登録日）。登録日はその選手が出られる最初の日（次の公式戦の日）。

    期中の昇格（#170 P3a）が書く。#169 F3 の期中の外国人の獲得も同じ表に乗せる予定。
    """

    team_id: int
    player_id: int
    registered_on: datetime.date


def ensure_registration_matches_stint(registered_on: datetime.date, promoted_year: int | None) -> None:
    """期中の登録日と、在籍の昇格の年が合うか（同じ事実が2か所にあるので照合する）。合わなければ `InvalidContract`。

    期中の昇格は登録日の年で在籍に書く（`Team.promote_player(year=登録日の年)`）。
    """
    if promoted_year != registered_on.year:
        raise InvalidContract(
            f"支配下登録の日（{registered_on.isoformat()}）と、在籍の支配下登録の年（{promoted_year}）が合いません。"
        )
