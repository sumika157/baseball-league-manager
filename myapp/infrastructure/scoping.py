"""世界の範囲（`WorldScope`）で絞った QuerySet の出入口。

**世界の出典は `League.world` の1か所だけ**で、球団・選手・試合はリーグをたどって
世界が決まる。その「たどり方」をここに集める。リポジトリ・参照クエリ・管理画面・
データ投入コマンドは、テーブルを直接 `.objects` で引かず、ここを通す。
絞り込みはすべて SQL 側で行う（取得後に Python で捨てると、捨てる分の組み立てが無駄になる）。

選手は世界を持たない（世界ごとに選手の行が別）。在籍をたどって世界が決まり、
在籍のない選手は実データとして扱う（登録途中の選手を実データの管理画面から隠さないため）。
"""

from __future__ import annotations

from django.db.models import Exists, OuterRef, Q, QuerySet

from ..domain.pennant.world import WorldScope
from . import orm_models


def world_condition(path: str, scope: WorldScope) -> Q:
    """`League` へ至る道（`path`）から見た、範囲の条件。

    `path` は対象のモデルから League までの関連名（Team なら "league"、
    Game なら "home_team__league"）。League 自身なら空文字。
    """
    prefix = f"{path}__" if path else ""
    if scope.is_real:
        return Q(**{f"{prefix}world__isnull": True})
    return Q(**{f"{prefix}world_id": scope.world_id})


def leagues_in(scope: WorldScope) -> QuerySet[orm_models.League]:
    return orm_models.League.objects.filter(world_condition("", scope))


def teams_in(scope: WorldScope) -> QuerySet[orm_models.Team]:
    return orm_models.Team.objects.filter(world_condition("league", scope))


def stints_in(scope: WorldScope) -> QuerySet[orm_models.PlayerStint]:
    return orm_models.PlayerStint.objects.filter(world_condition("team__league", scope))


def games_in(scope: WorldScope) -> QuerySet[orm_models.Game]:
    """試合は、ホームのチームのリーグで世界が決まる（リーグをまたぐ世界の試合は無い）。"""
    return orm_models.Game.objects.filter(world_condition("home_team__league", scope))


def fixtures_in(scope: WorldScope) -> QuerySet[orm_models.PennantFixture]:
    """範囲の未消化の対戦。試合と同じく、ホームの球団のリーグで世界が決まる。"""
    return orm_models.PennantFixture.objects.filter(world_condition("home_team__league", scope))


def ratings_in(scope: WorldScope) -> QuerySet[orm_models.PennantPlayerRatings]:
    """範囲の選手の能力。能力は選手の行に付くので、範囲の選手で絞る。"""
    return orm_models.PennantPlayerRatings.objects.filter(player_id__in=players_in(scope).values("id"))


def players_in(scope: WorldScope) -> QuerySet[orm_models.Player]:
    """範囲に属する選手。ペナントの選手は、その世界の球団に在籍の行がある選手。

    実データの側は「ペナントの球団に在籍した選手**ではない**」で絞る。複製した選手と
    元の選手は同じ名前なので、名前だけで引くと2人ずつ出てしまう。
    """
    if scope.is_pennant:
        in_world = orm_models.PlayerStint.objects.filter(player_id=OuterRef("pk")).filter(
            world_condition("team__league", scope)
        )
        return orm_models.Player.objects.filter(Exists(in_world))
    in_pennant = orm_models.PlayerStint.objects.filter(player_id=OuterRef("pk"), team__league__world__isnull=False)
    return orm_models.Player.objects.filter(~Exists(in_pennant))
