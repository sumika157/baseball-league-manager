"""世界の範囲（`WorldScope`）で絞った QuerySet の出入口。

**世界の出典は `League.world` の1か所だけ**で、球団・選手・試合はリーグをたどって
世界が決まる。その「たどり方」をここに集める。リポジトリ・参照クエリ・管理画面・
データ投入コマンドは、テーブルを直接 `.objects` で引かず、ここを通す。
絞り込みはすべて SQL 側で行う（取得後に Python で捨てると、捨てる分の組み立てが無駄になる）。

選手は世界を持たない（世界ごとに選手の行が別）。在籍をたどって世界が決まり、
在籍のない選手は実データとして扱う（登録途中の選手を実データの管理画面から隠さないため）。

**試合は世界に加えて区分（`StageFilter`）も必須の引数で受け取る。** 公式戦の集計（成績・順位・
タイトル・FA の日数）はポストシーズンの試合を数えない。「区分を省いたら全部読む」形にすると、
入れ忘れた集計に例外を出さずにポストシーズンが混ざるので、範囲（`WorldScope`）と同じく既定値を持たない。
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import Enum

from django.db.models import Exists, OuterRef, Q, QuerySet

from ..domain.pennant.world import WorldScope
from ..domain.value_objects import GameStage
from . import orm_models


class StageFilter(Enum):
    """試合を読むときの区分の範囲。試合を読む入口（`games_in` など）の必須の引数。

    「None なら全部」の形にしない（`WorldScope` と同じ考え）。どの集計がどれを読むかは呼ぶ側
    （リポジトリ・参照クエリ）が決め、application の参照の API には出さない。
    """

    # 公式戦の集計（成績・順位・タイトル・試合一覧・FA の日数・オフの出場機会）
    REGULAR_ONLY = "公式戦だけ"
    # ポストシーズンの結果と成績（S3 以降で使う）
    POSTSEASON_ONLY = "ポストシーズンだけ"
    # 試合そのもの（試合詳細・保存）・世界の今日・直近の登板（疲労）・削除と道具
    ALL_STAGES = "全区分"


def stage_condition(path: str, stages: StageFilter) -> Q:
    """`Game` へ至る道（`path`）から見た、区分の条件。`Game` 自身なら空文字、明細の行なら "game"。

    範囲（世界）では絞らない集計（選手や球団の id で絞る集計）が使う。範囲も一緒に絞るなら
    `game_rows_condition` を使う。
    """
    prefix = f"{path}__" if path else ""
    if stages is StageFilter.REGULAR_ONLY:
        return Q(**{f"{prefix}stage": GameStage.REGULAR.value})
    if stages is StageFilter.POSTSEASON_ONLY:
        return Q(**{f"{prefix}stage": GameStage.POSTSEASON.value})
    return Q()


def game_rows_condition(path: str, scope: WorldScope, *, stages: StageFilter) -> Q:
    """試合の明細（打撃・投球・守備）から見た、範囲と区分の条件。`path` は明細から `Game` への関連名（"game"）。

    試合は、ホームのチームのリーグで世界が決まる。明細の行を範囲で読むときは
    `world_condition("game__home_team__league", ...)` を直接書かず、ここを通す（区分の入れ忘れを防ぐ。
    `tests/integration/test_postseason_isolation.py` が直接書いた箇所を検査する）。
    """
    return game_rows_in_world(path, scope) & stage_condition(path, stages)


def game_rows_in_world(path: str, scope: WorldScope) -> Q:
    """試合の明細から見た、**範囲だけ**の条件（区分を含まない）。

    成績の集計の共通の入口（`repositories._line_rows`）だけが使う。区分はそこで `stages`（必須）から足すので、
    ここで足すと区分を2か所で言うことになる。それ以外で明細を読むときは `game_rows_condition` を使う。
    """
    return world_condition(f"{path}__home_team__league", scope)


def world_condition(path: str, scope: WorldScope) -> Q:
    """`League` へ至る道（`path`）から見た、範囲の条件。

    `path` は対象のモデルから League までの関連名（Team なら "league"、
    Game なら "home_team__league"）。League 自身なら空文字。
    """
    prefix = f"{path}__" if path else ""
    if scope.is_real:
        return Q(**{f"{prefix}world__isnull": True})
    return Q(**{f"{prefix}world_id": scope.world_id})


def world_condition_in(path: str, world_ids: Sequence[int]) -> Q:
    """`world_condition` の、世界の id の集まり版（ペナントの世界だけ。実データは含まない）。

    世界の台帳のように、複数の世界をまとめて読む参照が使う。`path` の意味は `world_condition` と同じ。
    """
    prefix = f"{path}__" if path else ""
    return Q(**{f"{prefix}world_id__in": list(world_ids)})


def leagues_in_worlds(world_ids: Sequence[int]) -> QuerySet[orm_models.League]:
    return orm_models.League.objects.filter(world_condition_in("", world_ids))


def games_in_worlds(world_ids: Sequence[int], *, stages: StageFilter) -> QuerySet[orm_models.Game]:
    return orm_models.Game.objects.filter(
        world_condition_in("home_team__league", world_ids), stage_condition("", stages)
    )


def fixtures_in_worlds(world_ids: Sequence[int]) -> QuerySet[orm_models.PennantFixture]:
    return orm_models.PennantFixture.objects.filter(world_condition_in("home_team__league", world_ids))


def leagues_in(scope: WorldScope) -> QuerySet[orm_models.League]:
    return orm_models.League.objects.filter(world_condition("", scope))


def teams_in(scope: WorldScope) -> QuerySet[orm_models.Team]:
    return orm_models.Team.objects.filter(world_condition("league", scope))


def stints_in(scope: WorldScope) -> QuerySet[orm_models.PlayerStint]:
    return orm_models.PlayerStint.objects.filter(world_condition("team__league", scope))


def games_in(scope: WorldScope, *, stages: StageFilter) -> QuerySet[orm_models.Game]:
    """範囲と区分の試合。試合は、ホームのチームのリーグで世界が決まる（リーグをまたぐ世界の試合は無い）。"""
    return orm_models.Game.objects.filter(world_condition("home_team__league", scope), stage_condition("", stages))


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
