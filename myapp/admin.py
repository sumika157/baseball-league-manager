import types

from django.contrib import admin

from .domain.value_objects import BattingLine, InningsPitched, PitchingLine
from .infrastructure.orm_models import (
    League,
    PitcherStats,
    Player,
    PlayerStats,
    Team,
    TeamSeasonRecord,
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

    model_order = {'League': 1, 'Team': 2, 'Player': 3}
    for app in app_list:
        if app.get('app_label') == 'myapp':
            app['models'].sort(key=lambda m: model_order.get(m.get('object_name'), 99))

    # 野球データを先頭、認証を後ろに
    app_list.sort(key=lambda a: 0 if a.get('app_label') == 'myapp' else 1)
    return app_list


_original_get_app_list = admin.site.get_app_list
admin.site.get_app_list = types.MethodType(_ordered_app_list, admin.site)


class TeamInline(admin.TabularInline):
    model = Team
    extra = 0
    fields = ('name', 'city')
    show_change_link = True


@admin.register(League)
class LeagueAdmin(admin.ModelAdmin):
    list_display = ('name', 'team_count', 'created_at')
    search_fields = ('name',)
    ordering = ('name',)
    inlines = [TeamInline]

    @admin.display(description='チーム数')
    def team_count(self, obj):
        return obj.teams.count()


class TeamSeasonRecordInline(admin.TabularInline):
    """シーズン成績。チームの編集画面から年ごとに登録する。

    順位は保持しない。勝率から算出するため、入力すると勝敗と矛盾しうる。
    """

    model = TeamSeasonRecord
    extra = 1
    fields = ('year', 'wins', 'losses', 'ties')
    ordering = ('-year',)


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = ('name', 'league', 'city', 'active_player_count', 'latest_season')
    list_filter = ('league',)
    search_fields = ('name', 'city')
    ordering = ('league', 'name')
    list_select_related = ('league',)
    inlines = [TeamSeasonRecordInline]

    @admin.display(description='現役選手')
    def active_player_count(self, obj):
        return obj.players.filter(is_active=True).count()

    @admin.display(description='最新シーズン')
    def latest_season(self, obj):
        row = obj.season_records.order_by('-year').first()
        if row is None:
            return '—'
        return f'{row.year}年 {row.wins}勝{row.losses}敗{row.ties}分'


class PlayerStatsInline(admin.StackedInline):
    """打撃成績。選手と 1 対 1 なので、選手の編集画面から直接扱えるようにする。"""

    model = PlayerStats
    can_delete = False
    extra = 0
    verbose_name_plural = '打撃成績'


class PitcherStatsInline(admin.StackedInline):
    """投球成績。"""

    model = PitcherStats
    can_delete = False
    extra = 0
    verbose_name_plural = '投球成績'


@admin.register(Player)
class PlayerAdmin(admin.ModelAdmin):
    list_display = ('number', 'name', 'team', 'position', 'is_active', 'key_stat')
    list_display_links = ('name',)
    list_filter = ('team__league', 'team', 'position', 'is_active')
    search_fields = ('name',)
    ordering = ('team', 'number')
    list_select_related = ('team',)
    list_editable = ('is_active',)
    inlines = [PlayerStatsInline, PitcherStatsInline]

    @admin.display(description='主要成績')
    def key_stat(self, obj):
        """一覧でも成績の当たりが付くように、代表的な指標を1つ出す。

        計算はドメイン層に委ねる。ここで式を書くと管理画面だけ別の定義になり、
        画面ごとに数値が食い違う原因になる。
        """
        if obj.position == '投手':
            stats = getattr(obj, 'pitcher_stats', None)
            if stats is None:
                return '—'
            line = PitchingLine(
                innings=InningsPitched.from_notation(stats.innings_pitched),
                earned_runs=stats.earned_runs,
            )
            if line.innings.outs == 0:
                return '未登板'
            return f'防御率 {line.earned_run_average:.2f}'

        stats = getattr(obj, 'stats', None)
        if stats is None:
            return '—'
        line = BattingLine(
            at_bats=stats.at_bats,
            singles=stats.singles,
            doubles=stats.doubles,
            triples=stats.triples,
            home_runs=stats.home_runs,
            walks=stats.walks,
            hit_by_pitch=stats.hit_by_pitch,
            sacrifice_flies=stats.sacrifice_flies,
        )
        if line.at_bats == 0:
            return '未出場'
        return f'OPS {line.ops:.3f}'

    def get_queryset(self, request):
        # 一覧で成績を出すため、関連を先読みして N+1 を避ける
        return (
            super().get_queryset(request)
            .select_related('team', 'stats', 'pitcher_stats')
        )


class HiddenFromIndexMixin:
    """トップの一覧には出さないが、URL からは開けるようにする。

    打撃成績・投球成績は選手と 1 対 1 で、選手の編集画面からインラインで扱える。
    トップに並べると項目が増えるうえ、選手を介さず成績だけを作れてしまい
    「選手のいない成績」が生まれる余地ができるため、導線としては出さない。
    """

    def get_model_perms(self, request):
        return {}


@admin.register(PlayerStats)
class PlayerStatsAdmin(HiddenFromIndexMixin, admin.ModelAdmin):
    list_display = ('player', 'at_bats', 'singles', 'doubles', 'triples', 'home_runs', 'runs_batted_in')
    search_fields = ('player__name',)
    list_select_related = ('player',)


@admin.register(PitcherStats)
class PitcherStatsAdmin(HiddenFromIndexMixin, admin.ModelAdmin):
    list_display = ('player', 'innings_pitched', 'wins', 'losses', 'saves', 'earned_runs', 'strikeouts')
    search_fields = ('player__name',)
    list_select_related = ('player',)
