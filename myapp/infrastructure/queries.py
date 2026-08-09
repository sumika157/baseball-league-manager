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
            .select_related('league', 'home_stadium')
            # 在籍中＝退団年が空の在籍
            .annotate(
                active_player_count=Count('stints', filter=Q(stints__to_year__isnull=True))
            )
            # 管理画面で手動設定した表示順を既定にする
            .order_by('display_order', 'name')
        )
        return [
            TeamSummary(
                id=row.id,
                name=row.name,
                league_id=row.league_id,
                league_name=row.league.name,
                player_count=row.active_player_count,
                stadium_name=row.home_stadium.name if row.home_stadium else '',
                # 所在地は球場から取る。チーム側には持たせない
                city=row.home_stadium.city if row.home_stadium else '',
            )
            for row in rows
        ]
