from django.contrib import admin

from .domain.value_objects import BattingLine, InningsPitched, PitchingLine
from .infrastructure.orm_models import (
    League,
    PitcherStats,
    Player,
    PlayerStats,
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


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = ('name', 'league', 'city', 'active_player_count')
    list_filter = ('league',)
    search_fields = ('name', 'city')
    ordering = ('league', 'name')
    list_select_related = ('league',)

    @admin.display(description='現役選手')
    def active_player_count(self, obj):
        return obj.players.filter(is_active=True).count()


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


@admin.register(PlayerStats)
class PlayerStatsAdmin(admin.ModelAdmin):
    list_display = ('player', 'at_bats', 'singles', 'doubles', 'triples', 'home_runs', 'runs_batted_in')
    search_fields = ('player__name',)
    list_select_related = ('player',)


@admin.register(PitcherStats)
class PitcherStatsAdmin(admin.ModelAdmin):
    list_display = ('player', 'innings_pitched', 'wins', 'losses', 'saves', 'earned_runs', 'strikeouts')
    search_fields = ('player__name',)
    list_select_related = ('player',)
