"""ビュー。

責務は「HTTP を解釈してアプリケーションサービスを呼び、結果を描画する」ことだけ。
成績の計算も背番号の重複判定もここには無い（ドメイン層にある）。
"""

import secrets
from collections.abc import Callable
from datetime import date, timedelta
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404, HttpResponseNotAllowed
from django.middleware.csrf import get_token
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy
from django.views.decorators.http import require_GET, require_safe
from django.views.generic import CreateView

from ..application.club_management import ClubManagementService
from ..application.dto import (
    AwardRow,
    ClubPlanView,
    GameEditData,
    GameEditPlateAppearance,
    Listing,
    PlayerRatingsCard,
    PlayerRatingsHistory,
    RatingsTable,
    ReleasePlanView,
    TeamMonthlyRow,
    WorldContext,
)
from ..application.game_recording import GameRecordingService
from ..application.game_view import GameViewService
from ..application.pennant_club_details import ClubDetailsService
from ..application.pennant_history import PennantHistoryService
from ..application.pennant_home import PennantHomeService
from ..application.pennant_offseason import PennantOffseasonService
from ..application.pennant_offseason_view import OffseasonViewService
from ..application.pennant_ratings import PennantRatingsViewService
from ..application.pennant_season import PennantSeasonService
from ..application.pennant_view import PennantWorldViewService
from ..application.pennant_world import PennantWorldService, WorldRepositories
from ..application.queries import SimulationContextQuery
from ..application.roster import RosterService
from ..application.services import TeamApplicationService
from ..application.team_analysis import STATS_TAB, TeamAnalysisService
from ..domain.exceptions import (
    AlreadyAdvanced,
    AlreadyClosed,
    DomainError,
    GameNotFound,
    InvalidReleasePlan,
    InvalidWorld,
    LeagueNotFound,
    PlayerNotFound,
    TeamNotFound,
    WorkspaceNotFound,
    WorldNotFound,
)
from ..domain.pennant.club_plan import PlanSection
from ..domain.pennant.roster import OPENING_REGISTERED_BAND
from ..domain.pennant.schedule import AdvanceTarget
from ..domain.pennant.season import MAX_GAMES_PER_ADVANCE, SCREEN_ADVANCE_TARGETS, SeasonPhase, world_today
from ..domain.pennant.world import WorldScope
from ..domain.value_objects import (
    AdvanceReason,
    Base,
    ContractStatus,
    ErrorKind,
    FieldingPosition,
    FreeAgencyKind,
    PlateAppearanceResult,
    Position,
)
from ..infrastructure.queries import (
    DjangoAwardQuery,
    DjangoFieldingTotalsQuery,
    DjangoGameListQuery,
    DjangoHomeStadiumQuery,
    DjangoOffseasonQuery,
    DjangoPennantActivityQuery,
    DjangoPlayerFieldingQuery,
    DjangoPlayerSearchQuery,
    DjangoPlayerStatsQuery,
    DjangoRatingsHistoryQuery,
    DjangoSeasonPlayingTimeQuery,
    DjangoSimulationContextQuery,
    DjangoTeamAnalysisQuery,
    DjangoTeamListQuery,
    DjangoTeamPermissionQuery,
    DjangoWorkspaceAccessQuery,
    DjangoWorkspaceListQuery,
    DjangoWorldSummaryQuery,
)
from ..infrastructure.repositories import (
    DjangoAwardRepository,
    DjangoClubPlanRepository,
    DjangoFixtureRepository,
    DjangoGameRepository,
    DjangoLeagueRepository,
    DjangoRatingsRepository,
    DjangoReleasePlanRepository,
    DjangoTeamRepository,
    DjangoWorldRepository,
)
from ..infrastructure.workspaces import legacy_workspace_id
from .club_input import (
    ACTION_AUTO,
    ACTION_MANUAL,
    ACTIONS,
    DEFAULT_TAB,
    RELEASE_SECTION,
    RELEASE_TAB,
    SECTIONS,
    TAB_OF_SECTION,
    ClubFormState,
    ClubInputError,
    LineupSlotInput,
    NoticeLink,
    ReleaseFormState,
    RotationSlotInput,
    failed_state,
    parse_active,
    parse_closer,
    parse_expected_year,
    parse_lineup,
    parse_releases,
    parse_rotation,
    player_option,
    read_releases,
    tab_of,
)
from .forms import (
    FIELDED_BY_SEPARATOR,
    MAX_INNINGS,
    GameForm,
    PennantWorldForm,
    PlayerRegistrationForm,
    PlayerUpdateForm,
)

BATTER_MODE = "batter"
PITCHER_MODE = "pitcher"
# 球団の画面の「成績｜能力」の切り替え（?view=ratings）。能力はペナントの世界だけ
RATINGS_VIEW = "ratings"
# 成績の期間の切り替え（?period=career）。ペナントの世界は既定が「今季」で、通算に切り替えられる
CAREER_PERIOD = "career"


def _stats_year(request, world: WorldContext | None) -> int | None:
    """成績に数える年。None は通算。

    実データは常に通算（今季という区切りを持たない）。ペナントの世界は既定が今季（世界のいまの年度）で、
    `?period=career` のときだけ通算にする。
    """
    if world is None or request.GET.get("period") == CAREER_PERIOD:
        return None
    return world.season_year


def _requires_login(request):
    """閲覧は誰でも、書き込みはログイン必須。

    一覧のように読み書きが同じ URL に同居する画面で使う。画面ごと
    login_required にすると閲覧までログインが要る。
    """
    if request.user.is_authenticated:
        return None
    return redirect_to_login(request.get_full_path())


def _requires_team_permission(request, *team_ids):
    """担当チームでなければ拒否する。

    _requires_login と役割は同じだが、ログインしているだけでは通さない。
    管理ユーザーは常に通り、それ以外は渡したチームのうち少なくとも1つの
    担当者であることを求める（試合は2チームにまたがるため）。
    """
    if not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path())
    if not build_permission_query(_real_scope(request)).can_manage_any(request.user, team_ids):
        raise PermissionDenied("このチームを編集する権限がありません。")
    return None


# 要求（request）に覚える、今の URL のワークスペースの id の属性名
_WORKSPACE_ID_ATTR = "_legacy_workspace_id"


def _workspace_id(request) -> int:
    """今の URL が読み書きするワークスペース（W1 の暫定。W2 で URL の key の解決に替える。決定 2026-10-07 Q2）。

    一時関数 `legacy_workspace_id()`（id が最小のワークスペース）は1クエリ読むので、**1つの要求で1回だけ**呼び、
    要求に覚える（範囲を作る `_real_scope`・`_world_scope` も、権限の判定も、API もここを通る）。
    プロセスに覚えないのは、テストごとにワークスペースが作り直されると古い値を返すため。
    ワークスペースが1つも無ければ 404。
    """
    cached = getattr(request, _WORKSPACE_ID_ATTR, None)
    if cached is not None:
        return cached
    try:
        workspace_id = legacy_workspace_id()
    except WorkspaceNotFound:
        raise Http404("ワークスペースがありません。") from None
    setattr(request, _WORKSPACE_ID_ATTR, workspace_id)
    return workspace_id


def _real_scope(request) -> WorldScope:
    """今の URL のワークスペースの実データの範囲。

    画面・API・管理画面のテンプレートタグが範囲を作るのはここと `_world_scope` だけ（組み立て口には範囲を渡す）。
    """
    return WorldScope.real(_workspace_id(request))


def _world_scope(request, world_id: int) -> WorldScope:
    """今の URL のワークスペースの、ペナントの世界ひとつの範囲。**そのワークスペースの台帳に無い世界は 404。**

    ペナントの範囲の条件（`world_id = W`）はワークスペースを見ないので、ほかのワークスペースの世界の id で
    範囲を作らせないよう、ワークスペースで絞った台帳で世界を引けたときだけ範囲を作る。
    台帳は `_world_context` が既に読んでいる（世界の画面は必ず先に見出しを作る）ので、その結果を要求に覚えて使い、
    クエリを増やさない。見出しを読んでいない要求だけ、ここで台帳を1回読む。
    範囲を作るのは依存の組み立て（`views.py` の `build_*` と同じ組み立て口の側）の一部なので、
    ここで `DjangoWorldRepository` を直接作る（ビューの本体はリポジトリを触らない）。
    """
    workspace_id = _workspace_id(request)
    if world_id not in _verified_world_ids(request):
        try:
            DjangoWorldRepository(workspace_id).find_by_id(world_id)
        except WorldNotFound:
            raise Http404("世界が見つかりません。") from None
        _verified_world_ids(request).add(world_id)
    return WorldScope.pennant(workspace_id, world_id)


# 要求（request）に覚える、今の URL のワークスペースの台帳で引けた世界の id の属性名
_VERIFIED_WORLDS_ATTR = "_verified_world_ids"


def _verified_world_ids(request) -> set[int]:
    """この要求の中で、今の URL のワークスペースの台帳で引けた世界の id（`_world_context`・`_world_scope` が足す）。"""
    verified = getattr(request, _VERIFIED_WORLDS_ATTR, None)
    if verified is None:
        verified = set()
        setattr(request, _VERIFIED_WORLDS_ATTR, verified)
    return verified


def _pennant_world_id(scope: WorldScope) -> int:
    """ペナント専用の組み立て口が、渡された範囲から世界の id を取り出す。実データの範囲なら InvalidWorld。

    範囲の取り違え（ペナントの画面に実データの範囲を渡す）を、組み立てるところで止める。
    """
    if scope.world_id is None:
        raise InvalidWorld("ペナントの世界の範囲が必要です。実データの範囲は渡せません。")
    return scope.world_id


def _require_real(scope: WorldScope) -> None:
    """実データ専用の組み立て口が、渡された範囲を確かめる。ペナントの範囲なら InvalidWorld。"""
    if scope.is_pennant:
        raise InvalidWorld("実データの範囲が必要です。ペナントの世界の範囲は渡せません。")


