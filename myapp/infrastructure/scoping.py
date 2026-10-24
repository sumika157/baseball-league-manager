"""範囲（`WorldScope`。ワークスペースと世界）で絞った QuerySet の出入口。

**範囲の出典はリーグの1か所だけ**で、実データのリーグは `League.workspace`、ペナントのリーグは
`League.world` を持つ（どちらか片方だけ。`league_workspace_xor_world`）。球団・選手・試合は
リーグをたどって範囲が決まる。その「たどり方」をここに集める。リポジトリ・参照クエリ・管理画面・
データ投入コマンドは、テーブルを直接 `.objects` で引かず、ここを通す。
絞り込みはすべて SQL 側で行う（取得後に Python で捨てると、捨てる分の組み立てが無駄になる）。

選手は範囲の列を持たない（世界ごとに選手の行が別）。在籍をたどって範囲が決まるので、
在籍の無い選手はどの範囲にも出ない（管理画面だけが `admin_real_queryset` で拾う）。

球場はリーグを持たず、ワークスペースに直属する（世界ごとの行は無く、実データとそのワークスペースの世界が共有する）。
球場の範囲の条件は `_stadium_condition` の1か所で、`stadiums_in`・`home_stadium_in` がこれを使う。

**全範囲を読む関数を作らない。** 例外は次の2つだけ。

- 管理画面専用の `admin_real_queryset`（決定 2026-10-07 Q4）。`admin.py` だけが使う。
- `real_teams_of_outer_workspace`（W2 T1）。範囲を取らないが、`OuterRef("pk")` で外側の `Workspace` の行に
  相関した副問い合わせとしてしか働かない（外側のワークスペースの実データの球団に絞られる）ので、全範囲を読まない。
  ただし `Workspace` 以外を外側にして使うと、その行の pk をワークスペースの id と取り違えて**エラーにならずに**
  別のワークスペースの球団を数える。`infrastructure/queries.py`（ワークスペースの一覧）だけが使う
  （`test_wiring.py` の `OuterWorkspaceTeamsTest` が縛る）。
"""

from __future__ import annotations

from collections.abc import Sequence

from django.db.models import Exists, FilteredRelation, Model, OuterRef, Q, QuerySet

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


def worlds_in(workspace_id: int) -> QuerySet[orm_models.PennantWorld]:
    """そのワークスペースの世界（台帳の行）。世界の台帳（`DjangoWorldRepository`・`DjangoWorldSummaryQuery`）が使う。

    ほかのワークスペースの世界の id は、ここで絞った台帳では見つからない。`world_condition` のペナントの条件は
    ワークスペースを見ないので、ペナントの範囲（`pennant(ws, w)`）はこの台帳で世界を引けたときだけ作る。
    """
    return orm_models.PennantWorld.objects.filter(workspace_id=workspace_id)


