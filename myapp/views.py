from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import messages
from django.contrib.auth.forms import UserCreationForm
from .models import Team, Player, PlayerStats, PitcherStats  # PitcherStatsを追加
from .services import BaseballService
from django.core.exceptions import ValidationError
from django.db.models import F, ExpressionWrapper, FloatField, Case, When, Value
from django.db.models.functions import Floor
from django.urls import reverse_lazy
from django.views.generic import CreateView

def team_list(request):
    """全てのチームを表示する（ホーム画面）"""
    teams = Team.objects.all().select_related('league')
    return render(request, 'myapp/team_list.html', {'teams': teams})

def player_list(request, team_id):
    team = get_object_or_404(Team, id=team_id)
    
    # 1. 新規登録処理 (POST)
    if request.method == 'POST':
        name = request.POST.get('name')
        number = request.POST.get('number')
        position = request.POST.get('position')
        
        try:
            # 選手の基本情報を保存（背番号の重複チェックを含む）
            new_player = BaseballService.add_player_to_team(team.id, name, number, position)

            # ポジションに応じて初期成績レコードを作成
            if position == '投手':
                PitcherStats.objects.create(player=new_player)
            else:
                PlayerStats.objects.create(player=new_player)

            messages.success(request, f"{name} 選手を登録しました。")
            return redirect(f"{request.path}?pos={'pitcher' if position == '投手' else 'batter'}")
        except ValidationError as e:
            messages.error(request, e.message)
        except (ValueError, TypeError):
            messages.error(request, "背番号は数値で入力してください。")

    # 2. 表示モードの取得とデータ取得
    pos_mode = request.GET.get('pos', 'batter')
    base_players = Player.objects.filter(team=team, is_active=True)

    if pos_mode == 'pitcher':
        # 1. まず「合計アウト数」を計算し、それを使って各指標を計算する
        players = base_players.filter(position='投手').annotate(
            # 合計アウト数を計算 (5.2 -> 17)
            total_outs_val=ExpressionWrapper(
                Floor(F('pitcher_stats__innings_pitched')) * 3 +
                (F('pitcher_stats__innings_pitched') * 10 % 10),
                output_field=FloatField()
            )
        ).annotate(
            # 2. 上で計算した total_outs_val を使って各指標を出す
            era=Case(
                When(total_outs_val__gt=0,
                    then=ExpressionWrapper(F('pitcher_stats__earned_runs') * 27.0 / F('total_outs_val'), output_field=FloatField())),
                default=Value(0.0),
                output_field=FloatField(),
            ),
            k_9=Case(
                When(total_outs_val__gt=0,
                    then=ExpressionWrapper(F('pitcher_stats__strikeouts') * 27.0 / F('total_outs_val'), output_field=FloatField())),
                default=Value(0.0),
                output_field=FloatField(),
            ),
            whip=Case(
                When(total_outs_val__gt=0,
                    then=ExpressionWrapper((F('pitcher_stats__hits_allowed') + F('pitcher_stats__walks_allowed')) * 3.0 / F('total_outs_val'), output_field=FloatField())),
                default=Value(0.0),
                output_field=FloatField(),
            )
        ).order_by('era')
    else:
        # 野手モード
        players = base_players.exclude(position='投手').annotate(
            # 合計安打数を計算
            total_hits = F('stats__singles') + F('stats__doubles') + F('stats__triples') + F('stats__home_runs'),
        ).annotate(
            # 合計安打(total_hits)を使って出塁率(OBP)を計算
            obp = Case(
                When(stats__at_bats__gt=0, 
                    then=ExpressionWrapper(
                        (F('total_hits') + F('stats__walks') + F('stats__hit_by_pitch')) * 1.0 / 
                        (F('stats__at_bats') + F('stats__walks') + F('stats__hit_by_pitch') + F('stats__sacrifice_flies')), 
                        output_field=FloatField())),
                default=Value(0.0),
            ),
            # 長打率 (SLG): (単打 + 2*二塁打 + 3*三塁打 + 4*本塁打) / 打数
            slg = Case(
                When(stats__at_bats__gt=0, 
                    then=ExpressionWrapper(
                        (F('stats__singles') + F('stats__doubles') * 2 + F('stats__triples') * 3 + F('stats__home_runs') * 4) * 1.0 / 
                        F('stats__at_bats'), 
                        output_field=FloatField())),
                default=Value(0.0),
            )
        ).annotate(
            ops = F('obp') + F('slg'),
            calculated_avg = Case(
                When(stats__at_bats__gt=0, 
                        then=ExpressionWrapper(F('total_hits') * 1.0 / F('stats__at_bats'), output_field=FloatField())),
                default=Value(0.0),
            )
        ).order_by('-ops', '-calculated_avg')

    return render(request, 'myapp/player_list.html', {
        'team': team,
        'players': players,
        'pos_mode': pos_mode,
    })

def player_edit(request, player_id):
    player = get_object_or_404(Player, id=player_id)
    
    if request.method == 'POST':
        name = request.POST.get('name')
        number = request.POST.get('number')
        position = request.POST.get('position')
        
        try:
            # 選手基本情報の更新
            BaseballService.update_player(player_id, name, number, position)
            
            if position == '投手':
                # 投手用データの保存（ここに必要な項目をすべて追加します）
                PitcherStats.objects.update_or_create(
                    player=player,
                    defaults={
                        'innings_pitched': request.POST.get('innings_pitched', 0),
                        'earned_runs': request.POST.get('earned_runs', 0),
                        'wins': request.POST.get('wins', 0),
                        'strikeouts': request.POST.get('strikeouts', 0),    # これを追加！
                        'hits_allowed': request.POST.get('hits_allowed', 0), # これを追加！
                        'walks_allowed': request.POST.get('walks_allowed', 0), # これを追加！
                    }
                )
            else:
                # 野手用データの保存
                PlayerStats.objects.update_or_create(
                    player=player,
                    defaults={
                        'at_bats': request.POST.get('at_bats', 0),
                        'singles': request.POST.get('singles', 0),
                        'doubles': request.POST.get('doubles', 0), # 追加
                        'triples': request.POST.get('triples', 0), # 追加
                        'home_runs': request.POST.get('home_runs', 0),
                        'runs_batted_in': request.POST.get('rbi', 0),
                        'walks': request.POST.get('walks', 0), # 追加
                        'hit_by_pitch': request.POST.get('hit_by_pitch', 0), # 追加
                        'sacrifice_flies': request.POST.get('sacrifice_flies', 0), # 追加
                    }
                )
            messages.success(request, f"選手情報を更新しました。")
            target_pos = 'pitcher' if position == '投手' else 'batter'
            return redirect(f"/team/{player.team.id}/?pos={target_pos}")

        except ValidationError as e:
            messages.error(request, e.message)

    return render(request, 'myapp/player_edit.html', {'player': player})


class SignUpView(CreateView):
    """新規ユーザー登録。登録後はログイン画面へ遷移する。"""
    form_class = UserCreationForm
    template_name = 'registration/signup.html'
    success_url = reverse_lazy('login')