def _today_for(scope: WorldScope) -> Callable[[], date]:
    """範囲の「今日」を返す関数。実データなら暦の今日、ペナントの世界ならその世界の「今日」。"""
    if scope.world_id is None:
        return date.today
    return _world_clock(scope, DjangoSimulationContextQuery(scope))


def build_service(scope: WorldScope) -> TeamApplicationService:
    """依存を組み立てる。差し替えたい場合はここだけ変えればよい。

    組み立ては**この1か所だけ**にする（管理画面のテンプレートタグもテストも
    ここを呼ぶ）。呼ぶ側ごとに一部の依存だけを渡すと、使う画面によって
    落ちるサービスができてしまうため。

    **範囲は必ず渡す**（既定値は無い。既定値で切り替える形にすると、渡し忘れが
    「ペナントの画面に実データが出る」形で現れる）。依存は全部、渡された範囲で作る
    （範囲の外の球団・試合には触れない）。実データの範囲なら年齢の基準日は暦の今日。
    ペナントの世界の範囲なら、その世界の「今日」（最後に試合をした日。まだ無ければ開幕日）。
    存在しない世界は、年齢を数えるときに WorldNotFound。
    """
    today = _today_for(scope)
    return TeamApplicationService(
        teams=DjangoTeamRepository(scope),
        team_list_query=DjangoTeamListQuery(scope),
        games=DjangoGameRepository(scope),
        leagues=DjangoLeagueRepository(scope),
        game_list_query=DjangoGameListQuery(scope),
        player_fielding_query=DjangoPlayerFieldingQuery(scope),
        player_stats_query=DjangoPlayerStatsQuery(scope),
        today=today,
    )


def _world_clock(scope: WorldScope, context_query: SimulationContextQuery) -> Callable[[], date]:
    """その世界の「今日」を返す関数。呼ばれるたびに読み直す（進めた後に古い日付を返さない）。

    世界は範囲のワークスペースで絞った台帳から引く（ほかのワークスペースの世界なら、使うときに WorldNotFound）。
    """
    world_id = _pennant_world_id(scope)
    worlds = DjangoWorldRepository(scope.workspace_id)

    def today() -> date:
        return world_today(worlds.find_by_id(world_id).start_year, context_query.last_played_on())

    return today


def build_pennant_world_view(workspace_id: int) -> PennantWorldViewService:
    """世界の見出し（世界バー・世界の一覧）を作るサービスを組み立てる。

    見出しの材料は、**渡されたワークスペースで絞った**世界の台帳を読む参照クエリが一定のクエリ数でまとめて読む
    （ほかのワークスペースの世界は一覧に出ず、id で引いても WorldNotFound）。自軍の順位だけは、世界の範囲のサービス
    （`build_service(世界の範囲)`）に任せる。世界の範囲は渡されたワークスペースで作る（世界の id は台帳が返したもの
    だけなので、ほかのワークスペースの世界の範囲はできない。世界ごとにワークスペースを読み直さない）。
    作成フォームを出すかの上限の判定は、作成の検査と同じ台帳の `count_by_owner`（全ワークスペースで数える）で行う。
    """
    return PennantWorldViewService(
        summaries=DjangoWorldSummaryQuery(workspace_id),
        worlds=DjangoWorldRepository(workspace_id),
        standings_for=lambda world_id: build_service(WorldScope.pennant(workspace_id, world_id)),
        games_for=lambda world_id: DjangoGameListQuery(WorldScope.pennant(workspace_id, world_id)),
    )


def build_pennant_ratings_service(scope: WorldScope) -> PennantRatingsViewService:
    """ペナントの世界ひとつの、能力の表示（選手ページのカード・球団の能力の表）を作るサービスを組み立てる。

    球団と能力は**渡された世界の範囲で**読む。年齢の基準日は `build_service` と同じ世界の「今日」。
    ペナントの世界の範囲だけを受け取る（実データの範囲なら InvalidWorld）。
    """
    world_id = _pennant_world_id(scope)
    return PennantRatingsViewService(
        world_id=world_id,
        worlds=DjangoWorldRepository(scope.workspace_id),
        fixtures=DjangoFixtureRepository(scope),
        context_query=DjangoSimulationContextQuery(scope),
        stats=DjangoPlayerStatsQuery(scope),
        ratings=DjangoRatingsRepository(scope),
        history=DjangoRatingsHistoryQuery(scope),
        today=_world_clock(scope, DjangoSimulationContextQuery(scope)),
    )


def build_recording_service(scope: WorldScope) -> GameRecordingService:
    """試合の登録とスコアブックの保存を担うサービスを組み立てる。

    `build_service(scope)` と同じく**組み立てはここだけ**にする。試合の登録も打席の記録も
    チームの一覧も試合の一覧も要らないので、依存は4つ（試合・チーム・リーグ・本拠地の球場）で足りる。
    実データの範囲だけを受け取る（ペナントの世界の範囲なら InvalidWorld）。
    """
    _require_real(scope)
    return GameRecordingService(
        games=DjangoGameRepository(scope),
        teams=DjangoTeamRepository(scope),
        leagues=DjangoLeagueRepository(scope),
        home_stadiums=DjangoHomeStadiumQuery(scope),
    )


def build_game_view_service(scope: WorldScope) -> GameViewService:
    """試合の一覧と詳細を読むサービスを組み立てる。

    `build_service(scope)` と同じく**組み立てはここだけ**にする。依存は全部渡し、呼ぶ側ごとに
    一部だけ渡さない。実データの範囲もペナントの世界の範囲も受け取り、依存は全部、渡された範囲で作る
    （範囲の外の試合・球団には触れない）。
    """
    return GameViewService(
        games=DjangoGameRepository(scope),
        teams=DjangoTeamRepository(scope),
        game_list_query=DjangoGameListQuery(scope),
    )


def build_team_analysis_service(scope: WorldScope) -> TeamAnalysisService:
    """戦力分析のサービスを組み立てる。

    `build_service(scope)` と同じく**組み立てはここだけ**にする。依存は全部渡し、呼ぶ側ごとに
    一部だけ渡さず、必ずここを通す。依存は全部、渡された範囲で作る
    （範囲の外の球団を指されても、球団が見つからない扱いになる）。FA 取得の「試合の記録のある
    最初の年」もこの範囲の試合から取るので、実データの古い年に引きずられない。
    試合の無い球団の既定の年は、実データなら暦の今日、ペナントの世界ならその世界の「今日」の年。
    """
    today = _today_for(scope)
    return TeamAnalysisService(
        team_list_query=DjangoTeamListQuery(scope),
        analysis_query=DjangoTeamAnalysisQuery(scope),
        today=today,
    )


def _analysis_scope(
    request, world_id: int | None
) -> tuple[TeamAnalysisService, TeamApplicationService, WorldContext | None]:
    """戦力分析の画面が読む範囲。世界が無ければ実データ、あればその世界（無ければ 404）。"""
    if world_id is None:
        scope = _real_scope(request)
        return build_team_analysis_service(scope), build_service(scope), None
    world = _world_context(request, world_id)
    scope = _world_scope(request, world_id)
    return build_team_analysis_service(scope), build_service(scope), world


def build_roster_service(scope: WorldScope) -> RosterService:
    """契約区分（昇格）と FA 宣言のサービスを組み立てる。

    `build_service(scope)` と同じく**組み立てはここだけ**にする。呼ぶ側ごとに
    一部の依存だけを渡さず、必ずここを通す。実データの範囲だけを受け取る
    （管理画面の操作で、ペナントの世界の球団には触れない。ペナントの範囲なら InvalidWorld）。
    """
    _require_real(scope)
    return RosterService(
        teams=DjangoTeamRepository(scope),
        leagues=DjangoLeagueRepository(scope),
    )


def build_permission_query(scope: WorldScope) -> DjangoTeamPermissionQuery:
    """チームの編集権限の判定を組み立てる。実データの範囲だけを受け取る（ペナントの範囲なら InvalidWorld）。"""
    _require_real(scope)
    return DjangoTeamPermissionQuery(scope)


def build_player_search_query(scope: WorldScope) -> DjangoPlayerSearchQuery:
    """選手検索を組み立てる。実データの範囲だけを受け取る（ペナントの範囲なら InvalidWorld）。"""
    _require_real(scope)
    return DjangoPlayerSearchQuery(scope)


def build_workspace_access_query() -> DjangoWorkspaceAccessQuery:
    """URL の key と利用者からワークスペースと権限を引く台帳を組み立てる（W2 T4 の `workspace_view` が使う）。

    範囲を取らない: key から引くワークスペースそのものの台帳で、ワークスペースの中のデータは読まない。
    """
    return DjangoWorkspaceAccessQuery()


def build_workspace_list_query() -> DjangoWorkspaceListQuery:
    """自分が入っているワークスペースの一覧（`/`）を組み立てる。範囲を取らない台帳（利用者で絞る）。"""
    return DjangoWorkspaceListQuery()


def _repositories_for(scope: WorldScope) -> WorldRepositories:
    """世界の範囲でリポジトリを組み立てる。世界の作成が、写し先の世界に書くのに使う。"""
    return WorldRepositories(
        leagues=DjangoLeagueRepository(scope),
        teams=DjangoTeamRepository(scope),
        ratings=DjangoRatingsRepository(scope),
    )


def build_pennant_world_service(scope: WorldScope) -> PennantWorldService:
    """世界の作成・削除のサービスを組み立てる。

    分岐元は**渡された実データの範囲**で読み、写し先は世界の範囲のリポジトリ（生成器で受け取る）に書く。
    実データの範囲だけを受け取る（ペナントの範囲なら InvalidWorld）。
    作る世界は、渡された範囲のワークスペースに属す（`workspace_id` で渡す）。日程を作る世界の範囲も
    同じワークスペースで作るので、`World.scope` と食い違わない。
    """
    _require_real(scope)
    return PennantWorldService(
        real_leagues=DjangoLeagueRepository(scope),
        real_teams=DjangoTeamRepository(scope),
        real_fielding=DjangoFieldingTotalsQuery(scope),
        worlds=DjangoWorldRepository(scope.workspace_id),
        repositories_for=_repositories_for,
        atomic=transaction.atomic,
        ensure_schedule=lambda world_id: build_pennant_season_service(
            WorldScope.pennant(scope.workspace_id, world_id)
        ).ensure_schedule(),
        workspace_id=scope.workspace_id,
    )


