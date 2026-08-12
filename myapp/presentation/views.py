"""ビュー。

責務は「HTTP を解釈してアプリケーションサービスを呼び、結果を描画する」ことだけ。
成績の計算も背番号の重複判定もここには無い（ドメイン層にある）。
"""

from datetime import date

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.middleware.csrf import get_token
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy
from django.views.decorators.http import require_GET
from django.views.generic import CreateView

from ..application.game_recording import GameRecordingService
from ..application.services import TeamApplicationService
from ..domain.exceptions import (
    DomainError,
    GameNotFound,
    LeagueNotFound,
    PlayerNotFound,
    TeamNotFound,
)
from ..domain.value_objects import (
    AdvanceReason,
    Base,
    ErrorKind,
    FieldingPosition,
    PlateAppearanceResult,
    Position,
)
from ..infrastructure.queries import (
    DjangoGameListQuery,
    DjangoPlayerSearchQuery,
    DjangoTeamListQuery,
    DjangoTeamPermissionQuery,
)
from ..infrastructure.repositories import (
    DjangoGameRepository,
    DjangoLeagueRepository,
    DjangoTeamRepository,
)
from .forms import (
    FIELDED_BY_SEPARATOR,
    MAX_INNINGS,
    GameForm,
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
    if not DjangoTeamPermissionQuery().can_manage_any(request.user, team_ids):
        raise PermissionDenied("このチームを編集する権限がありません。")
    return None


def build_service() -> TeamApplicationService:
    """依存を組み立てる。差し替えたい場合はここだけ変えればよい。

    組み立ては**この1か所だけ**にする（管理画面のテンプレートタグもテストも
    ここを呼ぶ）。呼ぶ側ごとに一部の依存だけを渡すと、使う画面によって
    落ちるサービスができてしまうため。
    """
    return TeamApplicationService(
        teams=DjangoTeamRepository(),
        team_list_query=DjangoTeamListQuery(),
        games=DjangoGameRepository(),
        leagues=DjangoLeagueRepository(),
        game_list_query=DjangoGameListQuery(),
    )


def build_recording_service() -> GameRecordingService:
    """スコアブックを保存するサービスを組み立てる。

    `build_service()` と同じく**組み立てはここだけ**にする。打席の記録は
    チームの一覧も試合の一覧も要らないので、依存は3つで足りる。
    """
    return GameRecordingService(
        games=DjangoGameRepository(),
        teams=DjangoTeamRepository(),
        leagues=DjangoLeagueRepository(),
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


def team_list(request):
    """チーム一覧。"""
    sort, descending = _sort_params(request)
    listing = build_service().list_teams_by_league(sort=sort, descending=descending)
    return render(
        request,
        "myapp/team_list.html",
        {
            "leagues": listing.rows,
            # 件数の表示や既存の判定に使うため、平坦にしたものも渡す
            "teams": [team for group in listing.rows for team in group.teams],
            "current_sort": listing.sort,
            "current_descending": listing.descending,
        },
    )


def standings(request, year=None):
    """年別の順位表。年を指定しない場合は最新シーズン。"""
    sort, descending = _sort_params(request)
    try:
        board = build_service().get_standings(year, sort=sort, descending=descending)
    except DomainError as error:
        raise Http404(str(error)) from error

    return render(
        request,
        "myapp/standings.html",
        {
            "standings": board,
            "current_sort": board.sort,
            "current_descending": board.descending,
        },
    )


def player_list(request, team_id):
    """選手一覧。野手／投手モードを切り替えて表示する。"""
    service = build_service()

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

    return render(
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
            "can_edit_team": DjangoTeamPermissionQuery().can_manage(request.user, team_id),
        },
    )


def player_search(request):
    """選手を名前で探す。チームが増えると所属からはたどり着きにくいため。"""
    keyword = (request.GET.get("q") or "").strip()
    results = DjangoPlayerSearchQuery().search(keyword) if keyword else []

    return render(
        request,
        "myapp/player_search.html",
        {
            "keyword": keyword,
            "results": results,
            "searched": bool(keyword),
        },
    )


def league_detail(request, league_id, year=None):
    """リーグ画面。所属チーム・順位表・直近の試合。"""
    try:
        detail = build_service().get_league_detail(league_id, year)
    except LeagueNotFound:
        raise Http404("リーグが見つかりません。") from None

    return render(request, "myapp/league_detail.html", {"league": detail})


def league_titles(request, league_id, year=None):
    """リーグのタイトル一覧。部門別の上位者をシーズンで区切って並べる。"""
    try:
        titles = build_service().get_league_titles(league_id, year)
    except LeagueNotFound:
        raise Http404("リーグが見つかりません。") from None
    except DomainError as error:
        raise Http404(str(error)) from error

    return render(request, "myapp/league_titles.html", {"titles": titles})


def league_stats(request, league_id):
    """リーグの成績一覧。所属する全選手の通算成績を並べ替えて見る。"""
    pos_mode = PITCHER_MODE if request.GET.get("pos") == PITCHER_MODE else BATTER_MODE
    # 規定の絞り込み。指定が無い・読めない値なら全員（並べ替えのキーと同じ扱い）
    qualified = request.GET.get("qualified") == "1"
    sort, descending = _sort_params(request)

    try:
        stats = build_service().get_league_stats(
            league_id,
            pitchers=pos_mode == PITCHER_MODE,
            qualified=qualified,
            sort=sort,
            descending=descending,
        )
    except LeagueNotFound:
        raise Http404("リーグが見つかりません。") from None

    return render(
        request,
        "myapp/league_stats.html",
        {
            "stats": stats,
            "players": stats.listing.rows,
            "pos_mode": pos_mode,
            "current_sort": stats.listing.sort,
            "current_descending": stats.listing.descending,
        },
    )


def game_list(request):
    """試合一覧。シーズン・月・リーグ・チームで絞り込める。

    全件を一度に描くと件数ぶん重くなるため、指定が無ければ最新シーズンの
    最後に試合があった月を見せる。
    """
    service = build_service()

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

    return render(
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
            "can_create_game": DjangoTeamPermissionQuery().can_manage_any(request.user, [t.id for t in all_teams]),
        },
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
        if not DjangoTeamPermissionQuery().can_manage_any(request.user, (home_team_id, away_team_id)):
            messages.error(request, "どちらのチームも担当していないため、この試合は登録できません。")
        else:
            try:
                game = service.create_game(
                    year=form.cleaned_data["year"],
                    played_on=form.cleaned_data["played_on"],
                    home_team_id=home_team_id,
                    away_team_id=away_team_id,
                    home_score=form.cleaned_data["home_score"],
                    away_score=form.cleaned_data["away_score"],
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

    game, rosters = data["game"], data["rosters"]
    if not DjangoTeamPermissionQuery().can_manage_any(request.user, (game.home_team_id, game.away_team_id)):
        raise PermissionDenied("このチームを編集する権限がありません。")

    return render(
        request,
        "myapp/game_edit.html",
        {"game": game, "payload": _game_edit_payload(request, game, rosters)},
    )


def _game_edit_payload(request, game, rosters) -> dict:
    """試合編集画面（React）に埋め込む初期データ。

    キーは保存 API（api_game_scorebook）のフォームのフィールド名と 1:1 の
    snake_case にし、送り返すときにそのまま使える形にする。

    **打席の語彙（結果・進塁の理由・塁・失策の種類）と、既定の進塁の対応表も
    ここに載せる。** TypeScript から Python の Enum は読めないので、画面側に
    同じ表を書くとずれても例外にならず、選択肢や既定値だけが静かに古くなる。
    払い出せば出典は1つのままになる。
    """
    lineup = {
        entry.player_id: {
            "batting_order": entry.batting_order,
            "slot_sequence": entry.slot_sequence,
            "fielding_position": entry.fielding_position.value if entry.fielding_position else "",
        }
        for entry in game.batting
    }

    return {
        "game": {
            "id": game.id,
            "year": game.season.year,
            "played_on": game.played_on.isoformat(),
            "home_team": game.home_team_id,
            "away_team": game.away_team_id,
            "home_score": game.home_score,
            "away_score": game.away_score,
        },
        "teams": [
            {
                "team_id": roster["team_id"],
                "team_name": roster["team_name"],
                # rosters が home を先頭に返す前提に頼らず、試合の home_team_id と比べて決める
                "is_home": roster["team_id"] == game.home_team_id,
                "players": [
                    {
                        "id": player["id"],
                        "name": player["name"],
                        "number": player["number"],
                        "position": player["position"],
                        "is_pitcher": player["is_pitcher"],
                    }
                    for player in roster["players"]
                ],
                "lineup": [
                    dict(lineup[player["id"]], player_id=player["id"])
                    for player in roster["players"]
                    if lineup.get(player["id"], {}).get("batting_order") is not None
                ],
            }
            for roster in rosters
        ],
        "plate_appearances": [_plate_appearance_row(entry) for entry in game.plate_appearances_in_order()],
        "vocabulary": _scorebook_vocabulary(),
        "max_innings": MAX_INNINGS,
        "urls": {
            "save": reverse("api_game_scorebook", args=[game.id]),
            "detail": reverse("game_detail", args=[game.id]),
        },
        "csrf_token": get_token(request),
    }


def _plate_appearance_row(entry) -> dict:
    """打席1つぶん。保存 API に送り返す形と同じにする。"""
    return {
        "sequence": entry.sequence,
        "inning": entry.inning,
        "is_bottom": entry.is_bottom,
        "batter_id": entry.batter_id,
        "pitcher_id": entry.pitcher_id,
        "batting_order": entry.batting_order,
        "slot_sequence": entry.slot_sequence,
        "result": entry.result.value,
        "fielded_by": FIELDED_BY_SEPARATOR.join(position.value for position in entry.fielded_by),
        "advances": [
            {
                "runner_id": advance.runner_id,
                "from_base": advance.from_base.value,
                "to_base": advance.to_base.value,
                "reason": advance.reason.value,
                "error_index": advance.error_index,
            }
            for advance in entry.advances
        ],
        "errors": [
            {"player_id": error.player_id, "position": error.position.value, "kind": error.kind.value}
            for error in entry.errors
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
    }


def game_detail(request, game_id):
    """試合詳細。その試合の出場選手の成績を並べる。"""
    try:
        detail = build_service().get_game_detail(game_id)
    except GameNotFound:
        raise Http404("試合が見つかりません。") from None

    can_edit = DjangoTeamPermissionQuery().can_manage_any(
        request.user, (detail.game.home_team_id, detail.game.away_team_id)
    )
    return render(request, "myapp/game_detail.html", {"detail": detail, "can_edit": can_edit})


def player_detail(request, team_id, player_id):
    """選手の個人ページ。通算・年度別・月別の成績と、選んだ月の試合ごとの記録。

    月の指定（`?month=2026-04`）が不正なら application 側が最新の月に落とす。
    ここでは弾かず、そのまま渡す（並べ替えのキーと同じ扱い）。
    """
    try:
        profile = build_service().get_player_profile(team_id, player_id, month=request.GET.get("month"))
    except (TeamNotFound, PlayerNotFound):
        raise Http404("選手が見つかりません。") from None

    return render(
        request,
        "myapp/player_detail.html",
        {
            "profile": profile,
            "player": profile.detail,
            "can_edit_team": DjangoTeamPermissionQuery().can_manage(request.user, team_id),
        },
    )


@login_required
def player_edit(request, team_id, player_id):
    """選手の基本情報と成績を編集する。"""
    service = build_service()

    try:
        detail = service.get_player_detail(team_id, player_id)
    except (TeamNotFound, PlayerNotFound):
        raise Http404("選手が見つかりません。") from None

    if not DjangoTeamPermissionQuery().can_manage(request.user, team_id):
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
