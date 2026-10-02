"""試合詳細に出すスコアブック（打順 × 回）の組み立て。

編集画面（React の `ScorebookCard`）のマス目と同じ構成を、読み取り専用の
DTO にする。結果の表記は `PlateAppearanceResult.label` が出典で、ここに表を持たない。
"""

from __future__ import annotations

from collections.abc import Callable

from ..domain.entities import BATTING_ORDER_SIZE, Game
from .dto import ScorebookGrid, ScorebookMark, ScorebookRow


def build_scorebook_grids(
    game: Game, team_names: dict[int, str], batter_name: Callable[[int], str]
) -> list[ScorebookGrid]:
    """ビジター → ホームの順に、チームごとのスコアブックを返す。打席が無ければ空。"""
    if not game.plate_appearances:
        return []
    ordered = game.plate_appearances_in_order()
    sides = ((False, game.away_team_id), (True, game.home_team_id))
    grids = []
    for is_bottom, team_id in sides:
        entries = [entry for entry in ordered if entry.is_bottom == is_bottom]
        # 列数は編集画面と同じ。打席のある最後の回まで（最低1列）
        innings = list(range(1, max([1, *(entry.inning for entry in entries)]) + 1))
        rows = []
        for order in range(1, BATTING_ORDER_SIZE + 1):
            cells = [
                [
                    ScorebookMark(result=entry.result.label, batter_name=batter_name(entry.batter_id))
                    for entry in entries
                    if entry.batting_order == order and entry.inning == inning
                ]
                for inning in innings
            ]
            rows.append(ScorebookRow(batting_order=order, cells=cells))
        grids.append(ScorebookGrid(team_name=team_names.get(team_id, ""), innings=innings, rows=rows))
    return grids