def build_pennant_season_service(scope: WorldScope) -> PennantSeasonService:
    """ペナントの世界ひとつのシーズン進行サービスを組み立てる。

    **リポジトリと参照クエリは、すべて渡された世界の範囲で作る**（範囲の外の球団・試合・日程には触れない）。
    保存と消化済みの対戦の削除を1つにまとめるトランザクションは、ここで渡す（application は Django を知らない）。
    ペナントの世界の範囲だけを受け取る（実データの範囲なら InvalidWorld）。存在しない世界は、使うときに WorldNotFound。
    """
    world_id = _pennant_world_id(scope)
    return PennantSeasonService(
        world_id=world_id,
        worlds=DjangoWorldRepository(scope.workspace_id),
        leagues=DjangoLeagueRepository(scope),
        teams=DjangoTeamRepository(scope),
        games=DjangoGameRepository(scope),
        fixtures=DjangoFixtureRepository(scope),
        ratings=DjangoRatingsRepository(scope),
        plans=DjangoClubPlanRepository(scope),
        context_query=DjangoSimulationContextQuery(scope),
        atomic=transaction.atomic,
    )


def build_pennant_offseason_service(scope: WorldScope) -> PennantOffseasonService:
    """ペナントの世界ひとつのオフ（シーズンを締める）サービスを組み立てる。

    **リポジトリと参照クエリは、すべて渡された世界の範囲で作る。** 引退・新人・翌年の能力・日程・編成の
    後始末を1つのトランザクションにまとめるので、`transaction.atomic` もここで渡す。
    ペナントの世界の範囲だけを受け取る（実データの範囲なら InvalidWorld）。存在しない世界は、使うときに WorldNotFound。
    """
    world_id = _pennant_world_id(scope)
    return PennantOffseasonService(
        world_id=world_id,
        worlds=DjangoWorldRepository(scope.workspace_id),
        leagues=DjangoLeagueRepository(scope),
        teams=DjangoTeamRepository(scope),
        fixtures=DjangoFixtureRepository(scope),
        ratings=DjangoRatingsRepository(scope),
        plans=DjangoClubPlanRepository(scope),
        releases=DjangoReleasePlanRepository(scope),
        awards=DjangoAwardRepository(scope),
        titles=build_service(scope),
        context_query=DjangoSimulationContextQuery(scope),
        playing_time=DjangoSeasonPlayingTimeQuery(scope),
        atomic=transaction.atomic,
    )


def build_offseason_view_service(scope: WorldScope) -> OffseasonViewService:
    """オフの結果（Y年オフ）を読むサービスを組み立てる。読むだけ。参照クエリは渡された世界の範囲で作る。

    ペナントの世界の範囲だけを受け取る（実データの範囲なら InvalidWorld）。
    """
    _pennant_world_id(scope)
    return OffseasonViewService(
        worlds=DjangoWorldRepository(scope.workspace_id), offseason=DjangoOffseasonQuery(scope)
    )


def build_pennant_home_service(scope: WorldScope) -> PennantHomeService:
    """GM ホームの材料を作るサービスを組み立てる。

    順位・主力・タイトルは世界の範囲の `TeamApplicationService`（`build_service(世界の範囲)`）に任せ、
    試合の一覧・日程・期間の見どころの参照は、すべて渡された世界の範囲で作る。
    ペナントの世界の範囲だけを受け取る（実データの範囲なら InvalidWorld）。
    """
    _pennant_world_id(scope)
    return PennantHomeService(
        teams=build_service(scope),
        games=DjangoGameListQuery(scope),
        game_records=DjangoGameRepository(scope),
        fixtures=DjangoFixtureRepository(scope),
        activity=DjangoPennantActivityQuery(scope),
        team_records=DjangoTeamRepository(scope),
        leagues=DjangoLeagueRepository(scope),
    )


def build_pennant_history_service(scope: WorldScope) -> PennantHistoryService:
    """歴代の記録と球団史の材料を作るサービスを組み立てる。読むだけ。

    順位・タイトルは世界の範囲の `TeamApplicationService`（`build_service(世界の範囲)`）に任せ、
    試合の一覧・球団・リーグの参照は、すべて渡された世界の範囲で作る。
    ペナントの世界の範囲だけを受け取る（実データの範囲なら InvalidWorld）。
    """
    _pennant_world_id(scope)
    return PennantHistoryService(
        teams=build_service(scope),
        games=DjangoGameListQuery(scope),
        team_records=DjangoTeamRepository(scope),
        leagues=DjangoLeagueRepository(scope),
        awards=DjangoAwardQuery(scope),
    )


def _world_context(request, world_id: int) -> WorldContext:
    """世界の見出し。存在しない・id が正しくない・今の URL のワークスペースに無い世界は 404。

    見出しはワークスペースで絞った台帳から読むので、引けた世界の id を要求に覚え、
    続く `_world_scope` が台帳を読み直さずに範囲を作れるようにする。
    """
    try:
        context = build_pennant_world_view(_workspace_id(request)).get_context(world_id)
    except (WorldNotFound, InvalidWorld):
        raise Http404("世界が見つかりません。") from None
    _verified_world_ids(request).add(context.world_id)
    return context


def _is_owner(request, world: WorldContext) -> bool:
    """ログインしている人が、その世界のオーナーか。書き込みの導線を出す・操作を通すかの判定に使う。"""
    return request.user.is_authenticated and world.owner_id is not None and world.owner_id == request.user.id


def _requires_world_owner(request, world: WorldContext):
    """世界のオーナーでなければ拒否する。未ログインはログインへ、ほかの人は 403。

    実データの担当者の判定（`_requires_team_permission`）とは別で、管理ユーザーでも世界のオーナーでなければ通さない
    （世界は遊ぶ人のセーブデータで、管理ユーザーの権限は世界に及ばない）。
    """
    if not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path())
    if not _is_owner(request, world):
        raise PermissionDenied("この世界のオーナーではありません。")
    return None


def _scope(request, world_id: int | None) -> tuple[TeamApplicationService, WorldContext | None]:
    """画面が読む範囲。URL に世界の id が無ければ実データ、あればその世界（無ければ 404）。

    実データの画面とペナントの画面は同じビューとテンプレートを使う。違いは、読むサービスの範囲と、
    テンプレートに渡す世界（`world`。リンクの引き方・世界バー・文言の差し替えの合図）だけ。
    """
    if world_id is None:
        return build_service(_real_scope(request)), None
    world = _world_context(request, world_id)
    return build_service(_world_scope(request, world_id)), world


def _game_scope(request, world_id: int | None) -> tuple[TeamApplicationService, GameViewService, WorldContext | None]:
    """試合の画面が読む範囲。`_scope` と同じく、世界が無ければ実データ、あればその世界（無ければ 404）。"""
    if world_id is None:
        scope = _real_scope(request)
        return build_service(scope), build_game_view_service(scope), None
    world = _world_context(request, world_id)
    scope = _world_scope(request, world_id)
    return build_service(scope), build_game_view_service(scope), world


def _own_team_id(world: WorldContext | None) -> int | None:
    """順位表で「自軍」の印を付ける球団。実データには自軍が無い。"""
    return world.managed_team_id if world is not None else None


def _render(request, template: str, context: dict, world: WorldContext | None):
    """世界の範囲の画面なら `world` を添えて描画する。実データでは `world` は None。"""
    return render(request, template, {**context, "world": world})


def _parse_date(value: str | None) -> date | None:
    """URL の日付。読めなければ None（エラーにしない）。"""
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _next_season_year(request) -> int:
    """世界の開幕年の既定。実データの最新シーズンの翌年（試合が無ければ今年）。"""
    latest = build_game_view_service(_real_scope(request)).latest_game_year()
    return latest + 1 if latest is not None else date.today().year


def pennant_index(request):
    """ペナントの世界の一覧と作成。一覧は誰でも、作成（POST）はログインが必要。

    世界の一覧は「あなたの世界」と「ほかの人の世界」に分け、オーナーの名前は出さない。
    作成フォームはログインしている人にだけ出す（上限に達していれば理由を出す）。
    """
    if request.method not in ("GET", "HEAD", "POST"):
        return HttpResponseNotAllowed(["GET", "HEAD", "POST"])
    if request.method == "POST":
        denied = _requires_login(request)
        if denied is not None:
            return denied

    views = build_pennant_world_view(_workspace_id(request))
    form = None
    if request.user.is_authenticated:
        form_args = {
            "groups": build_service(_real_scope(request)).list_teams_by_league().rows,
            "default_year": _next_season_year(request),
        }
        form = (
            PennantWorldForm(request.POST, **form_args) if request.method == "POST" else PennantWorldForm(**form_args)
        )
        if request.method == "POST" and form.is_valid():
            created = _create_world(request, form)
            if created is not None:
                return redirect("pennant_world", world_id=created)

    return render(
        request,
        "pennant/world_list.html",
        {"worlds": views.list_worlds(request.user.id if request.user.is_authenticated else None), "form": form},
    )


def _create_world(request, form: PennantWorldForm) -> int | None:
    """検証済みのフォームから世界を作る。作れたらその id、作れなければ理由を messages に出して None。

    世界の作成と日程の生成は1つのトランザクション（`create_world_with_schedule`）で、失敗したら何も残らない。
    """
    data = form.cleaned_data
    seed = data["seed"]
    try:
        created = build_pennant_world_service(_real_scope(request)).create_world_with_schedule(
            name=data["name"],
            owner_id=request.user.id,
            source_league_ids=[int(league) for league in data["leagues"]],
            start_year=data["start_year"],
            seed=seed if seed is not None else secrets.randbelow(2**31),
            managed_source_team_id=int(data["managed_team"]),
        )
    except DomainError as error:
        messages.error(request, str(error))
        return None
    messages.success(
        request, f"世界「{created.world.name}」を作りました（{created.team_count}球団・{created.player_count}選手）。"
    )
    return _saved_world_id(created.world.id)


