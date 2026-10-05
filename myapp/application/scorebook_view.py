"""試合詳細に出すスコアブック（打順 × 回）とスコアボードの組み立て。

編集画面（React の `ScorebookCard`）のマス目と同じ構成を、読み取り専用の
DTO にする。結果の表記は `PlateAppearanceResult.label` が出典で、ここに表を持たない。
"""

from __future__ import annotations

from collections.abc import Callable

from ..domain import services as domain_services
from ..domain.entities import BATTING_ORDER_SIZE, Game
from .dto import GameLineScore, InningScoreColumn, ScorebookGrid, ScorebookMark, ScorebookRow


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


def build_line_score(game: Game, hits_of: Callable[[int], int]) -> GameLineScore | None:
    """スコアボード（回ごとの得点）。回ごとの得点が記録されていなければ None。

    安打・失策は打席が出典（打撃明細の合計とは照合済みで一致する）。打席の無い古い記録だけは、
    安打を `hits_of`（チームの id → 打撃明細の安打の合計）から取り、失策は数えられないので None にする。
    """
    score = game.line_score
    if score.is_empty:
        return None

    columns = []
    for inning in range(1, score.innings + 1):
        away = str(score.runs_in(inning, home=False))
        # ホームが最終回を攻めずに終わった場合は 'X' を置く（記録の慣例）
        home = str(score.runs_in(inning, home=True)) if inning <= len(score.home) else "X"
        columns.append(InningScoreColumn(inning=inning, away=away, home=home))

    plate_appearances = game.plate_appearances
    if plate_appearances:
        # 失策は守備側のチームに付くので、ホームの失策は表の打席から数える
        return GameLineScore(
            columns=columns,
            away_total=score.away_total,
            home_total=score.home_total,
            away_hits=sum(domain_services.hits_by_inning(plate_appearances, home=False).values()),
            home_hits=sum(domain_services.hits_by_inning(plate_appearances, home=True).values()),
            away_errors=sum(domain_services.errors_by_inning(plate_appearances, home=False).values()),
            home_errors=sum(domain_services.errors_by_inning(plate_appearances, home=True).values()),
        )

    return GameLineScore(
        columns=columns,
        away_total=score.away_total,
        home_total=score.home_total,
        away_hits=hits_of(game.away_team_id),
        home_hits=hits_of(game.home_team_id),
    )
