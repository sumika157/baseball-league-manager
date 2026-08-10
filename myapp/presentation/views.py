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
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy
from django.views.generic import CreateView

from ..application.services import TeamApplicationService
from ..domain.exceptions import (
    DomainError,
    GameNotFound,
    LeagueNotFound,
    PlayerNotFound,
    TeamNotFound,
)
from ..domain.value_objects import (
    BattingLine,
    FieldingPosition,
    InningsPitched,
    LineScore,
    PitchingLine,
    Position,
)
from ..infrastructure.queries import (
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
    MAX_INNINGS,
    BattingEntryForm,
    BattingEntryFormSet,
    GameForm,
    InningScoreFormSet,
    PitchingEntryForm,
    PitchingEntryFormSet,
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


def _service() -> TeamApplicationService:
    """依存を組み立てる。差し替えたい場合はここだけ変えればよい。"""
    return TeamApplicationService(
        teams=DjangoTeamRepository(),
        team_list_query=DjangoTeamListQuery(),
        games=DjangoGameRepository(),
        leagues=DjangoLeagueRepository(),
    )


def dashboard(request):
    """ホーム画面。リーグ全体の概況と各種ランキングを表示する。"""
    return render(request, "myapp/dashboard.html", {"board": _service().get_dashboard()})


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
    listing = _service().list_teams_by_league(sort=sort, descending=descending)
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
        board = _service().get_standings(year, sort=sort, descending=descending)
    except DomainError as error:
        raise Http404(str(error))

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
    service = _service()

    try:
        team_name = service.get_team_name(team_id)
    except TeamNotFound:
        raise Http404("チームが見つかりません。")

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
        detail = _service().get_league_detail(league_id, year)
    except LeagueNotFound:
        raise Http404("リーグが見つかりません。")

    return render(request, "myapp/league_detail.html", {"league": detail})


def league_titles(request, league_id, year=None):
    """リーグのタイトル一覧。部門別の上位者をシーズンで区切って並べる。"""
    try:
        titles = _service().get_league_titles(league_id, year)
    except LeagueNotFound:
        raise Http404("リーグが見つかりません。")
    except DomainError as error:
        raise Http404(str(error))

    return render(request, "myapp/league_titles.html", {"titles": titles})


def game_list(request):
    """試合一覧。年とチームで絞り込める。"""
    service = _service()

    def _int(name):
        value = request.GET.get(name)
        return int(value) if value and value.isdigit() else None

    year, team_id = _int("year"), _int("team")
    listing = service.list_games(year=year, team_id=team_id)
    teams = service.list_teams().rows

    return render(
        request,
        "myapp/game_list.html",
        {
            "games": listing.rows,
            "years": service.list_game_seasons(),
            "teams": teams,
            "selected_year": year,
            "selected_team": team_id,
            # 担当チームが1つも無ければ、押しても弾かれるだけの登録導線は見せない
            "can_create_game": DjangoTeamPermissionQuery().can_manage_any(request.user, [t.id for t in teams]),
        },
    )


@login_required
def game_create(request):
    """試合を作る。作成後、成績の入力画面へ進む。"""
    service = _service()
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
def game_edit(request, game_id):
    """試合の基本情報と、両チームのロスターぶんの成績を一度に入力する。"""
    service = _service()

    try:
        data = service.get_game_edit_data(game_id)
    except GameNotFound:
        raise Http404("試合が見つかりません。")

    game, rosters = data["game"], data["rosters"]
    if not DjangoTeamPermissionQuery().can_manage_any(request.user, (game.home_team_id, game.away_team_id)):
        raise PermissionDenied("このチームを編集する権限がありません。")

    batters, pitchers = _split_roster(rosters)

    if request.method == "POST":
        form = GameForm(request.POST)
        batting_formset = BattingEntryFormSet(request.POST, prefix="batting")
        pitching_formset = PitchingEntryFormSet(request.POST, prefix="pitching")
        inning_formset = InningScoreFormSet(request.POST, prefix="innings")

        if (
            form.is_valid()
            and batting_formset.is_valid()
            and pitching_formset.is_valid()
            and inning_formset.is_valid()
        ):
            try:
                service.update_game(
                    game_id,
                    year=form.cleaned_data["year"],
                    played_on=form.cleaned_data["played_on"],
                    home_team_id=form.cleaned_data["home_team"],
                    away_team_id=form.cleaned_data["away_team"],
                    home_score=form.cleaned_data["home_score"],
                    away_score=form.cleaned_data["away_score"],
                    batting=_collect_batting(batting_formset),
                    pitching=_collect_pitching(pitching_formset),
                    lineup=_collect_lineup(batting_formset),
                    staff=_collect_staff(pitching_formset),
                    line_score=_collect_line_score(inning_formset),
                )
            except DomainError as error:
                messages.error(request, str(error))
            else:
                messages.success(request, "試合の記録を保存しました。")
                return redirect(reverse("game_detail", args=[game_id]))
        else:
            messages.error(
                request,
                _first_error(form)
                or _first_formset_error(inning_formset)
                or _first_formset_error(batting_formset)
                or _first_formset_error(pitching_formset),
            )
    else:
        form = GameForm(
            initial={
                "year": game.season.year,
                "played_on": game.played_on,
                "home_team": game.home_team_id,
                "away_team": game.away_team_id,
                "home_score": game.home_score,
                "away_score": game.away_score,
            }
        )
        batting_formset = BattingEntryFormSet(prefix="batting", initial=[_batting_initial(p) for p in batters])
        pitching_formset = PitchingEntryFormSet(prefix="pitching", initial=[_pitching_initial(p) for p in pitchers])
        inning_formset = InningScoreFormSet(prefix="innings", initial=_inning_initial(game.line_score))

    return render(
        request,
        "myapp/game_edit.html",
        {
            "game": game,
            "form": form,
            "rosters": rosters,
            # フォームと選手情報を対にして渡す。テンプレート側で添字を扱わずに済ませる
            "batting_rows": list(zip(batting_formset.forms, batters)),
            "pitching_rows": list(zip(pitching_formset.forms, pitchers)),
            "batting_formset": batting_formset,
            "pitching_formset": pitching_formset,
            "inning_formset": inning_formset,
            "positions": FieldingPosition.labels(),
        },
    )


def _inning_initial(line_score):
    """イニングスコアの初期値。記録されている回まで埋め、残りは空欄で用意する。"""
    return [
        {
            "inning": inning,
            "away": (line_score.runs_in(inning, home=False) if inning <= len(line_score.away) else None),
            "home": (line_score.runs_in(inning, home=True) if inning <= len(line_score.home) else None),
        }
        for inning in range(1, MAX_INNINGS + 1)
    ]


def _collect_line_score(formset) -> LineScore:
    """入力された回ごとの得点を組み立てる。

    後ろの空欄は「行われていない回」として切り落とす。途中の空欄は 0 とみなす
    （1回に得点が無ければ空欄のままにする人もいるため）。
    """
    away, home = [], []
    for form in formset:
        if form.is_blank():
            continue
        away.append(form.cleaned_data.get("away") or 0)
        home.append(form.cleaned_data.get("home") or 0)

    # ホームが最終回を攻めていない（裏が空欄）場合は、その回を落とす
    if home and formset.forms:
        last = len(away)
        if last and formset.forms[last - 1].cleaned_data.get("home") is None:
            home = home[:-1]
    return LineScore(away=tuple(away), home=tuple(home))


def _collect_lineup(formset) -> dict:
    """打順・交代の順・守備位置を {選手id: (打順, 交代の順, 守備位置)} で返す。"""
    return {form.cleaned_data["player_id"]: form.lineup() for form in formset if not form.is_blank()}


def _collect_staff(formset) -> dict:
    """登板した回を {選手id: 回} で返す。"""
    return {form.cleaned_data["player_id"]: form.entered() for form in formset if not form.is_blank()}


def _split_roster(rosters):
    """両チームのロスターを、野手と投手に分ける。並びは画面と保存で共通。"""
    batters, pitchers = [], []
    for roster in rosters:
        for player in roster["players"]:
            entry = dict(player, team_name=roster["team_name"])
            (pitchers if player["is_pitcher"] else batters).append(entry)
    return batters, pitchers


def _batting_initial(player):
    line = player["batting"]
    initial = {"player_id": player["id"]}
    if line is not None:
        initial.update({f: getattr(line, f) for f in BattingEntryForm.STAT_FIELDS})
    lineup = player.get("lineup")
    if lineup is not None:
        order, sequence, position = lineup
        initial.update(
            {
                "batting_order": order,
                "slot_sequence": sequence,
                "fielding_position": position.value if position else "",
            }
        )
    return initial


def _pitching_initial(player):
    line = player["pitching"]
    initial = {"player_id": player["id"]}
    if line is not None:
        initial["innings_pitched"] = line.innings.to_notation()
        initial.update({f: getattr(line, f) for f in PitchingEntryForm.COUNT_FIELDS})
    if player.get("entered_inning") is not None:
        initial["entered_inning"] = player["entered_inning"]
    return initial


def _collect_batting(formset) -> dict:
    """未入力の行は含めない。含めると出場していない選手の記録が残る。"""
    result = {}
    for form in formset:
        if form.is_blank():
            continue
        result[form.cleaned_data["player_id"]] = BattingLine(**form.counts())
    return result


def _collect_pitching(formset) -> dict:
    result = {}
    for form in formset:
        if form.is_blank():
            continue
        result[form.cleaned_data["player_id"]] = PitchingLine(
            innings=InningsPitched.from_notation(form.innings()), **form.counts()
        )
    return result


def _first_formset_error(formset) -> str:
    for form in formset:
        message = _first_error(form)
        if message:
            return message
    return ""


def game_detail(request, game_id):
    """試合詳細。その試合の出場選手の成績を並べる。"""
    try:
        detail = _service().get_game_detail(game_id)
    except GameNotFound:
        raise Http404("試合が見つかりません。")

    can_edit = DjangoTeamPermissionQuery().can_manage_any(
        request.user, (detail.game.home_team_id, detail.game.away_team_id)
    )
    return render(request, "myapp/game_detail.html", {"detail": detail, "can_edit": can_edit})


def player_detail(request, team_id, player_id):
    """選手の個人ページ。通算成績と試合ごとの記録。"""
    try:
        profile = _service().get_player_profile(team_id, player_id)
    except (TeamNotFound, PlayerNotFound):
        raise Http404("選手が見つかりません。")

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
    service = _service()

    try:
        detail = service.get_player_detail(team_id, player_id)
    except (TeamNotFound, PlayerNotFound):
        raise Http404("選手が見つかりません。")

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