def _saved_world_id(world_id: int | None) -> int:
    assert world_id is not None, "保存した世界には id がある"
    return world_id


def pennant_world(request, world_id):
    """GM ホーム。自軍の位置・結果のまとめ・進める・次の試合・順位表・主力・タイトル争い。

    読むだけの画面なので誰でも開ける。「進める」と削除の導線はオーナーにだけ出す。
    `?since=<日付>` は進める前の今日（結果のまとめの起点）。読めない・未来の値はまとめを出さない。
    """
    world = _world_context(request, world_id)
    is_owner = _is_owner(request, world)
    league = request.GET.get("league")
    home = build_pennant_home_service(_world_scope(request, world_id)).get_home(
        world,
        since=_parse_date(request.GET.get("since")),
        league_id=int(league) if league and league.isdigit() else None,
        include_advance=is_owner,
    )
    # 「シーズンを締める」はシーズン終了のオーナーにだけ出す。最終シーズンを終えた世界は締められないので、
    # 締めるカードの代わりに「世界の終了」（ホームの `finale`）を出す（終了の情報を1か所にする）
    close_option = (
        build_pennant_offseason_service(_world_scope(request, world_id)).close_option()
        if is_owner and world.phase is SeasonPhase.FINISHED and not world.is_over
        else None
    )
    return _render(
        request,
        "pennant/home.html",
        {
            "home": home,
            "is_owner": is_owner,
            "plan_notices": _plan_notices(request, world) if is_owner else [],
            "close_option": close_option,
            "registered_band": OPENING_REGISTERED_BAND,
        },
        world,
    )


def _plan_notices(request, world: WorldContext) -> list[NoticeLink]:
    """いま進めたら自動に落ちる区画と理由（ホームの「注意」）。受け持つ球団の編成を読むだけで、保存はしない。

    編成を読めない世界（受け持つ球団が無い・球団が見つからない）では注意を出さない。ホームを開けなくしないため。
    """
    if world.managed_team_id is None:
        return []
    try:
        view = build_club_service(_world_scope(request, world.world_id)).view(world.managed_team_id)
    except DomainError:
        return []
    return [
        NoticeLink(notice.section.value, notice.reason, TAB_OF_SECTION[notice.section], notice.falls_back)
        for notice in view.notices
    ]


def pennant_advance(request, world_id):
    """世界を進める。POST だけで、オーナーだけが使える（GET はホームへ戻す）。

    進めたあとは `?since=<進める前の今日>` つきでホームへ戻る（結果は URL に残るので、再読み込みしても消えない）。
    `expected_today` がずれていたら進めない（二重送信や、別の画面で先に進めたときに、さらに進めてしまわない）。
    """
    world = _world_context(request, world_id)
    home_url = reverse("pennant_world", args=[world_id])
    if request.method != "POST":
        return redirect(home_url)
    denied = _requires_world_owner(request, world)
    if denied is not None:
        return denied

    try:
        target = AdvanceTarget(request.POST.get("target", ""))
    except ValueError:
        target = None
    if target not in SCREEN_ADVANCE_TARGETS:
        messages.error(request, "その進め方はできません。")
        return redirect(home_url)

    # 画面を開いたときの「今日」（まだ試合が無ければ空）。読めない値は、どの今日とも一致しない日付にする
    sent = request.POST.get("expected_today", "")
    expected_today = None if sent == "" else (_parse_date(sent) or date.min)
    try:
        report = build_pennant_season_service(_world_scope(request, world_id)).advance(
            target, max_games=MAX_GAMES_PER_ADVANCE, expected_today=expected_today
        )
    except AlreadyAdvanced as error:
        messages.warning(request, str(error))
        return redirect(home_url)
    except DomainError as error:
        messages.error(request, str(error))
        return redirect(home_url)
    if not report.games:
        messages.info(request, "進める試合がありません。")
        return redirect(home_url)

    # まだ1試合も無かった世界は、最初の試合日の前日を起点にする
    since = world.today if world.today is not None else report.played_dates[0] - timedelta(days=1)
    messages.success(request, f"{report.games}試合を進めました。")
    return redirect(f"{home_url}?since={since.isoformat()}")


def pennant_close_season(request, world_id):
    """シーズンを締める。POST だけで、オーナーだけが使える（GET はホームへ戻す）。

    締めたら「<年>年のシーズンを締めました」を出してオフの結果へ。元に戻せないので、
    `expected_year`（画面を開いたときの年）がずれていたら何もしない（二重送信や、別の画面で先に締めたときに、
    さらに次の年を締めてしまわない）。`expected_year` の欠落は「検査なし」にせず弾く。
    """
    world = _world_context(request, world_id)
    home_url = reverse("pennant_world", args=[world_id])
    if request.method != "POST":
        return redirect(home_url)
    denied = _requires_world_owner(request, world)
    if denied is not None:
        return denied

    sent = request.POST.get("expected_year", "")
    if not sent.isdigit():
        messages.error(request, "画面が古いか、送信が正しくありません。開き直してからもう一度お試しください。")
        return redirect(home_url)
    expected_year = int(sent)

    try:
        result = build_pennant_offseason_service(_world_scope(request, world_id)).close_season(
            expected_year=expected_year
        )
    except AlreadyClosed as error:
        messages.warning(request, str(error))
        # 締めていると確かめられた年（error.year）のオフの結果へ。確かめられないとき（年度のずれなど）はホームへ
        if error.year is not None:
            return redirect("pennant_offseason", world_id=world_id, year=error.year)
        return redirect(home_url)
    except DomainError as error:
        messages.error(request, str(error))
        return redirect(home_url)

    messages.success(
        request,
        f"{result.year}年のシーズンを締めました"
        f"（引退 {result.retired_count}人・戦力外 {result.released_count}人・新人 {result.draftee_count}人）。",
    )
    if result.reverted_sections:
        labels = "・".join(section.value for section in result.reverted_sections)
        messages.warning(request, f"引退・戦力外の選手を含んでいたため、自軍の編成（{labels}）を自動に戻しました。")
    if result.without_ratings_count:
        messages.info(request, f"能力の無い選手 {result.without_ratings_count}人は、翌年の能力を作りませんでした。")
    return redirect("pennant_offseason", world_id=world_id, year=result.year)


def pennant_offseason(request, world_id, year):
    """Y年オフの結果。読むだけの画面なので誰でも開ける。締めていない年・範囲外の年は 404。

    `?league=<id>` は引退・退団と新人を見るリーグ（無効な値は自軍のリーグに落とす）。
    """
    world = _world_context(request, world_id)
    league = request.GET.get("league", "")
    summary = build_offseason_view_service(_world_scope(request, world_id)).get_summary(
        world, year, league_id=int(league) if league.isdigit() else None
    )
    if summary is None:
        raise Http404("その年のオフの結果は、まだありません。")
    return _render(request, "pennant/offseason.html", {"summary": summary}, world)


@require_safe
def pennant_history(request, world_id):
    """歴代の記録。各リーグの年ごとの優勝と、選んだリーグ（`?league=`）のタイトル受賞者。読むだけで誰でも開ける。

    載せるのは終わったシーズンだけ。無効なリーグの指定は既定のリーグに落とす。
    """
    world = _world_context(request, world_id)
    league = request.GET.get("league", "")
    history = build_pennant_history_service(_world_scope(request, world_id)).get_world_history(
        world, league_id=int(league) if league.isdecimal() else None
    )
    return _render(request, "pennant/history.html", {"history": history}, world)


@require_safe
def pennant_team_history(request, world_id, team_id):
    """球団史。どの球団でも開ける。年ごとの順位・勝敗・優勝と、その球団の選手が取ったタイトル。"""
    world = _world_context(request, world_id)
    try:
        history = build_pennant_history_service(_world_scope(request, world_id)).get_club_history(world, team_id)
    except TeamNotFound:
        raise Http404("球団が見つかりません。") from None
    return _render(request, "pennant/team_history.html", {"history": history}, world)


def pennant_delete(request, world_id):
    """世界の削除の確認。GET も POST もオーナーだけ。削除したら世界の一覧へ戻る。"""
    world = _world_context(request, world_id)
    if request.method not in ("GET", "HEAD", "POST"):
        return HttpResponseNotAllowed(["GET", "HEAD", "POST"])
    denied = _requires_world_owner(request, world)
    if denied is not None:
        return denied

    if request.method == "POST":
        build_pennant_world_service(_real_scope(request)).delete_world(world_id)
        messages.success(request, f"世界「{world.name}」を削除しました。")
        return redirect("pennant_index")
    deletion = build_pennant_home_service(_world_scope(request, world_id)).get_deletion(world)
    return _render(request, "pennant/world_delete.html", {"deletion": deletion}, world)


def build_club_service(scope: WorldScope) -> ClubManagementService:
    """ペナントの世界ひとつの、球団の編成を管理するサービスを組み立てる。

    `build_pennant_season_service()` と同じく、**リポジトリと参照クエリはすべて渡された世界の範囲で作る**
    （他の世界の球団の編成は、読めも書けもしない）。ペナントの世界の範囲だけを受け取る
    （実データの範囲なら InvalidWorld）。
    """
    world_id = _pennant_world_id(scope)
    return ClubManagementService(
        world_id=world_id,
        worlds=DjangoWorldRepository(scope.workspace_id),
        plans=DjangoClubPlanRepository(scope),
        ratings=DjangoRatingsRepository(scope),
        fixtures=DjangoFixtureRepository(scope),
        context_query=DjangoSimulationContextQuery(scope),
    )


