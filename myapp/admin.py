import types

from django import forms
from django.contrib import admin
from django.contrib.admin.views.main import ORDER_VAR, ChangeList
from django.contrib.admin.widgets import FilteredSelectMultiple
from django.db.models import Count, IntegerField, OuterRef, Prefetch, Q, Subquery
from django.db.models.functions import Coalesce
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils.html import format_html, format_html_join

from .domain.entities import Stint as DomainStint
from .domain.exceptions import DomainError
from .domain.value_objects import JerseyNumber, StadiumProfile
from .domain.value_objects import Profile as DomainProfile
from .infrastructure import orm_models
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


class GroupedChangeList(ChangeList):
    """区切りを保ったまま並べ替える一覧。

    Django の既定では列を押すと並びがその列だけで決まり、まとまりが崩れて
    全行が混ざる。まとまりの順序を常に先に置き、利用者が指定した並びは
    その中で効かせる。

    手動で並べ替える一覧では使えない（ManualOrderAdminMixin を使う）。
    """

    def get_ordering(self, request, queryset):
        ordering = super().get_ordering(request, queryset)
        prefix = list(getattr(self.model_admin, 'group_ordering', ()))
        if not prefix:
            return ordering

        fixed = {field.lstrip('-') for field in prefix}
        return prefix + [f for f in ordering if f.lstrip('-') not in fixed]


class GroupedAdminMixin:
    """group_by で区切る一覧に、区切りを保つ並べ替えを組み込む。"""

    def get_changelist(self, request, **kwargs):
        return GroupedChangeList


class ManualOrderAdminMixin:
    """行をドラッグして並べ替える一覧。列での並べ替えは持たない。

    ドラッグした順がそのまま保存される順なので、列で並べ替えると
    見えている順と保存される順が食い違い、ドラッグを止めるほかなくなる。
    2つの並べ方は両立しないため、この一覧では手動の順だけを扱う。
    """

    # 列見出しの並べ替えリンクを出さない
    sortable_by = ()

    def changelist_view(self, request, extra_context=None):
        """URL に並べ替えが残っていたら落として開き直す。

        古いリンクや履歴から入っても、並べ替えられない状態に迷い込ませない。
        無視するだけでは絞り込みのリンクなどに紛れて残ってしまうため、
        URL そのものから消す。絞り込みなど他の指定は保つ。
        """
        if request.method == 'GET' and ORDER_VAR in request.GET:
            params = request.GET.copy()
            del params[ORDER_VAR]
            query = params.urlencode()
            return HttpResponseRedirect(
                f'{request.path}?{query}' if query else request.path
            )
        return super().changelist_view(request, extra_context)


def _grouped_count(queryset, group_field):
    """親1件ぶんの件数を返す副問い合わせを組み立てる。

    一覧の各行で数えると行数だけ問い合わせが増えるため、
    annotate と組み合わせて1回のSQLにまとめるために使う。
    """
    return (
        queryset.order_by()
        .values(group_field)
        .annotate(n=Count('pk'))
        .values('n')[:1]
    )


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
class LeagueAdmin(ManualOrderAdminMixin, admin.ModelAdmin):
    # display_order は行をドラッグすると書き換わる。数値そのものに意味は無いが、
    # JavaScript が動かない環境でも直接入力できるよう残してある
    list_display = ('name', 'display_order', 'teams_accordion', 'created_at')
    list_display_links = ('name',)
    list_editable = ('display_order',)
    search_fields = ('name',)
    # 手動の並び順を既定にする。未設定どうしは名前で安定させる
    ordering = ('display_order', 'name')
    inlines = [TeamInline]

    class Media:
        js = ('myapp/js/admin-inline-sortable.js',)
        css = {'all': ('myapp/css/admin-theme.css',)}

    # 表示順の列は隠してあるので、操作方法を画面上で伝える
    change_list_template = 'admin/sortable_change_list.html'

    def get_queryset(self, request):
        """所属チームを先読みする。行ごとに引くと一覧で N+1 になる。"""
        teams = orm_models.Team.objects.annotate(
            active_players=Count('stints', filter=Q(stints__to_year__isnull=True))
        ).order_by('display_order', 'name')
        return super().get_queryset(request).prefetch_related(
            Prefetch('teams', queryset=teams)
        )

    @admin.display(description='所属チーム')
    def teams_accordion(self, obj):
        """所属チームを折りたたんで表示する。

        details 要素を使えば JavaScript なしで開閉できる。行数の多い一覧で
        全チームを常に出すと縦に長くなるため、既定では畳んでおく。
        """
        teams = list(obj.teams.all())
        if not teams:
            return '—'

        items = format_html_join(
            '', '<li><a href="{}">{}</a><span class="team-accordion-meta">{}名</span></li>',
            (
                (
                    reverse('admin:myapp_team_change', args=[team.id]),
                    team.name,
                    team.active_players,
                )
                for team in teams
            ),
        )
        return format_html(
            '<details class="team-accordion">'
            '<summary>{}チーム</summary><ul>{}</ul></details>',
            len(teams), items,
        )


