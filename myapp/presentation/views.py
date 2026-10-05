"""ビュー。

責務は「HTTP を解釈してアプリケーションサービスを呼び、結果を描画する」ことだけ。
成績の計算も背番号の重複判定もここには無い（ドメイン層にある）。
"""

import secrets
from collections.abc import Callable
from datetime import date, timedelta

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
from django.views.decorators.http import require_GET
from django.views.generic import CreateView

from ..application.club_management import ClubManagementService
from ..application.dto import GameEditData, GameEditPlateAppearance, WorldContext
from ..application.game_recording import GameRecordingService
from ..application.pennant_home import PennantHomeService
from ..application.pennant_season import PennantSeasonService
from ..application.pennant_view import PennantWorldViewService
from ..application.pennant_world import PennantWorldService, WorldRepositories
from ..application.queries import SimulationContextQuery
from ..application.services import TeamApplicationService
from ..domain.exceptions import (
    AlreadyAdvanced,
    DomainError,
    GameNotFound,
    InvalidWorld,
    LeagueNotFound,
    PlayerNotFound,
    TeamNotFound,
    WorldNotFound,
)
from ..domain.pennant.schedule import AdvanceTarget
from ..domain.pennant.season import MAX_GAMES_PER_ADVANCE, SCREEN_ADVANCE_TARGETS, world_today
from ..domain.pennant.world import WorldScope
from ..domain.value_objects import (
    AdvanceReason,
    Base,
    ErrorKind,
    FieldingPosition,
    PlateAppearanceResult,
    Position,
)
from ..infrastructure.queries import (
    DjangoFieldingTotalsQuery,
    DjangoGameListQuery,
    DjangoPennantActivityQuery,
    DjangoPlayerFieldingQuery,
    DjangoPlayerSearchQuery,
    DjangoPlayerStatsQuery,
    DjangoSimulationContextQuery,
    DjangoTeamListQuery,
    DjangoTeamPermissionQuery,
    DjangoWorldSummaryQuery,
)
from ..infrastructure.repositories import (
    DjangoClubPlanRepository,
    DjangoFixtureRepository,
    DjangoGameRepository,
    DjangoLeagueRepository,
    DjangoRatingsRepository,
    DjangoTeamRepository,
    DjangoWorldRepository,
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
    if not build_permission_query().can_manage_any(request.user, team_ids):
        raise PermissionDenied("このチームを編集する権限がありません。")
    return None


def build_service() -> TeamApplicationService:
    """依存を組み立てる。差し替えたい場合はここだけ変えればよい。

    組み立ては**この1か所だけ**にする（管理画面のテンプレートタグもテストも
    ここを呼ぶ）。呼ぶ側ごとに一部の依存だけを渡すと、使う画面によって
    落ちるサービスができてしまうため。

    **実データの範囲に固定する。引数で範囲を切り替えない**（既定値で切り替える形にすると、
    渡し忘れが「ペナントの画面に実データが出る」形で現れる）。
    """
    scope = WorldScope.real()
    return TeamApplicationService(
        teams=DjangoTeamRepository(scope),
        team_list_query=DjangoTeamListQuery(scope),
        games=DjangoGameRepository(scope),
        leagues=DjangoLeagueRepository(scope),
        game_list_query=DjangoGameListQuery(scope),
        player_fielding_query=DjangoPlayerFieldingQuery(scope),
        player_stats_query=DjangoPlayerStatsQuery(scope),
        today=date.today,
    )


def build_world_view_service(world_id: int) -> TeamApplicationService:
    """ペナントの世界ひとつの画面（順位・試合・選手）を読むサービスを組み立てる。

    `build_service()` と同じ `TeamApplicationService` を、**世界の範囲で**組み立てる。依存は
    `build_service()` と同じ全部を、すべて渡された世界の範囲で作る（範囲の外の球団・試合には触れない）。
    年齢の基準日は暦の今日ではなく、その世界の「今日」（最後に試合をした日。まだ無ければ開幕日）。
    世界の id が正しくなければ InvalidWorld。存在しない世界は、年齢を数えるときに WorldNotFound。
    """
    scope = WorldScope.pennant(world_id)
    return TeamApplicationService(
        teams=DjangoTeamRepository(scope),
        team_list_query=DjangoTeamListQuery(scope),
        games=DjangoGameRepository(scope),
        leagues=DjangoLeagueRepository(scope),
        game_list_query=DjangoGameListQuery(scope),
        player_fielding_query=DjangoPlayerFieldingQuery(scope),
        player_stats_query=DjangoPlayerStatsQuery(scope),
        today=_world_clock(world_id, DjangoSimulationContextQuery(scope)),
    )


def _world_clock(world_id: int, context_query: SimulationContextQuery) -> Callable[[], date]:
    """その世界の「今日」を返す関数。呼ばれるたびに読み直す（進めた後に古い日付を返さない）。"""
    worlds = DjangoWorldRepository()

    def today() -> date:
        return world_today(worlds.find_by_id(world_id).start_year, context_query.last_played_on())

    return today


def build_pennant_world_view() -> PennantWorldViewService:
    """世界の見出し（世界バー・世界の一覧）を作るサービスを組み立てる。

    見出しの材料は世界の台帳を読む参照クエリ（範囲を持たない）が一定のクエリ数でまとめて読む。
    自軍の順位だけは、世界の範囲のサービス（`build_world_view_service`）に任せる。
    """
    return PennantWorldViewService(
        summaries=DjangoWorldSummaryQuery(),
        standings_for=build_world_view_service,
    )


def build_recording_service() -> GameRecordingService:
    """スコアブックを保存するサービスを組み立てる。

    `build_service()` と同じく**組み立てはここだけ**にする。打席の記録は
    チームの一覧も試合の一覧も要らないので、依存は3つで足りる。実データの範囲に固定する。
    """
    scope = WorldScope.real()
    return GameRecordingService(
        games=DjangoGameRepository(scope),
        teams=DjangoTeamRepository(scope),
        leagues=DjangoLeagueRepository(scope),
    )


def build_permission_query() -> DjangoTeamPermissionQuery:
    """チームの編集権限の判定を組み立てる。実データの範囲に固定する。"""
    return DjangoTeamPermissionQuery(WorldScope.real())


def build_player_search_query() -> DjangoPlayerSearchQuery:
    """選手検索を組み立てる。実データの範囲に固定する。"""
    return DjangoPlayerSearchQuery(WorldScope.real())


def _repositories_for(scope: WorldScope) -> WorldRepositories:
    """世界の範囲でリポジトリを組み立てる。世界の作成が、写し先の世界に書くのに使う。"""
    return WorldRepositories(
        leagues=DjangoLeagueRepository(scope),
        teams=DjangoTeamRepository(scope),
        ratings=DjangoRatingsRepository(scope),
    )


def build_pennant_world_service() -> PennantWorldService:
    """世界の作成・削除のサービスを組み立てる。

    分岐元は**実データの範囲**で読み、写し先は世界の範囲のリポジトリ（生成器で受け取る）に書く。
    """
    scope = WorldScope.real()
    return PennantWorldService(
        real_leagues=DjangoLeagueRepository(scope),
        real_teams=DjangoTeamRepository(scope),
        real_fielding=DjangoFieldingTotalsQuery(scope),
        worlds=DjangoWorldRepository(),
        repositories_for=_repositories_for,
        atomic=transaction.atomic,
        ensure_schedule=lambda world_id: build_pennant_season_service(world_id).ensure_schedule(),
    )


def build_pennant_season_service(world_id: int) -> PennantSeasonService:
    """ペナントの世界ひとつのシーズン進行サービスを組み立てる。

    **リポジトリと参照クエリは、すべて渡された世界の範囲で作る**（範囲の外の球団・試合・日程には触れない）。
    保存と消化済みの対戦の削除を1つにまとめるトランザクションは、ここで渡す（application は Django を知らない）。
    世界の id が正しくなければ InvalidWorld、存在しなければ使うときに WorldNotFound。
    """
    scope = WorldScope.pennant(world_id)
    return PennantSeasonService(
        world_id=world_id,
        worlds=DjangoWorldRepository(),
        leagues=DjangoLeagueRepository(scope),
        teams=DjangoTeamRepository(scope),
        games=DjangoGameRepository(scope),
        fixtures=DjangoFixtureRepository(scope),
        ratings=DjangoRatingsRepository(scope),
        plans=DjangoClubPlanRepository(scope),
        context_query=DjangoSimulationContextQuery(scope),
        atomic=transaction.atomic,
    )


def build_pennant_home_service(world_id: int) -> PennantHomeService:
    """GM ホームの材料を作るサービスを組み立てる。

    順位・主力・タイトルは世界の範囲の `TeamApplicationService`（`build_world_view_service`）に任せ、
    試合の一覧・日程・期間の見どころの参照は、すべて渡された世界の範囲で作る。
    世界の id が正しくなければ InvalidWorld。
    """
    scope = WorldScope.pennant(world_id)
    return PennantHomeService(
        teams=build_world_view_service(world_id),
        games=DjangoGameListQuery(scope),
        game_records=DjangoGameRepository(scope),
        fixtures=DjangoFixtureRepository(scope),
        worlds=DjangoWorldRepository(),
        activity=DjangoPennantActivityQuery(scope),
    )


def _world_context(world_id: int) -> WorldContext:
    """世界の見出し。存在しない・id が正しくない世界は 404。"""
    try:
        return build_pennant_world_view().get_context(world_id)
    except (WorldNotFound, InvalidWorld):
        raise Http404("世界が見つかりません。") from None


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


def _scope(world_id: int | None) -> tuple[TeamApplicationService, WorldContext | None]:
    """画面が読む範囲。URL に世界の id が無ければ実データ、あればその世界（無ければ 404）。

    実データの画面とペナントの画面は同じビューとテンプレートを使う。違いは、読むサービスの範囲と、
    テンプレートに渡す世界（`world`。リンクの引き方・世界バー・文言の差し替えの合図）だけ。
    """
    if world_id is None:
        return build_service(), None
    world = _world_context(world_id)
    return build_world_view_service(world_id), world


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


def _next_season_year() -> int:
    """世界の開幕年の既定。実データの最新シーズンの翌年（試合が無ければ今年）。"""
    latest = build_service().latest_game_year()
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

    views = build_pennant_world_view()
    form = None
    if request.user.is_authenticated:
        form_args = {"groups": build_service().list_teams_by_league().rows, "default_year": _next_season_year()}
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
        created = build_pennant_world_service().create_world_with_schedule(
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
    world = _world_context(world_id)
    is_owner = _is_owner(request, world)
    league = request.GET.get("league")
    home = build_pennant_home_service(world_id).get_home(
        world,
        since=_parse_date(request.GET.get("since")),
        league_id=int(league) if league and league.isdigit() else None,
        include_advance=is_owner,
    )
    return _render(request, "pennant/home.html", {"home": home, "is_owner": is_owner}, world)


def pennant_advance(request, world_id):
    """世界を進める。POST だけで、オーナーだけが使える（GET はホームへ戻す）。

    進めたあとは `?since=<進める前の今日>` つきでホームへ戻る（結果は URL に残るので、再読み込みしても消えない）。
    `expected_today` がずれていたら進めない（二重送信や、別の画面で先に進めたときに、さらに進めてしまわない）。
    """
    world = _world_context(world_id)
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
        report = build_pennant_season_service(world_id).advance(
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


def pennant_delete(request, world_id):
    """世界の削除の確認。GET も POST もオーナーだけ。削除したら世界の一覧へ戻る。"""
    world = _world_context(world_id)
    if request.method not in ("GET", "HEAD", "POST"):
        return HttpResponseNotAllowed(["GET", "HEAD", "POST"])
    denied = _requires_world_owner(request, world)
    if denied is not None:
        return denied

    if request.method == "POST":
        build_pennant_world_service().delete_world(world_id)
        messages.success(request, f"世界「{world.name}」を削除しました。")
        return redirect("pennant_index")
    deletion = build_pennant_home_service(world_id).get_deletion(world)
    return _render(request, "pennant/world_delete.html", {"deletion": deletion}, world)


def build_club_service(world_id: int) -> ClubManagementService:
    """ペナントの世界ひとつの、球団の編成を管理するサービスを組み立てる。

    `build_pennant_season_service()` と同じく、**リポジトリと参照クエリはすべて渡された世界の範囲で作る**
    （他の世界の球団の編成は、読めも書けもしない）。世界の id が正しくなければ InvalidWorld。
    """
    scope = WorldScope.pennant(world_id)
    return ClubManagementService(
        world_id=world_id,
        worlds=DjangoWorldRepository(),
        plans=DjangoClubPlanRepository(scope),
        ratings=DjangoRatingsRepository(scope),
        fixtures=DjangoFixtureRepository(scope),
        context_query=DjangoSimulationContextQuery(scope),
    )


def dashboard(request):
    """ホーム画面。リーグ全体の概況と各種ランキングを表示する。"""
    return render(request, "myapp/dashboard.html", {"board": build_service().get_dashboard()})


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
    service, world = _scope(world_id)
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
    service, world = _scope(world_id)
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
    service, world = _scope(world_id)
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
                service.register_player(
                    team_id=team_id,
                    name=form.cleaned_data["name"],
                    number=form.cleaned_data["number"],
                    position_label=form.cleaned_data["position"],
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
    listing = (
        service.list_pitchers(team_id, sort=sort, descending=descending)
        if pos_mode == PITCHER_MODE
        else service.list_batters(team_id, sort=sort, descending=descending)
    )

    return _render(
        request,
        "myapp/player_list.html",
        {
            "team_id": team_id,
            "team_name": team_name,
            "totals": service.get_team_totals(team_id),
            "listing": listing,
            "players": listing.rows,
            "pos_mode": pos_mode,
            "form": form,
            "positions": Position.labels(),
            "current_sort": listing.sort,
            "current_descending": listing.descending,
            # 通算値では見えない調子の波を、月ごとに区切って出す
            "months": service.list_team_monthly_splits(team_id),
            # このチームの担当者（または管理ユーザー）だけが登録・編集の導線を見える
            "can_edit_team": world is None and build_permission_query().can_manage(request.user, team_id),
        },
        world,
    )


def player_search(request):
    """選手を名前で探す。チームが増えると所属からはたどり着きにくいため。"""
    keyword = (request.GET.get("q") or "").strip()
    results = build_player_search_query().search(keyword) if keyword else []

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
    service, world = _scope(world_id)
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
    service, world = _scope(world_id)
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

    service, world = _scope(world_id)
    try:
        stats = service.get_league_stats(
            league_id,
            pitchers=pos_mode == PITCHER_MODE,
            qualified=qualified,
            sort=sort,
            descending=descending,
        )
    except LeagueNotFound:
        raise Http404("リーグが見つかりません。") from None

    return _render(
        request,
        "myapp/league_stats.html",
        {
            "stats": stats,
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
    service, world = _scope(world_id)

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
        year = service.latest_game_year()

    months = service.list_game_months(year=year, team_id=team_id, league_id=league_id)
    # 月を選んでいないとき（＝一覧を開いた直後）と、年・リーグ・チームを変えて
    # 選んでいた月に試合が無くなったときは、その範囲の最新の月に落とす。
    # 全件を一度に描くと件数ぶん重くなるため、月は必ず1つに決める
    if months and month not in months:
        month = months[-1]

    listing = service.list_games(year=year, team_id=team_id, month=month, league_id=league_id)
    leagues = service.list_leagues()

    return _render(
        request,
        "myapp/game_list.html",
        {
            "games": listing.rows,
            "years": service.list_game_seasons(),
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
            and build_permission_query().can_manage_any(request.user, [t.id for t in all_teams]),
        },
        world,
    )


@login_required
def game_create(request):
    """試合を作る。作成後、成績の入力画面へ進む。"""
    service = build_service()
    teams = service.list_teams().rows
    form = GameForm(request.POST or None, initial={"year": date.today().year})

    if request.method == "POST" and form.is_valid():
        home_team_id = form.cleaned_data["home_team"]
        away_team_id = form.cleaned_data["away_team"]
        if not build_permission_query().can_manage_any(request.user, (home_team_id, away_team_id)):
            messages.error(request, "どちらのチームも担当していないため、この試合は登録できません。")
        else:
            try:
                game = service.create_game(
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
    service = build_service()

    try:
        data = service.get_game_edit_data(game_id)
    except GameNotFound:
        raise Http404("試合が見つかりません。") from None

    header = data.header
    if not build_permission_query().can_manage_any(request.user, (header.home_team_id, header.away_team_id)):
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
    service, world = _scope(world_id)
    try:
        detail = service.get_game_detail(game_id)
    except GameNotFound:
        raise Http404("試合が見つかりません。") from None

    # シミュレーションの試合は手で直さない
    can_edit = world is None and build_permission_query().can_manage_any(
        request.user, (detail.game.home_team_id, detail.game.away_team_id)
    )
    return _render(request, "myapp/game_detail.html", {"detail": detail, "can_edit": can_edit}, world)


def player_detail(request, team_id, player_id, world_id=None):
    """選手の個人ページ。通算・年度別・月別の成績と、選んだ月の試合ごとの記録。

    月の指定（`?month=2026-04`）が不正なら application 側が最新の月に落とす。
    ここでは弾かず、そのまま渡す（並べ替えのキーと同じ扱い）。
    """
    service, world = _scope(world_id)
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
            "can_edit_team": world is None and build_permission_query().can_manage(request.user, team_id),
        },
        world,
    )


@login_required
def player_edit(request, team_id, player_id):
    """選手の基本情報と成績を編集する。"""
    service = build_service()

    try:
        detail = service.get_player_detail(team_id, player_id)
    except (TeamNotFound, PlayerNotFound):
        raise Http404("選手が見つかりません。") from None

    if not build_permission_query().can_manage(request.user, team_id):
        raise PermissionDenied("このチームを編集する権限がありません。")

    if request.method == "POST":
        # 退団・主将の指名/解任はフォームの検証を通さず、押されたボタンで判断する
        if "retire" in request.POST:
            service.retire_player(team_id, player_id)
            messages.success(request, f"{detail.name} 選手を退団にしました。")
            return redirect(reverse("player_list", args=[team_id]))

        if "appoint_captain" in request.POST:
            try:
                service.appoint_captain(team_id, player_id)
            except DomainError as error:
                messages.error(request, str(error))
            else:
                messages.success(request, f"{detail.name} 選手を主将に指名しました。")
            return redirect(reverse("player_edit", args=[team_id, player_id]))

        if "remove_captain" in request.POST:
            service.remove_captain(team_id, player_id)
            messages.success(request, f"{detail.name} 選手の主将を解任しました。")
            return redirect(reverse("player_edit", args=[team_id, player_id]))

        base_form = PlayerUpdateForm(request.POST)
        if base_form.is_valid():
            try:
                service.update_player(
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
        },
    )


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