def build_club_details_service(scope: WorldScope) -> ClubDetailsService:
    """編成画面の選手の詳細（年齢・能力の要約・今季の成績）を作るサービスを組み立てる。

    参照クエリとリポジトリは**渡された世界の範囲で**作る。年齢の基準日は世界の「今日」。
    ペナントの世界の範囲だけを受け取る（実データの範囲なら InvalidWorld）。
    """
    _pennant_world_id(scope)
    return ClubDetailsService(
        stats=DjangoPlayerStatsQuery(scope),
        ratings=DjangoRatingsRepository(scope),
        today=_world_clock(scope, DjangoSimulationContextQuery(scope)),
    )


def pennant_club(request, world_id):
    """編成。1軍登録・オーダー・投手陣の3タブ（`?tab=`）と、シーズン終了で締められるときだけ「戦力外」タブ。

    受け持つ球団の編成を、区画ごとに自動か手動のどちらかで見せる。「戦力外」は締めるときの戦力外の指名（自動か手動）。

    GET は誰でも（読み取り専用）、POST はオーナーだけ（未ログインはログインへ、ほかの人は 403）。
    POST は区画ごとに「手動にする」「保存する」「自動に戻す」を受け、成功したら同じタブへ戻る
    （再読み込みで二重に送らない）。
    失敗したら保存せず、入力を残してメッセージつきで再表示する。
    """
    world = _world_context(request, world_id)
    if request.method not in ("GET", "HEAD", "POST"):
        return HttpResponseNotAllowed(["GET", "HEAD", "POST"])
    if request.method == "POST":
        denied = _requires_world_owner(request, world)
        if denied is not None:
            return denied
        return _post_club(request, world)
    return _render_club(request, world, tab_of(request.GET.get("tab")))


def _post_club(request, world: WorldContext):
    club_url = reverse("pennant_club", args=[world.world_id])
    team_id = world.managed_team_id
    if team_id is None:
        messages.error(request, "この世界には、受け持つ球団が決まっていません。")
        return redirect(club_url)
    if request.POST.get("section") == RELEASE_SECTION:
        return _post_release(request, world)
    section = SECTIONS.get(request.POST.get("section", ""))
    action = request.POST.get("action", "")
    if section is None or action not in ACTIONS:
        messages.error(request, "その操作はできません。")
        return redirect(club_url)

    service = build_club_service(_world_scope(request, world.world_id))
    try:
        current = service.view(team_id)
    except DomainError as error:
        messages.error(request, str(error))
        return redirect(club_url)
    tab_url = f"{club_url}?tab={TAB_OF_SECTION[section]}"
    try:
        if action == ACTION_AUTO:
            service.reset(team_id, section)
            messages.success(request, f"{section.value}を自動編成に戻しました。")
        elif action == ACTION_MANUAL:
            if _is_manual(current, section):
                messages.info(request, f"{section.value}は、すでに手動です。")
            else:
                _start_manual(service, current, section)
                messages.success(request, f"{section.value}を手動にしました。")
        else:
            _save_section(service, request.POST, current, section)
            messages.success(request, f"{section.value}を保存しました。")
    except (ClubInputError, DomainError) as error:
        state = failed_state(
            request.POST,
            section,
            str(error),
            lineup_size=current.limits.lineup_size,
            rotation_size=current.limits.rotation_size,
        )
        return _render_club(request, world, TAB_OF_SECTION[section], state=state, status=400)
    return redirect(tab_url)


def _post_release(request, world: WorldContext):
    """「戦力外」タブの操作（手動にする・保存する・自動に戻す）。どれも画面を開いたときの締める年を送ってもらう。

    指名として成立しない・古い画面なら、チェックを残して再表示する。締められなくなっていたら（別の画面で締めた・
    年が変わった）、理由を出して編成の画面へ戻す（タブはもう出ない）。
    """
    club_url = reverse("pennant_club", args=[world.world_id])
    action = request.POST.get("action", "")
    if action not in ACTIONS:
        messages.error(request, "その操作はできません。")
        return redirect(club_url)
    service = build_pennant_offseason_service(_world_scope(request, world.world_id))
    try:
        expected_year = parse_expected_year(request.POST)
        if action == ACTION_AUTO:
            service.reset_releases(expected_year=expected_year)
            messages.success(request, "戦力外を自動に戻しました。")
        elif action == ACTION_MANUAL:
            service.start_manual_releases(expected_year=expected_year)
            messages.success(request, "戦力外を手動にしました。外す選手を選んで保存してください。")
        else:
            chosen = parse_releases(request.POST, service.release_view())
            service.set_releases(chosen, expected_year=expected_year)
            messages.success(request, f"戦力外の指名を保存しました（{len(chosen)}人）。")
    except (ClubInputError, InvalidReleasePlan) as error:
        state = ReleaseFormState(error=str(error), checked=read_releases(request.POST))
        return _render_club(request, world, RELEASE_TAB, release_state=state, status=400)
    except DomainError as error:
        messages.error(request, str(error))
        return redirect(club_url)
    return redirect(f"{club_url}?tab={RELEASE_TAB}")


def _release_view(request, world: WorldContext) -> ReleasePlanView | None:
    """「戦力外」タブの材料。締められないとき（シーズン中・締めた後・最終シーズン）・受け持ちが無いときは None。

    None ならタブを出さない。
    """
    try:
        return build_pennant_offseason_service(_world_scope(request, world.world_id)).release_view()
    except DomainError:
        return None


def _is_manual(view: ClubPlanView, section: PlanSection) -> bool:
    return {
        PlanSection.ACTIVE: view.active_is_manual,
        PlanSection.LINEUP: view.lineup_is_manual,
        PlanSection.ROTATION: view.rotation_is_manual,
        PlanSection.CLOSER: view.closer_is_manual,
    }[section]


def _start_manual(service: ClubManagementService, current: ClubPlanView, section: PlanSection) -> None:
    """区画を、いま映している自動編成の形を初期値にして手動にする。"""
    team_id = current.team_id
    if section is PlanSection.ACTIVE:
        service.start_manual_active(team_id)
    elif section is PlanSection.LINEUP:
        service.start_manual_lineup(team_id)
    elif section is PlanSection.ROTATION:
        service.set_rotation(team_id, current.rotation_ids)
    elif current.closer_id is not None:
        service.set_closer(team_id, current.closer_id)
    else:
        raise ClubInputError("抑えにできる投手がいません。")


def _save_section(service: ClubManagementService, post, current: ClubPlanView, section: PlanSection) -> None:
    team_id = current.team_id
    if section is PlanSection.ACTIVE:
        service.set_active_roster(team_id, parse_active(post, current))
    elif section is PlanSection.LINEUP:
        service.set_lineup(team_id, parse_lineup(post, current.limits.lineup_size))
    elif section is PlanSection.ROTATION:
        service.set_rotation(team_id, parse_rotation(post, current.limits.rotation_size))
    else:
        service.set_closer(team_id, parse_closer(post))


def _render_club(
    request,
    world: WorldContext,
    tab: str,
    *,
    state: ClubFormState | None = None,
    release_state: ReleaseFormState | None = None,
    status: int = 200,
):
    """編成の画面を描く。`state` / `release_state` があるときは、保存に失敗した入力を残して出す。

    手動の区画の編集欄の初期値は、**保存済みの上書き**から作る（使えなくなって自動に落ちた区画も、
    GM が決めた形のまま出す。解決後の値から作ると、保存したときに上書きが AI の形で置き換わる）。
    「戦力外」タブはシーズン終了で締められるときだけ出す。出せないときに `?tab=release` が来たら既定のタブに落とす。
    """
    # ほかのタブではタブを出すかだけを決める（締められるか。支配下の一覧までは読まない）
    release = None
    has_release_tab = False
    if world.managed_team_id is not None:
        if tab == RELEASE_TAB:
            release = _release_view(request, world)
            has_release_tab = release is not None
        else:
            has_release_tab = (
                build_pennant_offseason_service(_world_scope(request, world.world_id)).close_option().can_close
            )
    if tab == RELEASE_TAB and release is None:
        tab = DEFAULT_TAB
    context: dict = {
        "is_owner": _is_owner(request, world),
        "tab": tab,
        "club": None,
        "club_error": None,
        "release": release,
        "has_release_tab": has_release_tab,
    }
    if world.managed_team_id is None:
        return render(request, "pennant/club.html", {**context, "world": world}, status=status)
    service = build_club_service(_world_scope(request, world.world_id))
    try:
        view = service.view(world.managed_team_id)
    except DomainError as error:
        # 受け持つ球団が見つからないなど。画面を落とさず理由を出す（ホームの注意と同じ扱い）
        context["club_error"] = str(error)
        return render(request, "pennant/club.html", {**context, "world": world}, status=status)
    limits = view.limits

    # 保存済みの上書き（あれば）→ 無ければ、いま進めたときに使われる形
    saved_lineup = view.saved_lineup if view.saved_lineup is not None else view.lineup
    lineup = state.lineup if state is not None else None
    if lineup is None:
        lineup = [LineupSlotInput(row.batting_order, row.player_id, row.position) for row in saved_lineup]
        lineup += [LineupSlotInput(order, None, None) for order in range(len(lineup) + 1, limits.lineup_size + 1)]
    rotation = state.rotation if state is not None else None
    if rotation is None:
        ids: list[int | None] = list(
            view.saved_rotation_ids if view.saved_rotation_ids is not None else view.rotation_ids
        )
        ids += [None] * (limits.rotation_size - len(ids))
        rotation = [RotationSlotInput(order, player_id) for order, player_id in enumerate(ids, start=1)]
    checked = state.active_checked if state is not None else None
    if checked is None:
        checked = frozenset(view.saved_active_ids if view.saved_active_ids is not None else view.active_ids)
    closer = state.closer_id if state is not None else None
    if closer is None:
        closer = view.saved_closer_id if view.saved_closer_id is not None else view.closer_id

    # 選択肢は1軍の選手。保存済みの上書きに入っている1軍外の選手も、印を付けて残す（保存時にドメインが弾く）
    kept = {slot.player_id for slot in lineup} | {slot.player_id for slot in rotation} | {closer}
    # 「戦力外」タブは編成の選手の詳細を使わない（下で支配下の詳細を作る）
    details = (
        ()
        if tab == RELEASE_TAB
        else build_club_details_service(_world_scope(request, world.world_id)).details(
            world.managed_team_id, view.year, view.players
        )
    )
    batters = [
        d for d in details if not d.player.position.is_pitcher and (d.player.is_active or d.player.player_id in kept)
    ]
    pitchers = [
        d for d in details if d.player.position.is_pitcher and (d.player.is_active or d.player.player_id in kept)
    ]

    context.update(
        club=view,
        club_players=details,
        error=state.error if state is not None else None,
        tab_notices=[
            NoticeLink(notice.section.value, notice.reason, TAB_OF_SECTION[notice.section], notice.falls_back)
            for notice in view.notices
            if TAB_OF_SECTION[notice.section] == tab
        ],
        active_checked=checked,
        batter_candidates=[player_option(row) for row in batters],
        pitcher_candidates=[player_option(row) for row in pitchers],
        lineup_slots=lineup,
        rotation_slots=rotation,
        closer_selected=closer,
        usage=service.pitching_usage(view) if tab == "pitching" else None,
    )
    if release_state is not None:
        # 戦力外の保存に失敗した理由（締められなくなってタブを出せないときも、既定のタブの上に出す）
        context["error"] = release_state.error
    if tab == RELEASE_TAB and release is not None:
        context.update(
            release_players=build_club_details_service(_world_scope(request, world.world_id)).details(
                release.team_id, release.year, release.players
            ),
            release_checked=release_state.checked if release_state is not None else release.chosen_ids,
        )
    return render(request, "pennant/club.html", {**context, "world": world}, status=status)


