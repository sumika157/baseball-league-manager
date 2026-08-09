import types

from django import forms
from django.contrib import admin

from .infrastructure.orm_models import (
    Game,
    GameBattingLine,
    GamePitchingLine,
    League,
    Player,
    Team,
)

# ヘッダーの文言
admin.site.site_header = 'Baseball Manager 管理'
admin.site.site_title = 'Baseball Manager'
admin.site.index_title = 'データ管理'

# サイト側で registration/password_change_*.html を上書きしているため、
# 同名テンプレートを使う管理画面までサイトの見た目になってしまう。
# 管理画面には管理画面の体裁を保たせる。
admin.site.password_change_template = 'admin/password_change_form.html'
admin.site.password_change_done_template = 'admin/password_change_done.html'

# トップに概況を足したテンプレート。index.html という名前にすると自分自身を
# 継承する形になって成立しないため、別名にして明示指定している。
admin.site.index_template = 'admin/dashboard_index.html'


def _ordered_app_list(self, request, app_label=None):
    """トップの並びを業務の重要度とドメインの階層に揃える。

    Django の既定は表示名の五十音／アルファベット順のため、
    「認証と認可」が先に来て、モデルもリーグ・チーム・選手が入り混じる。
    主に扱うのは野球データなので、そちらを先頭に置く。
    """
    app_list = _original_get_app_list(request, app_label)

    model_order = {'League': 1, 'Team': 2, 'Player': 3, 'Game': 4}
    for app in app_list:
        if app.get('app_label') == 'myapp':
            app['models'].sort(key=lambda m: model_order.get(m.get('object_name'), 99))

    # 野球データを先頭、認証を後ろに
    app_list.sort(key=lambda a: 0 if a.get('app_label') == 'myapp' else 1)
    return app_list


_original_get_app_list = admin.site.get_app_list
admin.site.get_app_list = types.MethodType(_ordered_app_list, admin.site)


class TeamInlineForm(forms.ModelForm):
    """表示順は画面に出さない。

    数値そのものに意味は無く、ドラッグした結果が入るだけなので、
    列として見せると読み手を混乱させる。ただし送信は必要なので
    hidden で残す（JavaScript もこの入力欄を目印に行を見つける）。
    """

    class Meta:
        model = Team
        fields = ('display_order', 'name', 'city')
        widgets = {'display_order': forms.HiddenInput()}


class TeamInline(admin.TabularInline):
    """リーグに所属するチーム。行をドラッグして表示順を並べ替えられる。"""

    model = Team
    form = TeamInlineForm
    extra = 0
    fields = ('display_order', 'name', 'city')
    ordering = ('display_order', 'name')
    show_change_link = True


@admin.register(League)
class LeagueAdmin(admin.ModelAdmin):
    list_display = ('name', 'team_count', 'created_at')
    search_fields = ('name',)
    ordering = ('name',)
    inlines = [TeamInline]

    class Media:
        js = ('myapp/js/admin-inline-sortable.js',)
        css = {'all': ('myapp/css/admin-theme.css',)}

    @admin.display(description='チーム数')
    def team_count(self, obj):
        return obj.teams.count()


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = ('name', 'league', 'city', 'active_player_count', 'game_count')
    list_filter = ('league',)
    search_fields = ('name', 'city')
    ordering = ('league', 'name')
    list_select_related = ('league',)

    @admin.display(description='現役選手')
    def active_player_count(self, obj):
        return obj.players.filter(is_active=True).count()

    @admin.display(description='試合数')
    def game_count(self, obj):
        return obj.home_games.count() + obj.away_games.count()


@admin.register(Player)
class PlayerAdmin(admin.ModelAdmin):
    list_display = ('number', 'name', 'team', 'position', 'is_active', 'appearances')
    list_display_links = ('name',)
    list_filter = ('team__league', 'team', 'position', 'is_active')
    search_fields = ('name',)
    ordering = ('team', 'number')
    list_select_related = ('team',)
    list_editable = ('is_active',)

    @admin.display(description='出場試合')
    def appearances(self, obj):
        """成績そのものは試合側にあるので、ここでは出場数だけ示す。"""
        count = obj.game_batting.count() + obj.game_pitching.count()
        return count or '—'

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('team')


class GameBattingLineInline(admin.TabularInline):
    """試合ごとの打撃成績。"""

    model = GameBattingLine
    extra = 0
    autocomplete_fields = ('player',)


class GamePitchingLineInline(admin.TabularInline):
    """試合ごとの投球成績。"""

    model = GamePitchingLine
    extra = 0
    autocomplete_fields = ('player',)


@admin.register(Game)
class GameAdmin(admin.ModelAdmin):
    """試合。チームの勝敗も選手の通算成績も、すべてここから集計される。"""

    list_display = ('played_on', 'year', 'matchup', 'score', 'result')
    list_filter = ('year', 'home_team__league')
    date_hierarchy = 'played_on'
    ordering = ('-played_on',)
    list_select_related = ('home_team', 'away_team')
    inlines = [GameBattingLineInline, GamePitchingLineInline]

    @admin.display(description='対戦')
    def matchup(self, obj):
        return f'{obj.home_team.name} vs {obj.away_team.name}'

    @admin.display(description='スコア')
    def score(self, obj):
        return f'{obj.home_score} - {obj.away_score}'

    @admin.display(description='結果')
    def result(self, obj):
        if obj.home_score == obj.away_score:
            return '引分'
        winner = obj.home_team if obj.home_score > obj.away_score else obj.away_team
        return f'{winner.name} の勝ち'