@admin.register(Team)
class TeamAdmin(ManualOrderAdminMixin, admin.ModelAdmin):
    # display_order は行をドラッグすると書き換わる。リーグ編集画面からだけでなく
    # この一覧でも並べ替えられるようにしてある
    list_display = (
        'name', 'display_order', 'home_stadium', 'active_player_count', 'game_count'
    )
    list_display_links = ('name',)
    list_editable = ('display_order',)
    list_filter = ('league',)
    search_fields = ('name', 'home_stadium__name')
    autocomplete_fields = ('home_stadium',)
    # リーグごとにまとまるよう並べる。リーグ内は手動の表示順を尊重する。
    # この並びが区切り表示の前提（同じリーグの行が続いていないと見出しが何度も出る）
    ordering = ('league__display_order', 'league__name', 'display_order', 'name')
    list_select_related = ('league', 'home_stadium')

    class Media:
        js = ('myapp/js/admin-inline-sortable.js',)
        css = {'all': ('myapp/css/admin-theme.css',)}

    # 表示順の列は隠してあるので、操作方法を画面上で伝える
    change_list_template = 'admin/sortable_change_list.html'

    def get_queryset(self, request):
        """一覧に出す件数を、すべて副問い合わせで先に数えておく。

        行ごとに数えると表示する行数だけ問い合わせが増える。
        """
        return super().get_queryset(request).annotate(
            league_team_count=Subquery(
                orm_models.League.objects
                .filter(pk=OuterRef('league_id'))
                .annotate(n=Count('teams')).values('n')[:1]
            ),
            active_players_count=Coalesce(Subquery(
                _grouped_count(
                    orm_models.PlayerStint.objects.filter(
                        team=OuterRef('pk'), to_year__isnull=True
                    ),
                    'team',
                ),
                output_field=IntegerField(),
            ), 0),
            played_games_count=(
                Coalesce(Subquery(
                    _grouped_count(
                        orm_models.Game.objects.filter(home_team=OuterRef('pk')),
                        'home_team',
                    ),
                    output_field=IntegerField(),
                ), 0)
                + Coalesce(Subquery(
                    _grouped_count(
                        orm_models.Game.objects.filter(away_team=OuterRef('pk')),
                        'away_team',
                    ),
                    output_field=IntegerField(),
                ), 0)
            ),
        )

    # 行をリーグごとに区切る。見出しに出るのでリーグ列は list_display から外した。
    # 表示順はリーグの中でしか意味が無いため、ドラッグも区切りをまたげない。
    # 区切りが成立するのは ordering がリーグ単位でまとめているから
    group_by = staticmethod(
        lambda team: f'{team.league.name}（{team.league_team_count}チーム）'
    )

    @admin.display(description='現役選手', ordering='active_players_count')
    def active_player_count(self, obj):
        # 在籍中＝退団年が空の在籍。件数は get_queryset で数えてある
        return obj.active_players_count

    @admin.display(description='試合数', ordering='played_games_count')
    def game_count(self, obj):
        return obj.played_games_count