def dashboard(request):
    """ホーム画面。全リーグの最新シーズンの順位表・部門の首位と、最新の試合日の結果。"""
    return render(request, "myapp/dashboard.html", {"board": build_service(_real_scope(request)).get_dashboard()})


def _sort_params(request):
    """URL の sort / dir を読む。dir は 'desc' のときだけ降順。

    未指定なら None を返し、既定の並び順をドメイン側に決めさせる。
    """
    sort = request.GET.get("sort") or None
    direction = request.GET.get("dir")
    descending = None if direction not in ("asc", "desc") else (direction == "desc")
    return sort, descending


def team_list(request, world_id=None):
    """チーム一覧。"""
    service, world = _scope(request, world_id)
    sort, descending = _sort_params(request)
    listing = service.list_teams_by_league(sort=sort, descending=descending)
    return _render(
        request,
        "myapp/team_list.html",
        {
            "leagues": listing.rows,
            # 件数の表示や既存の判定に使うため、平坦にしたものも渡す
            "teams": [team for group in listing.rows for team in group.teams],
            "current_sort": listing.sort,
            "current_descending": listing.descending,
        },
        world,
    )


def standings(request, year=None, world_id=None):
    """年別の順位表。年を指定しない場合は最新シーズン。"""
    service, world = _scope(request, world_id)
    sort, descending = _sort_params(request)
    try:
        board = service.get_standings(year, sort=sort, descending=descending)
    except DomainError as error:
        raise Http404(str(error)) from error

    return _render(
        request,
        "myapp/standings.html",
        {
            "standings": board,
            "current_sort": board.sort,
            "current_descending": board.descending,
            "highlight_team_id": _own_team_id(world),
        },
        world,
    )


def player_list(request, team_id, world_id=None):
    """選手一覧。野手／投手モードを切り替えて表示する。"""
    service, world = _scope(request, world_id)
    if world is not None and request.method not in ("GET", "HEAD"):
        # ペナントの世界の選手は、画面から登録・編集しない
        return HttpResponseNotAllowed(["GET", "HEAD"])

    try:
        team_name = service.get_team_name(team_id)
    except TeamNotFound:
        raise Http404("チームが見つかりません。") from None

    form = PlayerRegistrationForm()

    if request.method == "POST":
        # 一覧の閲覧は誰でもできるが、登録はこのチームの担当者だけができる
        denied = _requires_team_permission(request, team_id)
        if denied is not None:
            return denied

        form = PlayerRegistrationForm(request.POST)
        if form.is_valid():
            try:
                build_roster_service(_real_scope(request)).register_player(
                    team_id=team_id,
                    name=form.cleaned_data["name"],
                    number=form.cleaned_data["number"],
                    position_label=form.cleaned_data["position"],
                    contract_label=form.cleaned_data["contract"],
                )
            except DomainError as error:
                messages.error(request, str(error))
            else:
                messages.success(request, f"{form.cleaned_data['name']} 選手を登録しました。")
                mode = PITCHER_MODE if form.cleaned_data["position"] == Position.PITCHER.value else BATTER_MODE
                return redirect(f"{reverse('player_list', args=[team_id])}?pos={mode}")
        else:
            messages.error(request, _first_error(form))

    pos_mode = PITCHER_MODE if request.GET.get("pos") == PITCHER_MODE else BATTER_MODE
    sort, descending = _sort_params(request)
    ratings_table: RatingsTable | None = None
    listing: Listing | None = None
    # ペナントの世界は今季の成績が既定（?period=career で通算）。実データは通算のまま
    stats_year = _stats_year(request, world)
    if world is not None and request.GET.get("view") == RATINGS_VIEW:
        # 能力の表。並べ替えのキーは成績の表と別（不正なキーは domain が背番号順に落とす）
        ratings_table = build_pennant_ratings_service(_world_scope(request, world.world_id)).get_table(
            team_id, pitchers=pos_mode == PITCHER_MODE, sort=sort, descending=descending, stats_year=stats_year
        )
        rows, current_sort, current_descending = ratings_table.rows, ratings_table.sort, ratings_table.descending
        months: list[TeamMonthlyRow] = []
    else:
        listing = (
            service.list_pitchers(team_id, sort=sort, descending=descending, year=stats_year)
            if pos_mode == PITCHER_MODE
            else service.list_batters(team_id, sort=sort, descending=descending, year=stats_year)
        )
        rows, current_sort, current_descending = listing.rows, listing.sort, listing.descending
        # 通算値では見えない調子の波を、月ごとに区切って出す
        months = service.list_team_monthly_splits(team_id, stats_year)

    return _render(
        request,
        "myapp/player_list.html",
        {
            "team_id": team_id,
            "reserve_ids": _reserve_player_ids(request, world, team_id),
            "team_name": team_name,
            "totals": service.get_team_totals(team_id, stats_year),
            "stats_year": stats_year,
            "listing": listing,
            "ratings_table": ratings_table,
            "players": rows,
            "pos_mode": pos_mode,
            "form": form,
            "positions": Position.labels(),
            "contracts": ContractStatus.labels(),
            "current_sort": current_sort,
            "current_descending": current_descending,
            "months": months,
            # このチームの担当者（または管理ユーザー）だけが登録・編集の導線を見える
            "can_edit_team": world is None
            and build_permission_query(_real_scope(request)).can_manage(request.user, team_id),
        },
        world,
    )


def _reserve_player_ids(request, world: WorldContext | None, team_id: int) -> frozenset[int]:
    """ペナントの世界の球団の、いま在籍していて1軍に使われない選手（「2軍」の印を付ける）。実データは空で印を出さない。

    出典は編成（`ClubManagementService`）の1軍で、画面ごとに別の判定を持たない。球団が読めないときも印は出さない。
    """
    if world is None:
        return frozenset()
    try:
        return build_club_service(_world_scope(request, world.world_id)).reserve_player_ids(team_id)
    except DomainError:
        return frozenset()


def player_search(request):
    """選手を名前で探す。チームが増えると所属からはたどり着きにくいため。"""
    keyword = (request.GET.get("q") or "").strip()
    results = build_player_search_query(_real_scope(request)).search(keyword) if keyword else []

    return render(
        request,
        "myapp/player_search.html",
        {
            "keyword": keyword,
            "results": results,
            "searched": bool(keyword),
        },
    )


def league_detail(request, league_id, year=None, world_id=None):
    """リーグ画面。所属チーム・順位表・直近の試合。"""
    service, world = _scope(request, world_id)
    try:
        detail = service.get_league_detail(league_id, year)
    except LeagueNotFound:
        raise Http404("リーグが見つかりません。") from None

    return _render(
        request,
        "myapp/league_detail.html",
        {"league": detail, "highlight_team_id": _own_team_id(world)},
        world,
    )


def league_titles(request, league_id, year=None, world_id=None):
    """リーグのタイトル一覧。部門別の上位者をシーズンで区切って並べる。"""
    service, world = _scope(request, world_id)
    try:
        titles = service.get_league_titles(league_id, year)
    except LeagueNotFound:
        raise Http404("リーグが見つかりません。") from None
    except DomainError as error:
        raise Http404(str(error)) from error

    return _render(request, "myapp/league_titles.html", {"titles": titles}, world)


def league_stats(request, league_id, world_id=None):
    """リーグの成績一覧。所属する全選手の通算成績を並べ替えて見る。"""
    pos_mode = PITCHER_MODE if request.GET.get("pos") == PITCHER_MODE else BATTER_MODE
    # 規定の絞り込み。指定が無い・読めない値なら全員（並べ替えのキーと同じ扱い）
    qualified = request.GET.get("qualified") == "1"
    sort, descending = _sort_params(request)

    service, world = _scope(request, world_id)
    stats_year = _stats_year(request, world)
    try:
        stats = service.get_league_stats(
            league_id,
            pitchers=pos_mode == PITCHER_MODE,
            qualified=qualified,
            sort=sort,
            descending=descending,
            year=stats_year,
        )
    except LeagueNotFound:
        raise Http404("リーグが見つかりません。") from None

    return _render(
        request,
        "myapp/league_stats.html",
        {
            "stats": stats,
            "stats_year": stats_year,
            "players": stats.listing.rows,
            "pos_mode": pos_mode,
            "current_sort": stats.listing.sort,
            "current_descending": stats.listing.descending,
        },
        world,
    )


