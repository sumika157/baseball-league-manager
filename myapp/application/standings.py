"""リーグ別の順位表の組み立て。順位の規則は domain の `standings` が唯一の出典。

`LeagueViewService`（実データ・ペナントの順位表）と、ペナントの世界の終了の通算（年ごとの順位）が
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
        # 順位は所属球団の試合（交流戦を含む）だけで決まるので、リーグごとに試合を先に絞る
        member_ids = {team.id for team in members}
        league_games = [g for g in games if g.home_team_id in member_ids or g.away_team_id in member_ids]
        rows = domain_services.standings(members, league_games)
        if not rows:
            continue
        assert league.id is not None, "保存済みのリーグには id がある"
        result.append(
            LeagueStandings(league_id=league.id, league_name=league.name, rows=[standing_row_of(row) for row in rows])
        )
    return result


def record_label(wins: int, losses: int, ties: int) -> str:
    """「80勝60敗3分」（引分が無ければ「80勝60敗」）。勝敗の文言の唯一の出典。"""
    return f"{wins}勝{losses}敗" + (f"{ties}分" if ties else "")
