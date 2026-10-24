"""管理画面トップの概況（リーグ・チーム・選手の件数と、成績が未入力の選手の数）。

管理画面のテンプレートタグ（`templatetags/admin_overview.py`）だけが使う。読むだけで、書き込みはしない。
実データの範囲だけを読む（組み立て口が範囲を固定する）。
"""

from __future__ import annotations

from ..domain import services as domain_services
from ..domain.repositories import TeamRepository
from .dto import AdminOverview


class AdminOverviewService:
    """管理画面トップ用の概況を作るユースケース。"""

    def __init__(self, *, teams: TeamRepository) -> None:
        self._teams = teams

    def get_admin_overview(self) -> AdminOverview:
        """管理画面トップ用の概況。

        「成績が未入力か」の判定はドメインサービスの規定（打数0・投球回0を除く）を
        そのまま使う。ここで独自に条件を書くと、ランキングの対象と管理画面の
        警告がずれてしまう。
        """
        teams = self._teams.find_all_with_roster()
        active = [p for team in teams for p in team.active_players]
        retired = sum(1 for team in teams for p in team.players if not p.is_active)

        qualified = {id(p) for p in domain_services.qualified_batters(active)} | {
            id(p) for p in domain_services.qualified_pitchers(active)
        }

        return AdminOverview(
            league_count=len({team.league_id for team in teams}),
            team_count=len(teams),
            player_count=len(active),
            batter_count=sum(1 for p in active if not p.is_pitcher),
            pitcher_count=sum(1 for p in active if p.is_pitcher),
            players_without_stats=sum(1 for p in active if id(p) not in qualified),
            retired_count=retired,
            teams_without_players=sum(1 for team in teams if not team.active_players),
        )