def game_list(request, world_id=None):
    """試合一覧。シーズン・月・リーグ・チームで絞り込める。

    全件を一度に描くと件数ぶん重くなるため、指定が無ければ最新シーズンの
    最後に試合があった月を見せる。
    """
    service, games, world = _game_scope(request, world_id)

    def _int(name):
        value = request.GET.get(name)
        return int(value) if value and value.isdigit() else None

    year, team_id, month, league_id = _int("year"), _int("team"), _int("month"), _int("league")

    # チームの選択肢は、選んでいるリーグに所属するチームだけにする。
    # 他リーグのチームを選べても、結果が必ず空になるだけのため
    all_teams = service.list_teams().rows
    teams = [t for t in all_teams if league_id is None or t.league_id == league_id]
    if team_id is not None and team_id not in {t.id for t in teams}:
        # リーグを切り替えると、選んでいたチームがそのリーグにいないことがある
        team_id = None

    if year is None:
        year = games.latest_game_year()

    months = games.list_game_months(year=year, team_id=team_id, league_id=league_id)
    # 月を選んでいないとき（＝一覧を開いた直後）と、年・リーグ・チームを変えて
    # 選んでいた月に試合が無くなったときは、その範囲の最新の月に落とす。
    # 全件を一度に描くと件数ぶん重くなるため、月は必ず1つに決める
    if months and month not in months:
        month = months[-1]

    listing = games.list_games(year=year, team_id=team_id, month=month, league_id=league_id)
    leagues = service.list_leagues()

    return _render(
        request,
        "myapp/game_list.html",
        {
            "games": listing.rows,
            "years": games.list_game_seasons(),
            "months": months,
            "leagues": leagues,
            "teams": teams,
            "selected_year": year,
            "selected_month": month,
            "selected_team": team_id,
            "selected_league": league_id,
            # 件数が何の件数かを添えるため、選んでいるものの名前も渡す
            "selected_league_name": next((lg.name for lg in leagues if lg.id == league_id), ""),
            "selected_team_name": next((t.name for t in teams if t.id == team_id), ""),
            # 担当チームが1つも無ければ、押しても弾かれるだけの登録導線は見せない。
            # 判定はリーグの絞り込みに関係なく、全チームで行う
            "can_create_game": world is None
            and build_permission_query(_real_scope(request)).can_manage_any(request.user, [t.id for t in all_teams]),
        },
        world,
    )


@login_required
def game_create(request):
    """試合を作る。作成後、成績の入力画面へ進む。"""
    service = build_service(_real_scope(request))
    teams = service.list_teams().rows
    form = GameForm(request.POST or None, initial={"year": date.today().year})

    if request.method == "POST" and form.is_valid():
        home_team_id = form.cleaned_data["home_team"]
        away_team_id = form.cleaned_data["away_team"]
        if not build_permission_query(_real_scope(request)).can_manage_any(request.user, (home_team_id, away_team_id)):
            messages.error(request, "どちらのチームも担当していないため、この試合は登録できません。")
        else:
            try:
                game = build_recording_service(_real_scope(request)).create_game(
                    year=form.cleaned_data["year"],
                    played_on=form.cleaned_data["played_on"],
                    home_team_id=home_team_id,
                    away_team_id=away_team_id,
                )
            except DomainError as error:
                messages.error(request, str(error))
            else:
                messages.success(request, "試合を登録しました。続けて成績を入力できます。")
                return redirect(reverse("game_edit", args=[game.id]))
    elif request.method == "POST":
        messages.error(request, _first_error(form))

    return render(request, "myapp/game_form.html", {"form": form, "teams": teams})


@login_required
@require_GET
def game_edit(request, game_id):
    """試合の基本情報と、両チームのロスターぶんの成績を入力する画面を返す。

    編集フォームは React（frontend/src/game_edit/）が描画する。ここでは
    初期データを埋め込んだ器を返すだけで、保存は presentation/api.py が担う
    （保存経路を2つ残すと検証・文言の出典が増えるため、POST は受け付けない。
    require_GET により POST は 405 になり、旧フォームからの投稿が
    黙って捨てられて 200 が返る、という無反応な行き止まりを避ける）。
    """
    try:
        data = build_recording_service(_real_scope(request)).get_game_edit_data(game_id)
    except GameNotFound:
        raise Http404("試合が見つかりません。") from None

    header = data.header
    if not build_permission_query(_real_scope(request)).can_manage_any(
        request.user, (header.home_team_id, header.away_team_id)
    ):
        raise PermissionDenied("このチームを編集する権限がありません。")

    return render(
        request,
        "myapp/game_edit.html",
        {"game": header, "payload": _game_edit_payload(request, data)},
    )


def _game_edit_payload(request, data: GameEditData) -> dict:
    """試合編集画面（React）に埋め込む初期データ。

    キーは保存 API（api_game_scorebook）のフォームのフィールド名と 1:1 の
    snake_case にし、送り返すときにそのまま使える形にする。

    **打席の語彙（結果・進塁の理由・塁・失策の種類）と、既定の進塁の対応表も
    ここに載せる。** TypeScript から Python の Enum は読めないので、画面側に
    同じ表を書くとずれても例外にならず、選択肢や既定値だけが静かに古くなる。
    払い出せば出典は1つのままになる。
    """
    game = data.header
    return {
        "game": {
            "id": game.id,
            "year": game.year,
            "played_on": game.played_on.isoformat(),
            "home_team": game.home_team_id,
            "away_team": game.away_team_id,
            "home_score": game.home_score,
            "away_score": game.away_score,
            # 手入力の事実。時刻は時:分の文字列、未記録は空文字・null
            "start_time": game.start_time,
            "end_time": game.end_time,
            "ends_next_day": game.ends_next_day,
            "interruption_minutes": game.interruption_minutes,
            "attendance": game.attendance,
        },
        "teams": [
            {
                "team_id": roster.team_id,
                "team_name": roster.team_name,
                "is_home": roster.is_home,
                "players": [
                    {
                        "id": player.id,
                        "name": player.name,
                        "number": player.number,
                        "position": player.position,
                        "is_pitcher": player.is_pitcher,
                    }
                    for player in roster.players
                ],
                "lineup": [
                    {
                        "batting_order": slot.batting_order,
                        "slot_sequence": slot.slot_sequence,
                        "fielding_position": slot.fielding_position,
                        "entered_inning": slot.entered_inning,
                        "entered_is_bottom": slot.entered_is_bottom,
                        "entered_batter": slot.entered_batter,
                        "player_id": slot.player_id,
                    }
                    for slot in roster.lineup
                ],
            }
            for roster in data.rosters
        ],
        "plate_appearances": [_plate_appearance_row(entry) for entry in data.plate_appearances],
        "vocabulary": _scorebook_vocabulary(),
        "max_innings": MAX_INNINGS,
        "urls": {
            "save": reverse("api_game_scorebook", args=[game.id]),
            "detail": reverse("game_detail", args=[game.id]),
        },
        "csrf_token": get_token(request),
    }


def _plate_appearance_row(entry: GameEditPlateAppearance) -> dict:
    """打席1つぶん。保存 API に送り返す形と同じにする。"""
    return {
        "sequence": entry.sequence,
        "inning": entry.inning,
        "is_bottom": entry.is_bottom,
        "batter_id": entry.batter_id,
        "pitcher_id": entry.pitcher_id,
        "batting_order": entry.batting_order,
        "slot_sequence": entry.slot_sequence,
        "result": entry.result,
        "fielded_by": FIELDED_BY_SEPARATOR.join(entry.fielded_by),
        "advances": [
            {
                "runner_id": advance.runner_id,
                "from_base": advance.from_base,
                "to_base": advance.to_base,
                "reason": advance.reason,
                "error_index": advance.error_index,
            }
            for advance in entry.advances
        ],
        "errors": [
            {"player_id": error.player_id, "position": error.position, "kind": error.kind} for error in entry.errors
        ],
    }


def _scorebook_vocabulary() -> dict:
    """打席の入力に要る語彙。すべてドメインの値オブジェクトから払い出す。

    結果には「打者がどこまで進むか」「走者がどう動くか」の既定値も添える。
    画面はこれを見て進塁を自動で埋めるので、対応表を持たなくて済む。
    """
    return {
        "results": [
            {
                "label": result.value,
                "retires_batter": result.retires_batter,
                "is_hit": result.is_hit,
                "counts_as_at_bat": result.counts_as_at_bat,
                "requires_error": result is PlateAppearanceResult.REACHED_ON_ERROR,
                "default_batter_base": result.default_batter_base.value,
                "default_batter_reason": result.default_batter_reason.value,
                "default_runner_advance": result.default_runner_advance.value,
                "default_runner_reason": result.default_runner_reason.value,
            }
            for result in PlateAppearanceResult
        ],
        "reasons": [
            {"label": reason.value, "is_out": reason.is_out, "earns_run_batted_in": reason.earns_run_batted_in}
            for reason in AdvanceReason
        ],
        "bases": [{"value": base.value, "label": base.label} for base in Base],
        "error_kinds": [kind.value for kind in ErrorKind],
        "fielding_positions": FieldingPosition.labels(),
        "defensive_positions": FieldingPosition.defensive_labels(),
        # 出場時刻を打席から導く位置（代打・代走・投手）。画面は出場した半回の入力欄を出さない
        "entry_derived_positions": [position.value for position in FieldingPosition if position.entry_is_derived],
    }