def world_condition_in(path: str, world_ids: Sequence[int]) -> Q:
    """`world_condition` の、世界の id の集まり版（ペナントの世界だけ。実データは含まない）。

    世界の台帳のように、複数の世界をまとめて読む参照が使う。`path` の意味は `world_condition` と同じ。
    ここではワークスペースを見ないので、**渡す id は、ワークスペースで絞った台帳（`worlds_in`）が
    返したものだけにする。**
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


def real_teams_of_outer_workspace() -> QuerySet[orm_models.Team]:
    """相関副問い合わせ用: 外側の問い合わせのワークスペース（`OuterRef("pk")`）の実データの球団。

    `teams_in(WorldScope.real(ws))` の、ワークスペースの id が外側の行で決まる版。ワークスペースを
    まとめて読む台帳（自分が担当として入っているワークスペースの一覧）だけが `Exists` の中で使う。
    ペナントのリーグは workspace を持たないので、世界の球団は入らない。
    条件は `world_condition` の実データの分岐と対になる（実データの条件の表し方を変えるときは、ここも直す）。
    """
    return orm_models.Team.objects.filter(league__workspace_id=OuterRef("pk"))


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


def declarations_in(scope: WorldScope) -> QuerySet[orm_models.PlayerFreeAgentDeclaration]:
    """範囲の選手の FA 宣言。宣言は選手の行に付くので、`players_in` と同じく範囲の在籍がある選手で絞る。

    呼ぶ側が渡す選手の id だけで絞ると、ほかのワークスペース・世界の選手の id を渡されたときに宣言が漏れる。
    条件は宣言の行ごとの相関した `Exists`（同じ1本の中）。`player_id__in=players_in(scope)` の形にすると、
    相関しない副問い合わせが全ワークスペースの選手を走査する（チームの読み込みのたびに）。
    """
    in_scope = orm_models.PlayerStint.objects.filter(player_id=OuterRef("player_id")).filter(
        world_condition("team__league", scope)
    )
    return orm_models.PlayerFreeAgentDeclaration.objects.filter(Exists(in_scope))


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


def _stadium_condition(path: str, scope: WorldScope) -> Q:
    """球場（`path` は球場へ至る関連名。球場自身なら空文字）の、範囲の条件。

    球場はワークスペースに直属するので、**実データでもペナントでも `workspace_id = scope.workspace_id`**
    （ペナントの範囲もワークスペースの id を持つ理由の1つ。世界はそのワークスペースの球場を共有する）。
    """
    prefix = f"{path}__" if path else ""
    return Q(**{f"{prefix}workspace_id": scope.workspace_id})


def stadiums_in(scope: WorldScope) -> QuerySet[orm_models.Stadium]:
    """範囲の球場（範囲のワークスペースの球場）。本拠地の検査と、管理画面の選択肢の絞り込みが使う。

    ペナントの範囲では、さらに**範囲の世界がその球場のワークスペースに属すこと**も条件にする。ペナントの範囲の
    ほかの条件（`world_condition`）はワークスペースを見ないので、取り違えた範囲（`pennant(A, B の世界)`）で
    B の世界の球団に A の球場を書けないようにする（同じ1クエリの中の結合で見る）。
    """
    stadiums = orm_models.Stadium.objects.filter(_stadium_condition("", scope))
    if scope.world_id is not None:  # ペナントの範囲
        stadiums = stadiums.filter(workspace__pennant_worlds__id=scope.world_id)
    return stadiums


def home_stadium_in(scope: WorldScope) -> FilteredRelation:
    """球団の本拠地を、範囲の球場に限って結合する関連。

    `teams_in(scope).annotate(stadium=home_stadium_in(scope)).values("stadium__name")` の形で、参照クエリが
    本拠地の名前・所在地・収容人数を球団と一緒に読むときに使う（`select_related("home_stadium")` の代わり。
    結合の数は同じ）。範囲の外の球場を指す球団は、本拠地なし（列が NULL）として読む。
    本拠地を書く経路（`DjangoTeamRepository.save`）が範囲の外の球場を拒むので、ふだんの結果は素の結合と同じ。
    これは書き込みの検査を通らない行（ORM の直書き）に対する、読み取り側の防御。
    """
    return FilteredRelation("home_stadium", condition=_stadium_condition("home_stadium", scope))


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


def awards_in(scope: WorldScope) -> QuerySet[orm_models.PennantAward]:
    """範囲の表彰。表彰はリーグを持つので、リーグで世界が決まる。"""
    return orm_models.PennantAward.objects.filter(world_condition("league", scope))


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
    - それ以外のモデル（球場など、世界の行を持たないもの）: None（管理画面は全ワークスペースの球場を扱う。
      選択肢をワークスペースで絞るときは `stadiums_in` を使う）。
    """
    if model is orm_models.Player:
        in_pennant = orm_models.PlayerStint.objects.filter(player_id=OuterRef("pk"), team__league__world__isnull=False)
        return model._default_manager.filter(~Exists(in_pennant))
    path = _ADMIN_REAL_ROUTES.get(model)
    if path is None:
        return None
    prefix = f"{path}__" if path else ""
    return model._default_manager.filter(**{f"{prefix}workspace__isnull": False})
