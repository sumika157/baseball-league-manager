"""参照専用のクエリ（リードモデル）。

一覧表示のように「集約の不変条件を扱わず、値を読むだけ」の処理は、
集約を組み立てずに直接 DTO を作った方が素直で速い。
更新はリポジトリ経由（集約単位）、参照はこちら、と役割を分ける。
"""

from __future__ import annotations

from collections.abc import Iterable

from django.contrib.auth.models import AnonymousUser, User
from django.db.models import Count, Q, QuerySet

from ..application.dto import GameRow, PlayerSearchRow, TeamSummary
from ..domain.entities import Game, winning_team_id
from ..domain.value_objects import Season
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

    def can_manage(self, user: User | AnonymousUser, team_id: int) -> bool:
        """指定チームを編集できるか。"""
        return self.can_manage_any(user, (team_id,))

    def can_manage_any(self, user: User | AnonymousUser, team_ids: Iterable[int]) -> bool:
        """渡したチームのうち、少なくとも1つを編集できるか。

        試合は2チームにまたがるため、どちらか一方の担当者であれば編集できる。
        """
        if not user.is_authenticated:
            return False
        if user.is_staff or user.is_superuser:
            return True
        return orm_models.Team.objects.filter(id__in=team_ids, managers=user).exists()


class DjangoGameListQuery:
    """GameListQuery の Django ORM 実装。試合一覧に必要な値だけを取得する。

    一覧に要るのは日付・チーム名・スコアだけなので、集約（Game）を組み立てない。
    集約経由だと1試合ごとに打撃・投球・イニングスコアの明細まで読むため、
    件数が増えると一覧が開かなくなる。
    """

    def _rows(
        self,
        *,
        year: int | None = None,
        team_id: int | None = None,
        month: int | None = None,
        league_id: int | None = None,
    ) -> QuerySet[orm_models.Game]:
        """絞り込みは SQL 側で行う。取得後に Python で捨てると件数ぶん無駄になる。"""
        rows = orm_models.Game.objects.select_related("home_team", "away_team")
        if year is not None:
            rows = rows.filter(year=year)
        if league_id is not None:
            # どちらかのチームが所属していれば、そのリーグの日程に含める
            # （リーグをまたぐ対戦も、両リーグの日程に現れる）
            rows = rows.filter(Q(home_team__league_id=league_id) | Q(away_team__league_id=league_id))
        if team_id is not None:
            rows = rows.filter(Q(home_team_id=team_id) | Q(away_team_id=team_id))
        if month is not None:
            rows = rows.filter(played_on__month=month)
        return rows

    def list_rows(
        self,
        *,
        year: int | None = None,
        team_id: int | None = None,
        month: int | None = None,
        league_id: int | None = None,
    ) -> list[GameRow]:
        rows = self._rows(year=year, team_id=team_id, month=month, league_id=league_id).order_by("-played_on", "-id")
        return [
            GameRow(
                id=row.id,
                year=row.year,
                played_on=row.played_on,
                home_team_id=row.home_team_id,
                home_team_name=row.home_team.name,
                away_team_id=row.away_team_id,
                away_team_name=row.away_team.name,
                home_score=row.home_score,
                away_score=row.away_score,
                # 勝敗の判定はドメインの関数が唯一の出典。結果の文言は GameRow が持つ
                winner_team_id=winning_team_id(row.home_team_id, row.away_team_id, row.home_score, row.away_score),
            )
            for row in rows
        ]

    def list_for_standings(self, *, year: int | None = None) -> list[Game]:
        """順位表の計算に渡す試合。**明細は読まない**。

        順位は得点と対戦カードだけで決まるので、打撃・投球・イニングスコアは
        要らない。リポジトリの find_all() は集約として明細まで揃えるため、
        順位表のためだけに呼ぶと件数ぶん無駄になる（3480試合で3.5秒かかった）。
        戻り値は成績を持たない Game なので、順位・勝敗の集計にだけ使う。
        """
        rows = orm_models.Game.objects.all()
        if year is not None:
            rows = rows.filter(year=year)
        return [
            Game(
                id=row.id,
                season=Season(row.year),
                played_on=row.played_on,
                home_team_id=row.home_team_id,
                away_team_id=row.away_team_id,
                home_score=row.home_score,
                away_score=row.away_score,
            )
            for row in rows
        ]

    def list_seasons(self) -> list[int]:
        """試合のある年を新しい順に。"""
        return sorted(orm_models.Game.objects.values_list("year", flat=True).distinct(), reverse=True)

    def list_months(
        self, *, year: int | None = None, team_id: int | None = None, league_id: int | None = None
    ) -> list[int]:
        """その絞り込みで試合がある月を昇順に。

        月の切り出しは SQL 側で行う。1シーズンで千件を超えるため、
        全部の試合日を持ってきて Python で数えると月を割る意味が薄れる。
        """
        rows = self._rows(year=year, team_id=team_id, league_id=league_id)
        # dates() は月ごとに1つの日付を SQL 側で重複なく返す（返るのは最大12件）
        return sorted({date.month for date in rows.dates("played_on", "month")})

    def latest_year(self) -> int | None:
        """最新シーズン。一覧の既定に使う。"""
        seasons = self.list_seasons()
        return seasons[0] if seasons else None


class DjangoTeamListQuery:
    """TeamListQuery の Django ORM 実装。チーム一覧に必要な値だけを取得する。"""

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