def game_detail(request, game_id, world_id=None):
    """試合詳細。その試合の出場選手の成績を並べる。"""
    _, games, world = _game_scope(request, world_id)
    try:
        detail = games.get_game_detail(game_id)
    except GameNotFound:
        raise Http404("試合が見つかりません。") from None

    # シミュレーションの試合は手で直さない
    can_edit = world is None and build_permission_query(_real_scope(request)).can_manage_any(
        request.user, (detail.game.home_team_id, detail.game.away_team_id)
    )
    return _render(request, "myapp/game_detail.html", {"detail": detail, "can_edit": can_edit}, world)


def _ratings_card(request, world: WorldContext | None, player_id: int) -> PlayerRatingsCard | None:
    """選手ページの能力のカード。能力はペナントの世界の選手だけで、実データの選手には出さない。"""
    if world is None:
        return None
    return build_pennant_ratings_service(_world_scope(request, world.world_id)).get_card(player_id)


def _ratings_history(request, world: WorldContext | None, player_id: int) -> PlayerRatingsHistory | None:
    """選手ページの能力の推移。能力と同じく、ペナントの世界の選手だけ。"""
    if world is None:
        return None
    return build_pennant_ratings_service(_world_scope(request, world.world_id)).get_history(player_id)


def _player_awards(
    request, world: WorldContext | None, team_id: int, player_id: int, played_years: set[int]
) -> list[AwardRow]:
    """選手ページの「獲得タイトル」。表彰はペナントの世界だけで、実データの選手には出さない（実データは保存しない）。"""
    if world is None:
        return []
    return build_pennant_history_service(_world_scope(request, world.world_id)).get_player_awards(
        world, team_id, player_id, played_years=played_years
    )


def player_detail(request, team_id, player_id, world_id=None):
    """選手の個人ページ。通算・年度別・月別の成績と、選んだ月の試合ごとの記録。

    月の指定（`?month=2026-04`）が不正なら application 側が最新の月に落とす。
    ここでは弾かず、そのまま渡す（並べ替えのキーと同じ扱い）。
    """
    service, world = _scope(request, world_id)
    try:
        profile = service.get_player_profile(team_id, player_id, month=request.GET.get("month"))
    except (TeamNotFound, PlayerNotFound):
        raise Http404("選手が見つかりません。") from None

    return _render(
        request,
        "myapp/player_detail.html",
        {
            "profile": profile,
            "player": profile.detail,
            "ratings_card": _ratings_card(request, world, player_id),
            "ratings_history": _ratings_history(request, world, player_id),
            "awards": _player_awards(request, world, team_id, player_id, {row.year for row in profile.years or []}),
            "can_edit_team": world is None
            and build_permission_query(_real_scope(request)).can_manage(request.user, team_id),
        },
        world,
    )


@login_required
def player_edit(request, team_id, player_id):
    """選手の基本情報と成績を編集する。"""
    service = build_service(_real_scope(request))

    try:
        detail = service.get_player_detail(team_id, player_id)
    except (TeamNotFound, PlayerNotFound):
        raise Http404("選手が見つかりません。") from None

    if not build_permission_query(_real_scope(request)).can_manage(request.user, team_id):
        raise PermissionDenied("このチームを編集する権限がありません。")

    if request.method == "POST":
        # 退団・主将の指名/解任はフォームの検証を通さず、押されたボタンで判断する
        if "retire" in request.POST:
            build_roster_service(_real_scope(request)).retire_player(team_id, player_id)
            messages.success(request, f"{detail.name} 選手を退団にしました。")
            return redirect(reverse("player_list", args=[team_id]))

        if "appoint_captain" in request.POST:
            try:
                build_roster_service(_real_scope(request)).appoint_captain(team_id, player_id)
            except DomainError as error:
                messages.error(request, str(error))
            else:
                messages.success(request, f"{detail.name} 選手を主将に指名しました。")
            return redirect(reverse("player_edit", args=[team_id, player_id]))

        if "promote" in request.POST:
            # 支配下登録は新しい背番号が要るので、フォームの欄を自分で読む。表記の検証はドメインが行う
            new_number = request.POST.get("promote_number", "").strip()
            try:
                build_roster_service(_real_scope(request)).promote_player(team_id, player_id, new_number)
            except DomainError as error:
                messages.error(request, str(error))
            else:
                messages.success(request, f"{detail.name} 選手を支配下登録にしました（背番号 {new_number}）。")
            return redirect(reverse("player_edit", args=[team_id, player_id]))

        if "declare_fa" in request.POST or "remove_fa" in request.POST:
            # FA 宣言の追加・取り消しも、フォームの検証を通さず押されたボタンで判断する
            _handle_free_agency(request, team_id, player_id, detail.name)
            return redirect(reverse("player_edit", args=[team_id, player_id]))

        if "remove_captain" in request.POST:
            build_roster_service(_real_scope(request)).remove_captain(team_id, player_id)
            messages.success(request, f"{detail.name} 選手の主将を解任しました。")
            return redirect(reverse("player_edit", args=[team_id, player_id]))

        base_form = PlayerUpdateForm(request.POST)
        if base_form.is_valid():
            try:
                build_roster_service(_real_scope(request)).update_player(
                    team_id=team_id,
                    player_id=player_id,
                    name=base_form.cleaned_data["name"],
                    number=base_form.cleaned_data["number"],
                    position_label=base_form.cleaned_data["position"],
                )
            except DomainError as error:
                messages.error(request, str(error))
            else:
                messages.success(request, "選手情報を更新しました。")
                mode = PITCHER_MODE if base_form.cleaned_data["position"] == Position.PITCHER.value else BATTER_MODE
                return redirect(f"{reverse('player_list', args=[team_id])}?pos={mode}")
        else:
            messages.error(request, _first_error(base_form))

        detail = service.get_player_detail(team_id, player_id)

    return render(
        request,
        "myapp/player_edit.html",
        {
            "player": detail,
            "positions": Position.labels(),
            # FA 宣言の入力。種別の選択肢はドメインの FreeAgencyKind が出典
            "fa_kinds": FreeAgencyKind.labels(),
            "current_year": date.today().year,
        },
    )


def _handle_free_agency(request, team_id, player_id, player_name) -> None:
    """選手の編集画面の FA 宣言の追加・取り消し。結果はメッセージで知らせる。"""
    roster = build_roster_service(_real_scope(request))
    try:
        if "remove_fa" in request.POST:
            year = int(request.POST.get("remove_fa", ""))
            roster.remove_free_agency_declaration(team_id, player_id, year)
            messages.success(request, f"{player_name} 選手の{year}年の FA 宣言を取り消しました。")
        else:
            year = int(request.POST.get("fa_year", ""))
            roster.declare_free_agency(team_id, player_id, year, request.POST.get("fa_kind", ""))
            messages.success(request, f"{player_name} 選手の{year}年の FA 宣言を記録しました。")
    except ValueError:
        messages.error(request, "FA を宣言した年を数値で入力してください。")
    except DomainError as error:
        messages.error(request, str(error))


def _first_error(form) -> str:
    """フォームのエラーを画面表示用の1行にまとめる。"""
    for field, errors in form.errors.items():
        label = form.fields[field].label if field in form.fields else field
        return f"{label}: {errors[0]}"
    return ""


class SignUpView(CreateView):
    """新規ユーザー登録。登録後はログイン画面へ遷移する。"""

    form_class = UserCreationForm
    template_name = "registration/signup.html"
    success_url = reverse_lazy("login")


def _int_param(request, name):
    """URL の整数パラメータ。無い・読めない値は None（不正値はエラーにせず既定に落とす）。"""
    value = request.GET.get(name)
    return int(value) if value and value.isdigit() else None


@require_GET
def analysis_index(request, world_id=None):
    """戦力分析の入口。球団を選んでいればその球団、無ければ表示順で先頭の球団へ移る。

    球団の切り替えフォーム（JS なしの GET）がここへ `team` と `year` を送る。
    """
    service, _, world = _analysis_scope(request, world_id)
    team_id = _int_param(request, "team")
    if team_id is None or not service.has_team(team_id):
        team_id = service.first_team_id()
    if team_id is None:
        return _render(request, "myapp/team_analysis.html", {"analysis": None}, world)

    query = {
        name: request.GET[name] for name in ("year", "tab", "color", "pos", "sort", "dir") if request.GET.get(name)
    }
    if world is None:
        url = reverse("team_analysis", args=[team_id])
    else:
        url = reverse("pennant_team_analysis", args=[world.world_id, team_id])
    return redirect(f"{url}?{urlencode(query)}" if query else url)


@require_GET
def team_analysis(request, team_id, world_id=None):
    """球団×年度の戦力分析。閲覧だけで、書き込みの導線は無い。"""
    analysis_service, stats, world = _analysis_scope(request, world_id)
    try:
        analysis = analysis_service.get_analysis(
            team_id, year=_int_param(request, "year"), tab=request.GET.get("tab"), color=request.GET.get("color")
        )
    except TeamNotFound:
        raise Http404("チームが見つかりません。") from None
    context: dict[str, object] = {"analysis": analysis}
    if analysis.tab == STATS_TAB:
        # 成績一覧のタブだけ、その年に在籍した選手のその年の成績を読む（ほかのタブのクエリ数を増やさない）。
        # 表の材料は選手一覧と同じ `TeamApplicationService`。戦力分析のサービスにチームの表は持たせない
        pos_mode = PITCHER_MODE if request.GET.get("pos") == PITCHER_MODE else BATTER_MODE
        sort, descending = _sort_params(request)
        listing = (
            stats.list_pitchers(team_id, sort=sort, descending=descending, year=analysis.year)
            if pos_mode == PITCHER_MODE
            else stats.list_batters(team_id, sort=sort, descending=descending, year=analysis.year)
        )
        stats_tab = analysis_service.stats_tab(analysis, listing.rows)
        context.update(
            {
                "stats": stats_tab,
                "players": stats_tab.rows,
                "pos_mode": pos_mode,
                "team_id": team_id,
                "current_sort": listing.sort,
                "current_descending": listing.descending,
            }
        )
    return _render(request, "myapp/team_analysis.html", context, world)
