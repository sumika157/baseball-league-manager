"""ペナントの表彰（締めたシーズンのタイトル受賞者）を、試合から導く。#248

受賞者は**タイトルの画面と同じ集計**（世界の範囲の `LeagueViewService.get_league_titles`）の部門の1位で、
同率の1位は全員（`leaders=None` で部門の全員を順位づけてから1位を拾う。上位3人で切ると4人以上の同率が欠ける）。
ここで導いた受賞者を、シーズンを締める処理が表彰として保存し（`PennantOffseasonService`）、締めていない年は
歴代の記録・球団史・選手ページが同じ関数で導いて出す（`PennantHistoryService`）。受賞者の決め方を2つに増やさない。
"""

from __future__ import annotations

from collections.abc import Iterable

from ..domain.pennant.awards import Award
from ..domain.services.rankings import Title
from .dto import AwardRow
from .league_view import LeagueViewService


def derive_award_rows(titles: LeagueViewService, year: int, league_id: int) -> list[AwardRow]:
    """その年・そのリーグのタイトル受賞者（部門の1位。同率は全員）。並びは `AwardRow.sort_key`。"""
    rows = [
        AwardRow(
            year=year,
            league_id=league_id,
            title=Title(department.key),
            player_id=entry.player_id,
            player_name=entry.player_name,
            team_id=entry.team_id,
            team_name=entry.team_name,
            value=entry.value,
        )
        for department in titles.get_league_titles(league_id, year, leaders=None).departments
        for entry in department.entries or []
        if entry.rank == 1
    ]
    return sorted(rows, key=lambda row: row.sort_key)


def derive_awards(titles: LeagueViewService, year: int, league_ids: Iterable[int]) -> list[Award]:
    """その年の各リーグの表彰（保存する形）。シーズンを締める処理が、書き込みのトランザクションの外で呼ぶ。"""
    return [row.award() for league_id in league_ids for row in derive_award_rows(titles, year, league_id)]
