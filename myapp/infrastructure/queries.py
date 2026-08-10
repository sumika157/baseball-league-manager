"""参照専用のクエリ（リードモデル）。

一覧表示のように「集約の不変条件を扱わず、値を読むだけ」の処理は、
集約を組み立てずに直接 DTO を作った方が素直で速い。
更新はリポジトリ経由（集約単位）、参照はこちら、と役割を分ける。
"""

from __future__ import annotations

from django.db.models import Count, Q

from ..application.dto import PlayerSearchRow, TeamSummary
from . import orm_models


class DjangoPlayerSearchQuery:
    """選手を名前で探す。

    チーム数が増えると、所属を知らないと選手にたどり着けないため。
    集約を組み立てず、一覧に必要な値だけを読む。
    """

    LIMIT = 50

    def search(self, keyword: str) -> list[PlayerSearchRow]:
        keyword = (keyword or "").strip()
        if not keyword:
            return []

        rows = (
            orm_models.Player.objects.filter(name__icontains=keyword)
            .prefetch_related("stints__team__league")
            .order_by("name")[: self.LIMIT]
        )

        results = []
        for row in rows:
            stints = list(row.stints.all())
            current = next((s for s in stints if s.to_year is None), None)
            latest = current or (max(stints, key=lambda s: s.from_year) if stints else None)
            results.append(
                PlayerSearchRow(
                    id=row.id,
                    name=row.name,
                    position=row.position,
                    team_id=latest.team_id if latest else None,
                    team_name=latest.team.name if latest else "",
                    league_name=latest.team.league.name if latest else "",
                    number=latest.number if latest else None,
                    is_active=current is not None,
                )
            )
        return results


class DjangoTeamPermissionQuery:
    """チーム担当者の権限判定。

    ログインすれば誰でも全チームを編集できた状態をやめ、管理ユーザー
    （is_staff）以外は自分が担当するチームが関わる範囲だけ編集できるようにする。
    「担当者かどうか」は Team.managers という事実だけを見て決まるので、
    ドメインの業務ルールではなく、この参照専用クエリに置く。
    """

    def can_manage(self, user, team_id: int) -> bool:
        """指定チームを編集できるか。"""
        return self.can_manage_any(user, (team_id,))

    def can_manage_any(self, user, team_ids) -> bool:
        """渡したチームのうち、少なくとも1つを編集できるか。

        試合は2チームにまたがるため、どちらか一方の担当者であれば編集できる。
        """
        if not user.is_authenticated:
            return False
        if user.is_staff or user.is_superuser:
            return True
        return orm_models.Team.objects.filter(id__in=team_ids, managers=user).exists()


class DjangoTeamListQuery:
    """チーム一覧に必要な値だけを取得する。"""

    def list_summaries(self) -> list[TeamSummary]:
        rows = (
            orm_models.Team.objects.select_related("league", "home_stadium")
            # 在籍中＝退団年が空の在籍
            .annotate(active_player_count=Count("stints", filter=Q(stints__to_year__isnull=True)))
            # 管理画面で手動設定した表示順を既定にする
            .order_by("display_order", "name")
        )
        return [
            TeamSummary(
                id=row.id,
                name=row.name,
                league_id=row.league_id,
                league_name=row.league.name,
                player_count=row.active_player_count,
                stadium_name=row.home_stadium.name if row.home_stadium else "",
                # 所在地は球場から取る。チーム側には持たせない
                city=row.home_stadium.city if row.home_stadium else "",
            )
            for row in rows
        ]
