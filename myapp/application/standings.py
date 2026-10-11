"""リーグ別の順位表の組み立て。順位の規則は domain の `standings` が唯一の出典。

`TeamApplicationService`（実データ・ペナントの順位表）と、ペナントの世界の終了の通算（年ごとの順位）が
同じ関数を通る。並べ替えは表示の都合なので、ここには持たない（呼ぶ側が行う）。
"""

from __future__ import annotations

from collections.abc import Sequence

from ..domain import services as domain_services
from ..domain.entities import Game, League, Team
from ..domain.value_objects import format_average
from .dto import LeagueStandings, StandingRow


def standing_row_of(row: domain_services.StandingRow) -> StandingRow:
    """domain の順位の1行を表示用に写す。順位と勝率は勝敗から算出した結果（ここで計算し直さない）。"""

    return StandingRow(
        rank=row.rank,
        team_id=row.team_id,
        team_name=row.team_name,
        wins=row.record.wins,
        losses=row.record.losses,
        ties=row.record.ties,
        games_played=row.record.games_played,
        winning_percentage=format_average(row.record.winning_percentage),
        games_behind="—" if row.is_leader else f"{row.games_behind:.1f}",
    )


def league_standings(teams: Sequence[Team], leagues: Sequence[League], games: list[Game]) -> list[LeagueStandings]:
    """リーグ別の順位。試合の無いリーグは載せない。`games` は対象シーズンに絞ったものを渡す。"""
    result = []
    for league in leagues:
        members = [team for team in teams if team.league_id == league.id]
        rows = domain_services.standings(members, games)
        if not rows:
            continue
        assert league.id is not None, "保存済みのリーグには id がある"
        result.append(
            LeagueStandings(league_id=league.id, league_name=league.name, rows=[standing_row_of(row) for row in rows])
        )
    return result
