"""ビュー。

責務は「HTTP を解釈してアプリケーションサービスを呼び、結果を描画する」ことだけ。
成績の計算も背番号の重複判定もここには無い（ドメイン層にある）。
"""

from django.contrib import messages
from django.contrib.auth.forms import UserCreationForm
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy
from django.views.generic import CreateView

from ..application.services import TeamApplicationService
from ..domain.exceptions import DomainError, PlayerNotFound, TeamNotFound
from ..domain.value_objects import BattingLine, InningsPitched, PitchingLine, Position
from ..infrastructure.queries import DjangoTeamListQuery
from ..infrastructure.repositories import DjangoTeamRepository
from .forms import BattingStatsForm, PitchingStatsForm, PlayerRegistrationForm, PlayerUpdateForm

BATTER_MODE = 'batter'
PITCHER_MODE = 'pitcher'


def _service() -> TeamApplicationService:
    """依存を組み立てる。差し替えたい場合はここだけ変えればよい。"""
    return TeamApplicationService(
        teams=DjangoTeamRepository(),
        team_list_query=DjangoTeamListQuery(),
    )


def dashboard(request):
    """ホーム画面。リーグ全体の概況と各種ランキングを表示する。"""
    return render(request, 'myapp/dashboard.html', {'board': _service().get_dashboard()})


def team_list(request):
    """チーム一覧。"""
    return render(request, 'myapp/team_list.html', {'teams': _service().list_teams()})


def player_list(request, team_id):
    """選手一覧。野手／投手モードを切り替えて表示する。"""
    service = _service()

    try:
        team_name = service.get_team_name(team_id)
    except TeamNotFound:
        raise Http404("チームが見つかりません。")

    form = PlayerRegistrationForm()

    if request.method == 'POST':
        form = PlayerRegistrationForm(request.POST)
        if form.is_valid():
            try:
                service.register_player(
                    team_id=team_id,
                    name=form.cleaned_data['name'],
                    number=form.cleaned_data['number'],
                    position_label=form.cleaned_data['position'],
                )
            except DomainError as error:
                messages.error(request, str(error))
            else:
                messages.success(
                    request, f"{form.cleaned_data['name']} 選手を登録しました。"
                )
                mode = (
                    PITCHER_MODE
                    if form.cleaned_data['position'] == Position.PITCHER.value
                    else BATTER_MODE
                )
                return redirect(f"{reverse('player_list', args=[team_id])}?pos={mode}")
        else:
            messages.error(request, _first_error(form))

    pos_mode = PITCHER_MODE if request.GET.get('pos') == PITCHER_MODE else BATTER_MODE
    players = (
        service.list_pitchers(team_id)
        if pos_mode == PITCHER_MODE
        else service.list_batters(team_id)
    )

    return render(request, 'myapp/player_list.html', {
        'team_id': team_id,
        'team_name': team_name,
        'players': players,
        'pos_mode': pos_mode,
        'form': form,
        'positions': Position.labels(),
    })


def player_edit(request, team_id, player_id):
    """選手の基本情報と成績を編集する。"""
    service = _service()

    try:
        detail = service.get_player_detail(team_id, player_id)
    except (TeamNotFound, PlayerNotFound):
        raise Http404("選手が見つかりません。")

    if request.method == 'POST':
        base_form = PlayerUpdateForm(request.POST)
        is_pitcher = request.POST.get('position') == Position.PITCHER.value
        stats_form = (
            PitchingStatsForm(request.POST) if is_pitcher else BattingStatsForm(request.POST)
        )

        if base_form.is_valid() and stats_form.is_valid():
            batting = pitching = None
            try:
                if is_pitcher:
                    counts = stats_form.cleaned_counts()
                    pitching = PitchingLine(
                        innings=InningsPitched.from_notation(counts.pop('innings_pitched')),
                        **counts,
                    )
                else:
                    batting = BattingLine(**stats_form.cleaned_counts())

                service.update_player(
                    team_id=team_id,
                    player_id=player_id,
                    name=base_form.cleaned_data['name'],
                    number=base_form.cleaned_data['number'],
                    position_label=base_form.cleaned_data['position'],
                    batting=batting,
                    pitching=pitching,
                )
            except DomainError as error:
                messages.error(request, str(error))
            else:
                messages.success(request, "選手情報を更新しました。")
                mode = PITCHER_MODE if is_pitcher else BATTER_MODE
                return redirect(
                    f"{reverse('player_list', args=[team_id])}?pos={mode}"
                )
        else:
            messages.error(request, _first_error(base_form) or _first_error(stats_form))

        # 入力エラー時は送信内容を保ったまま再表示する
        detail = service.get_player_detail(team_id, player_id)

    return render(request, 'myapp/player_edit.html', {
        'player': detail,
        'positions': Position.labels(),
    })


def _first_error(form) -> str:
    """フォームのエラーを画面表示用の1行にまとめる。"""
    for field, errors in form.errors.items():
        label = form.fields[field].label if field in form.fields else field
        return f"{label}: {errors[0]}"
    return ''


class SignUpView(CreateView):
    """新規ユーザー登録。登録後はログイン画面へ遷移する。"""

    form_class = UserCreationForm
    template_name = 'registration/signup.html'
    success_url = reverse_lazy('login')