class DomainCheckedForm(forms.ModelForm):
    """入力をドメインの値オブジェクトに検証させる ModelForm。

    管理画面はドメインを経由しないため、放っておくと画面からだけ
    現実的でない値（身長400cm、開場年1800年など）を保存できてしまう。
    判定は値オブジェクトに任せ、ここでは組み立てて例外を訳すだけにする。
    """

    def clean(self):
        cleaned = super().clean()
        # 個々の欄が不正なうちは、組み立てても意味のある判定にならない
        if self.errors:
            return cleaned

        try:
            self.build_value_object(cleaned)
        except DomainError as error:
            raise forms.ValidationError(str(error)) from None
        return cleaned

    def build_value_object(self, cleaned):
        raise NotImplementedError


class StadiumForm(DomainCheckedForm):
    """球場の編集。本拠地とするチームもここから決められる。

    所属の出典は Team.home_stadium の1か所だけ。この欄は同じ関係を
    球場の側から編むためのもので、別に持つわけではない。
    """

    home_teams = forms.ModelMultipleChoiceField(
        queryset=Team.objects.select_related('league').order_by(
            'league__display_order', 'league__name', 'display_order', 'name'
        ),
        required=False,
        label='本拠地とするチーム',
        help_text='ここで選ぶと、そのチームの本拠地がこの球場になります。'
                  '他の球場を本拠地にしていたチームは、こちらへ移ります。',
        widget=FilteredSelectMultiple('チーム', is_stacked=False),
    )

    class Meta:
        model = Stadium
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields['home_teams'].initial = self.instance.home_teams.all()

    def build_value_object(self, cleaned):
        return StadiumProfile(
            city=cleaned.get('city', ''),
            capacity=cleaned.get('capacity'),
            surface=cleaned.get('surface', ''),
            opened_year=cleaned.get('opened_year'),
            roof=cleaned.get('roof', ''),
        )


@admin.register(Stadium)
class StadiumAdmin(admin.ModelAdmin):
    form = StadiumForm
    list_display = (
        'name', 'city', 'capacity', 'surface', 'roof', 'opened_year', 'home_team_names'
    )
    search_fields = ('name', 'city')
    list_filter = ('surface', 'roof')
    ordering = ('name',)

    fieldsets = (
        (None, {'fields': ('name', 'city')}),
        ('設備', {
            'description': '分かっているものだけ入力してください。すべて任意です。',
            'fields': ('capacity', 'surface', 'roof', 'opened_year'),
        }),
        ('本拠地', {
            'description': 'この球場を本拠地とするチーム。'
                           'チームの編集画面からも同じ設定ができます。',
            'fields': ('home_teams',),
        }),
    )

    def get_queryset(self, request):
        """一覧に出す本拠地チームを先読みする。行ごとに引くと N+1 になる。"""
        return super().get_queryset(request).prefetch_related('home_teams')

    @admin.display(description='本拠地とするチーム')
    def home_team_names(self, obj):
        names = [team.name for team in obj.home_teams.all()]
        return '、'.join(names) if names else '—'

    def save_related(self, request, form, formsets, change):
        """本拠地の割り当てを Team 側へ書き戻す。

        持ち主は Team.home_stadium なので、球場の側で選んだ結果を
        そちらへ反映する。外されたチームは本拠地なしに戻す。
        """
        super().save_related(request, form, formsets, change)

        stadium = form.instance
        selected = form.cleaned_data.get('home_teams')
        if selected is None:
            return

        Team.objects.filter(home_stadium=stadium).exclude(
            pk__in=[team.pk for team in selected]
        ).update(home_stadium=None)
        Team.objects.filter(pk__in=[team.pk for team in selected]).update(
            home_stadium=stadium
        )


