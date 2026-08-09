import types

from django import forms
from django.contrib import admin

from .domain.entities import Stint as DomainStint
from .domain.exceptions import DomainError
from .domain.value_objects import JerseyNumber
from .infrastructure.orm_models import (
    Game,
    GameBattingLine,
    GamePitchingLine,
    League,
    Player,
    PlayerStint,
    Stadium,
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

    model_order = {
        'League': 1, 'Team': 2, 'Player': 3, 'PlayerStint': 4, 'Game': 5, 'Stadium': 6,
    }
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
        fields = ('display_order', 'name', 'home_stadium')
        widgets = {'display_order': forms.HiddenInput()}


class TeamInline(admin.TabularInline):
    """リーグに所属するチーム。行をドラッグして表示順を並べ替えられる。"""

    model = Team
    form = TeamInlineForm
    extra = 0
    fields = ('display_order', 'name', 'home_stadium')
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
    list_display = ('name', 'home_stadium', 'active_player_count', 'game_count')
    list_filter = ('league',)
    search_fields = ('name', 'home_stadium__name')
    autocomplete_fields = ('home_stadium',)
    # リーグごとにまとまるよう並べる。リーグ内は手動の表示順を尊重する
    ordering = ('league__name', 'display_order', 'name')
    list_select_related = ('league', 'home_stadium')

    # 行をリーグごとに区切る。見出しに出るのでリーグ列は list_display から外した
    group_by = staticmethod(lambda team: team.league.name)

    @admin.display(description='現役選手')
    def active_player_count(self, obj):
        # 在籍中＝退団年が空の在籍
        return obj.stints.filter(to_year__isnull=True).count()

    @admin.display(description='試合数')
    def game_count(self, obj):
        return obj.home_games.count() + obj.away_games.count()


@admin.register(Stadium)
class StadiumAdmin(admin.ModelAdmin):
    list_display = ('name', 'city', 'capacity', 'surface', 'opened_year', 'home_team_names')
    search_fields = ('name', 'city')
    list_filter = ('surface',)
    ordering = ('name',)

    @admin.display(description='本拠地とするチーム')
    def home_team_names(self, obj):
        names = list(obj.home_teams.values_list('name', flat=True))
        return '、'.join(names) if names else '—'


class PlayerStintForm(forms.ModelForm):
    """在籍の入力検証。

    管理画面はドメインを経由しないため、放っておくと「同じチームで同じ背番号の
    選手が同時に2人」といった状態を作れてしまう。判定そのものはドメインの
    Stint に任せ、ここでは既存の在籍と突き合わせるだけにする。
    """

    class Meta:
        model = PlayerStint
        fields = '__all__'

    def clean(self):
        cleaned = super().clean()
        team = cleaned.get('team')
        number = cleaned.get('number')
        from_year = cleaned.get('from_year')
        if not (team and number is not None and from_year is not None):
            return cleaned

        try:
            candidate = DomainStint(
                team_id=team.id,
                number=JerseyNumber(number),
                from_year=from_year,
                to_year=cleaned.get('to_year'),
            )
        except DomainError as error:
            raise forms.ValidationError(str(error)) from None

        others = PlayerStint.objects.filter(team=team, number=number).select_related('player')
        if self.instance.pk:
            others = others.exclude(pk=self.instance.pk)
        if cleaned.get('player'):
            others = others.exclude(player=cleaned['player'])

        for other in others:
            existing = DomainStint(
                team_id=other.team_id,
                number=JerseyNumber(other.number),
                from_year=other.from_year,
                to_year=other.to_year,
            )
            if candidate.overlaps(existing):
                raise forms.ValidationError(
                    f'背番号 {number} は {other.player.name} が {existing} に'
                    f'使用しています。期間が重なる同じ背番号は登録できません。'
                )
        return cleaned


class PlayerStintInline(admin.TabularInline):
    """在籍。所属と背番号はここが出典で、移籍すると行が増える。"""

    model = PlayerStint
    form = PlayerStintForm
    extra = 0
    fields = ('team', 'number', 'from_year', 'to_year')
    ordering = ('-from_year',)
    autocomplete_fields = ('team',)
    verbose_name_plural = '在籍（経歴）'


@admin.register(Player)
class PlayerAdmin(admin.ModelAdmin):
    list_display = ('name', 'position', 'current_team', 'current_number', 'appearances')
    list_filter = ('position', 'stints__team__league', 'stints__team')
    search_fields = ('name',)
    ordering = ('name',)
    inlines = [PlayerStintInline]

    fieldsets = (
        (None, {
            'fields': ('name', 'position'),
            'description': '所属チームと背番号は下の「在籍」で管理します。',
        }),
        ('プロフィール', {
            'description': '分かっているものだけ入力してください。すべて任意です。',
            'fields': (
                'birth_date', ('throws', 'bats'),
                ('height_cm', 'weight_kg'), 'birthplace', 'debut_year',
            ),
        }),
    )

    @admin.display(description='現在の所属')
    def current_team(self, obj):
        stint = self._current(obj)
        return stint.team.name if stint else '—'

    @admin.display(description='背番号')
    def current_number(self, obj):
        stint = self._current(obj)
        return stint.number if stint else '—'

    @admin.display(description='出場試合')
    def appearances(self, obj):
        """成績そのものは試合側にあるので、ここでは出場数だけ示す。"""
        count = obj.game_batting.count() + obj.game_pitching.count()
        return count or '—'

    @staticmethod
    def _current(obj):
        for stint in obj.stints.all():
            if stint.to_year is None:
                return stint
        return None

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related('stints__team')


@admin.register(PlayerStint)
class PlayerStintAdmin(admin.ModelAdmin):
    """在籍そのものの一覧。チームごとの名簿として使える。"""

    form = PlayerStintForm
    list_display = ('number', 'player', 'from_year', 'to_year')
    list_filter = ('team__league', 'team')
    search_fields = ('player__name',)
    ordering = ('team__league__name', 'team__name', 'number')
    list_select_related = ('player', 'team', 'team__league')
    autocomplete_fields = ('player', 'team')

    # 行をチームごとに区切る
    group_by = staticmethod(lambda s: f'{s.team.league.name} · {s.team.name}')


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
