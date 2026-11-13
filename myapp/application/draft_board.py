"""ドラフトの盤（巡 × 球団。#298 D4）の材料を作る。

会議の画面（`PennantDraftService.view`。会議の途中の指名）とオフの結果（`OffseasonViewService`。入団した新人）が、
同じ関数で同じ形（`DraftBoard`）を作り、同じ部分テンプレート（`pennant/_draft_board.html`）で出す。
抽選の表記（「N球団競合」）の出典は domain の `draft_lottery.contest_label`（`DraftLotteryView.label`）。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .dto import DraftBoard, DraftBoardCell, DraftBoardColumn, DraftBoardRow, draft_round_label


@dataclass(frozen=True)
class BoardPick:
    """盤に置く指名ひとつ（球団・区分の中の巡・育成か・セルの中身）。"""

    round_number: int
    is_development: bool
    cell: DraftBoardCell


def draft_board(columns: Sequence[DraftBoardColumn], picks: Iterable[BoardPick]) -> DraftBoard | None:
    """列（球団）の指名から盤を作る。行は本指名の巡 → 育成の巡の順で、どこかの列が指名した巡まで。

    列に無い球団の指名は置かない（リーグで絞った盤）。置く指名が1つも無ければ None。
    同じ球団・同じ巡の指名が2つあれば `ValueError`（球団は1つの巡で1人しか指名しない）。
    """
    index = {column.team_id: position for position, column in enumerate(columns)}
    grid: dict[tuple[bool, int], list[DraftBoardCell | None]] = {}
    for pick in picks:
        position = index.get(pick.cell.team_id)
        if position is None:
            continue
        cells = grid.setdefault((pick.is_development, pick.round_number), [None] * len(columns))
        if cells[position] is not None:
            raise ValueError(f"同じ球団が同じ巡で2人を指名しています（{pick.cell.team_id}・{pick.round_number}）。")
        cells[position] = pick.cell
    if not grid:
        return None
    rows: list[DraftBoardRow] = []
    for is_development in (False, True):
        last = max((round_number for dev, round_number in grid if dev is is_development), default=0)
        rows.extend(
            DraftBoardRow(
                round_label=draft_round_label(round_number, is_development=is_development),
                is_development=is_development,
                cells=tuple(grid.get((is_development, round_number), [None] * len(columns))),
            )
            for round_number in range(1, last + 1)
        )
    return DraftBoard(columns=tuple(columns), rows=tuple(rows))