class PlayerStintForm(forms.ModelForm):
    """在籍の入力検証。

    管理画面はドメインを経由しないため、放っておくと「同じチームで同じ背番号の
    選手が同時に2人」といった状態を作れてしまう。判定そのものはドメインの
    Stint に任せ、ここでは既存の在籍と突き合わせるだけにする。
    """

    class Meta:
        model = PlayerStint
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 入団年で埋められるので、入力そのものは必須にしない
        self.fields['from_year'].required = False
        self.fields['from_year'].help_text = '空欄なら選手の入団年を使います。'

    def clean(self):
        cleaned = super().clean()
        self._fill_from_year(cleaned)

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
        # 選手の新規登録では、在籍と一緒に送られてくる選手がまだ保存されていない。
        # 未保存のまま絞り込むと落ちるうえ、そもそも既存の在籍を持たないので
        # 除外する必要も無い
        player = cleaned.get('player')
        if player is not None and player.pk:
            others = others.exclude(player=player)

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

    def _fill_from_year(self, cleaned):
        """加入年が空欄なら、選手の入団年で埋める。

        最初の在籍では加入年＝入団年になることがほとんどで、同じ年を
        二度入力させる意味が無い。どちらも空欄のときだけ入力を求める。
        """
        if cleaned.get('from_year') is not None:
            return

        player = cleaned.get('player')
        debut_year = getattr(player, 'debut_year', None) if player else None
        if debut_year is None:
            self.add_error(
                'from_year',
                '加入年を入力してください。'
                '選手に入団年が登録されていれば、空欄でもそちらを使います。',
            )
            return

        cleaned['from_year'] = debut_year


class PlayerStintFormSet(forms.BaseInlineFormSet):
    """同じ選手の在籍どうしの食い違いを見る。

    1行ずつの検証では、同時に送られた他の行が見えない。まとめて登録すると
    「同じチームに期間が重なって在籍している」経歴が通ってしまうため、
    ここで突き合わせる。

    別チームどうしの重なりは弾かない。シーズン途中の移籍では、移籍元と
    移籍先の在籍が同じ年を共有するのが普通のため。
    """

    def clean(self):
        super().clean()
        if any(self.errors):
            return

        checked = []
        for form in self.forms:
            data = getattr(form, 'cleaned_data', None)
            if not data or data.get('DELETE'):
                continue
            team = data.get('team')
            number = data.get('number')
            from_year = data.get('from_year')
            if not (team and number is not None and from_year is not None):
                continue

            current = DomainStint(
                team_id=team.id,
                number=JerseyNumber(number),
                from_year=from_year,
                to_year=data.get('to_year'),
            )
            for other in checked:
                if other.team_id == current.team_id and current.overlaps(other):
                    form.add_error(
                        'from_year',
                        f'{team.name} の在籍 {other} と期間が重なっています。'
                        f'同じチームに同時に2度在籍することはできません。',
                    )
                    break
            checked.append(current)


class PlayerStintInline(admin.TabularInline):
    """在籍。所属と背番号はここが出典で、移籍すると行が増える。"""

    model = PlayerStint
    form = PlayerStintForm
    formset = PlayerStintFormSet
    extra = 0
    fields = ('team', 'number', 'from_year', 'to_year')
    ordering = ('-from_year',)
    autocomplete_fields = ('team',)
    verbose_name_plural = '在籍（経歴）'


class PlayerForm(DomainCheckedForm):
    class Meta:
        model = Player
        fields = '__all__'

    def build_value_object(self, cleaned):
        return DomainProfile(
            birth_date=cleaned.get('birth_date'),
            throws=cleaned.get('throws') or None,
            bats=cleaned.get('bats') or None,
            height_cm=cleaned.get('height_cm'),
            weight_kg=cleaned.get('weight_kg'),
            birthplace=cleaned.get('birthplace', ''),
            debut_year=cleaned.get('debut_year'),
            high_school=cleaned.get('high_school', ''),
            university=cleaned.get('university', ''),
            corporate_team=cleaned.get('corporate_team', ''),
        )


@admin.register(Player)
class PlayerAdmin(admin.ModelAdmin):
    form = PlayerForm
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
        ('プロ入り前の経歴', {
            'description': '通った所だけ入力してください。高校からそのままプロ、'
                           '大学を経ずに社会人へ、といった順路にも対応します。',
            'fields': ('high_school', 'university', 'corporate_team'),
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
class PlayerStintAdmin(GroupedAdminMixin, admin.ModelAdmin):
    """在籍そのものの一覧。チームごとの名簿として使える。"""

    group_ordering = ('team__league__display_order', 'team__league__name', 'team__name')

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
