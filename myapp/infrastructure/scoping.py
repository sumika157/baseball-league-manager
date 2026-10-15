"""範囲（`WorldScope`。ワークスペースと世界）で絞った QuerySet の出入口。

**範囲の出典はリーグの1か所だけ**で、実データのリーグは `League.workspace`、ペナントのリーグは
`League.world` を持つ（どちらか片方だけ。`league_workspace_xor_world`）。球団・選手・試合は
リーグをたどって範囲が決まる。その「たどり方」をここに集める。リポジトリ・参照クエリ・管理画面・
データ投入コマンドは、テーブルを直接 `.objects` で引かず、ここを通す。
絞り込みはすべて SQL 側で行う（取得後に Python で捨てると、捨てる分の組み立てが無駄になる）。

選手は範囲の列を持たない（世界ごとに選手の行が別）。在籍をたどって範囲が決まるので、
在籍の無い選手はどの範囲にも出ない（管理画面だけが `admin_real_queryset` で拾う）。

**全範囲を読む関数を作らない。** 例外は管理画面専用の `admin_real_queryset` だけ（決定 2026-10-07 Q4）。
"""

from __future__ import annotations

from collections.abc import Sequence

from django.db.models import Exists, Model, OuterRef, Q, QuerySet

from ..domain.pennant.world import WorldScope
from . import orm_models


def world_condition(path: str, scope: WorldScope) -> Q:
    """`League` へ至る道（`path`）から見た、範囲の条件。

    `path` は対象のモデルから League までの関連名（Team なら "league"、
    Game なら "home_team__league"）。League 自身なら空文字。

    - 実データ: そのワークスペースのリーグ（`workspace_id = X`）。ペナントのリーグは workspace を
      持たないので、この条件だけで世界の行も外れる。
    - ペナント: その世界のリーグ（`world_id = W`）。世界がワークスペースに属することは、
      範囲を作る側（世界の台帳）が照合する。

    どちらも League の列なので、結合の深さは変わらない。試合の区分のようなほかの条件は、
    この `Q` と別の `Q` にして `&` で重ねる（範囲の条件に混ぜない）。
    """
    prefix = f"{path}__" if path else ""
    if scope.is_real:
        return Q(**{f"{prefix}workspace_id": scope.workspace_id})
    return Q(**{f"{prefix}world_id": scope.world_id})


def world_condition_in(path: str, world_ids: Sequence[int]) -> Q:
    """`world_condition` の、世界の id の集まり版（ペナントの世界だけ。実データは含まない）。

    世界の台帳のように、複数の世界をまとめて読む参照が使う。`path` の意味は `world_condition` と同じ。
    """
    prefix = f"{path}__" if path else ""
    return Q(**{f"{prefix}world_id__in": list(world_ids)})


def leagues_in_worlds(world_ids: Sequence[int]) -> QuerySet[orm_models.League]:
    return orm_models.League.objects.filter(world_condition_in("", world_ids))


def games_in_worlds(world_ids: Sequence[int]) -> QuerySet[orm_models.Game]:
    return orm_models.Game.objects.filter(world_condition_in("home_team__league", world_ids))


def fixtures_in_worlds(world_ids: Sequence[int]) -> QuerySet[orm_models.PennantFixture]:
    return orm_models.PennantFixture.objects.filter(world_condition_in("home_team__league", world_ids))


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
    """範囲に属する選手。範囲のリーグの球団に在籍の行がある選手（肯定形の `Exists`）。

    実データもペナントも同じ形。在籍の無い選手はどの範囲にも出ない（管理画面だけが
    `admin_real_queryset` で拾う）。複製した選手と元の選手は同じ名前なので、名前ではなく在籍で引く。
    """
    in_scope = orm_models.PlayerStint.objects.filter(player_id=OuterRef("pk")).filter(
        world_condition("team__league", scope)
    )
    return orm_models.Player.objects.filter(Exists(in_scope))


def club_plans_in(scope: WorldScope) -> QuerySet[orm_models.PennantClubPlan]:
    """範囲の球団の編成。日程・試合と同じく、球団のリーグで世界が決まる。"""
    return orm_models.PennantClubPlan.objects.filter(world_condition("team__league", scope))


def period_covering(year: int | None) -> Q:
    """在籍・主将の期間が `year` を含む条件（`from_year <= year かつ (to_year が空 または year <= to_year)`）。

    判定の出典は domain の `Stint.covers()` で、これはその SQL 側の写し（食い違わないことを
    `tests/integration/test_player_stats_query.py` の `PeriodCoveringMatchesTheDomainTest` が
    境界値で突き合わせる）。`year` が None なら「いま在籍中」（退団年が空）。
    """
    if year is None:
        return Q(to_year__isnull=True)
    return Q(from_year__lte=year) & (Q(to_year__isnull=True) | Q(to_year__gte=year))


def release_plans_in(scope: WorldScope) -> QuerySet[orm_models.PennantReleasePlan]:
    """範囲の球団の戦力外の指名。編成と同じく、球団のリーグで世界が決まる。"""
    return orm_models.PennantReleasePlan.objects.filter(world_condition("team__league", scope))


# 管理画面が全ワークスペースの実データを読むときの、モデルから League までの道
_ADMIN_REAL_ROUTES: dict[type[Model], str] = {
    orm_models.League: "",
    orm_models.Team: "league",
    orm_models.PlayerStint: "team__league",
    orm_models.Game: "home_team__league",
}


def admin_real_queryset[M: Model](model: type[M]) -> QuerySet[M] | None:
    """**`admin.py` 専用。** 全ワークスペースの実データの行（ペナントの世界の行は含まない）。

    「リポジトリと参照クエリは範囲を必須で受け取り、全範囲を読む API を作らない」規則の
    **唯一の例外**（決定 2026-10-07 Q4。管理画面は運営者が全ワークスペースを扱う）。
    `admin.py` 以外から import しない（`tests/integration/test_wiring.py` が検査する）。

    - リーグ・チーム・在籍・試合: リーグがワークスペースに属するもの（＝実データ）。
    - 選手: ペナントの球団に在籍したことが無い選手（否定形）。**在籍の無い選手も含む**
      （管理画面で登録途中の選手を隠さない。範囲で読む `players_in` には出ない）。
    - それ以外のモデル（球場など、世界の行を持たないもの）: None。
    """
    if model is orm_models.Player:
        in_pennant = orm_models.PlayerStint.objects.filter(player_id=OuterRef("pk"), team__league__world__isnull=False)
        return model._default_manager.filter(~Exists(in_pennant))
    path = _ADMIN_REAL_ROUTES.get(model)
    if path is None:
        return None
    prefix = f"{path}__" if path else ""
    return model._default_manager.filter(**{f"{prefix}workspace__isnull": False})
