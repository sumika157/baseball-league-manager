"""参照専用のクエリ（リードモデル）。

一覧表示のように「集約の不変条件を扱わず、値を読むだけ」の処理は、
集約を組み立てずに直接 DTO を作った方が素直で速い。
更新はリポジトリ経由（集約単位）、参照はこちら、と役割を分ける。
"""

from __future__ import annotations

from django.db.models import Count, Q

from ..application.dto import TeamSummary
from . import orm_models


class DjangoTeamListQuery:
    """チーム一覧に必要な値だけを取得する。"""

    def list_summaries(self) -> list[TeamSummary]:
        rows = (
            orm_models.Team.objects
            .select_related('league')
            .annotate(active_player_count=Count('players', filter=Q(players__is_active=True)))
            # 管理画面で手動設定した表示順を既定にする
            .order_by('display_order', 'name')
        )
        return [
            TeamSummary(
                id=row.id,
                name=row.name,
                city=row.city,
                league_name=row.league.name,
                player_count=row.active_player_count,
            )
            for row in rows
        ]
