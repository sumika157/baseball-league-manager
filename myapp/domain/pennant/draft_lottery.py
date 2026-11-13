"""ドラフトの抽選の記録（#298 D4）。**純粋な値**で、DB にも Django にも触れない。

会議の行動の行（`DraftRecord`）は締めた時点で消えるので、締めた後は「どの球団とどの球団が同じ候補に入札したか」を
どこからも導けない。そこで締める処理が、その年の会議（GM が開いた会議を再生したもの、または全自動で進めたもの）の
抽選（`draft_meeting.Lottery`）を、指名された選手に結びつけて写す（2026-10-12 ユーザー決定）。

- 会議の間はこの記録を書かない（出典は行動の行だけ）。締めた後はこの記録だけが出典。どの時点でも出典は1つで、
  CLAUDE.md「同じ事実の出典を2つ作らない」の例外（表彰・日本一のような「導ける値の保存」）ではない
- 導ける部分は持たない: 年・巡・本指名か育成かは指名された選手の `DraftPick`、当たった球団は選手の最初の在籍の球団
  （`DraftPick` が指名した球団を持たないのと同じ理由）。持つのは「指名された選手・入札の回・入札した球団」だけ
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .draft_meeting import Lottery

# 抽選になる入札の最少の球団数
MIN_BIDDERS = 2


def contest_label(bidder_count: int) -> str:
    """「3球団競合」。**抽選の表記の唯一の出典**（会議の画面とオフの結果が使う）。"""
    return f"{bidder_count}球団競合"


@dataclass(frozen=True)
class DraftLotteryRecord:
    """締めた年の抽選ひとつ。`player_id` は当たった球団が指名した選手、`attempt` は入札の回（1が最初の入札、
    2以降は外れた球団の再入札）、`bidder_ids` は入札した球団（当たった球団を含む。id の昇順）。"""

    player_id: int
    attempt: int
    bidder_ids: tuple[int, ...]

    def __post_init__(self) -> None:
        if self.attempt < 1:
            raise ValueError("入札の回は1以上です。")
        bidders = tuple(sorted(set(self.bidder_ids)))
        if len(bidders) != len(self.bidder_ids) or len(bidders) < MIN_BIDDERS:
            raise ValueError(f"抽選は{MIN_BIDDERS}球団以上の、重ならない入札です（{self.bidder_ids}）。")
        object.__setattr__(self, "bidder_ids", bidders)

    @property
    def label(self) -> str:
        return contest_label(len(self.bidder_ids))


# 指名の鍵: (球団 id, 区分の中の巡, 育成の巡か)。球団は1つの巡で1人しか指名しないので、新人1人に決まる
DraftKey = tuple[int, int, bool]


def lottery_records(lotteries: Sequence[Lottery], drafted: Mapping[DraftKey, int]) -> tuple[DraftLotteryRecord, ...]:
    """会議の抽選を、指名された選手に結びつける（締める処理が新人を保存した後に呼ぶ）。

    `drafted` は「当たった球団・巡・育成か」→ 保存した新人の選手 id。抽選で当たった球団は、その巡でその候補を
    指名している（`DraftState` が抽選の当選を指名として確定させる）ので、鍵は必ず見つかる。見つからなければ
    会議と新人が食い違っているので `ValueError`（黙って記録を落とさない）。
    """
    records: list[DraftLotteryRecord] = []
    for lottery in lotteries:
        key = (lottery.winner, lottery.round_number, lottery.is_development)
        if key not in drafted:
            raise ValueError(f"抽選で当たった球団の新人が見つかりません（{key}）。")
        records.append(DraftLotteryRecord(player_id=drafted[key], attempt=lottery.attempt, bidder_ids=lottery.bidders))
    return tuple(records)
