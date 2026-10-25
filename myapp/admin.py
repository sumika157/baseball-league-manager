import random
import types
from collections import Counter
from typing import ClassVar

from django import forms
from django.contrib import admin, messages
from django.contrib.admin.actions import delete_selected
from django.contrib.admin.views.main import ORDER_VAR, ChangeList
from django.contrib.admin.widgets import FilteredSelectMultiple
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import (
    Case,
    Count,
    F,
    IntegerField,
    OuterRef,
    Prefetch,
    Q,
    Subquery,
    Value,
    When,
)
from django.db.models.functions import Cast, Coalesce
from django.forms.formsets import DELETION_FIELD_NAME
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils.html import format_html, format_html_join

from .application.dto import OperatorOverview, OperatorWorkspaceRow
from .domain.entities import Captaincy as DomainCaptaincy
from .domain.entities import FreeAgentDeclaration as DomainFreeAgentDeclaration
from .domain.entities import Stint as DomainStint
from .domain.entities import (
    ensure_declarations_valid,
    ensure_draft_fits_career,
    ensure_free_agent_acquisitions,
    winning_team_id,
)
from .domain.exceptions import DomainError
from .domain.pennant.world import WorldScope
from .domain.value_objects import (
    AcquisitionRoute,
    ContractStatus,
    DraftPick,
    ForeignPlayerRules,
    FreeAgencyKind,
    GameTimes,
    JerseyNumber,
    StadiumProfile,
)
from .domain.value_objects import Profile as DomainProfile
from .domain.workspace import Visibility, generate_workspace_key, validate_workspace_key
from .infrastructure import orm_models
from .infrastructure.orm_models import (
    Captaincy,
    Game,
    GameBattingLine,
    GameFieldingLine,
    GameInningScore,
    GamePitchingLine,
    League,
    Player,
    PlayerFreeAgentDeclaration,
    PlayerStint,
    Stadium,
    Team,
    Workspace,
    WorkspaceEditor,
)
from .infrastructure.repositories import (
    DjangoLeagueRepository,
    DjangoTeamRepository,
    draft_of,
    duplicate_team_name,
    ensure_team_name_free,
)
from .infrastructure.scoping import (
    StageFilter,
    admin_real_queryset,
    leagues_in,
    stadiums_in,
    stage_condition,
    teams_in,
)
from .presentation.views import build_league_setup_service, build_recording_service, build_setup_query

# ヘッダーの文言
admin.site.site_header = "Baseball Manager 管理"
admin.site.site_title = "Baseball Manager"
admin.site.index_title = "データ管理"

# サイト側で registration/password_change_*.html を上書きしているため、
# 同名テンプレートを使う管理画面までサイトの見た目になってしまう。
# 管理画面には管理画面の体裁を保たせる。
admin.site.password_change_template = "admin/password_change_form.html"
admin.site.password_change_done_template = "admin/password_change_done.html"

# トップに概況を足したテンプレート。index.html という名前にすると自分自身を
# 継承する形になって成立しないため、別名にして明示指定している。
admin.site.index_template = "admin/dashboard_index.html"


def _ordered_app_list(self, request, app_label=None):
    """トップの並びを業務の重要度とドメインの階層に揃える。

    Django の既定は表示名の五十音／アルファベット順のため、
    「認証と認可」が先に来て、モデルもリーグ・チーム・選手が入り混じる。
    主に扱うのは野球データなので、そちらを先頭に置く。
    """
    app_list = _original_get_app_list(request, app_label)

    model_order = {
        "Workspace": 0,
        "League": 1,
        "Team": 2,
        "Player": 3,
        "PlayerStint": 4,
        "Game": 5,
        "Stadium": 6,
    }
    for app in app_list:
        if app.get("app_label") == "myapp":
            app["models"].sort(key=lambda m: model_order.get(m.get("object_name"), 99))

    # 野球データを先頭、認証を後ろに
    app_list.sort(key=lambda a: 0 if a.get("app_label") == "myapp" else 1)
    return app_list


_original_get_app_list = admin.site.get_app_list
# AdminSite を継承した独自サイトに置き換えるほどではないため、メソッドを差し替える
admin.site.get_app_list = types.MethodType(_ordered_app_list, admin.site)  # type: ignore[method-assign]

# 管理画面で扱うのは**実データだけ**。ペナントの世界の行は、一覧にも・id を指定した編集画面にも・
# 選択肢にも・フィルタにも出さない（手で直すと、世界の整合が実データの側から壊れるため）。
# ペナント専用のモデル（PennantWorld・PennantPlayerRatings・PennantFixture・PennantClubPlan と
# その行 PennantClubPlanEntry・PennantReleasePlan とその行 PennantReleasePlanEntry・表彰 PennantAward・
# 日本一 PennantChampionship）は管理画面に登録しない
# （手で直すと、能力と成績・日程の整合が壊れるため）。
# 管理画面は運営者が**全ワークスペースの実データ**を扱う。全範囲を読むのは `admin_real_queryset` だけで、
# 「全範囲を読む API を作らない」規則の唯一の例外（決定 2026-10-07 Q4。admin.py 以外からは使わない）。


def _real_queryset(model):
    """そのモデルの、全ワークスペースの実データの行だけの QuerySet。世界を持たないモデル（球場など）は None。"""
    return admin_real_queryset(model)


def _real_teams():
    """全ワークスペースの実データのチーム（ペナントの球団を除く）。"""
    return admin_real_queryset(Team)


# 運営向けの概況に出す、試合の多いワークスペースの数
OPERATOR_TOP_WORKSPACES = 5


def _real_rows(model):
    """全ワークスペースの実データの行（管理画面の一覧と同じ行）。世界の行を持たないモデル（球場など）は誤り。"""
    queryset = admin_real_queryset(model)
    if queryset is None:
        raise TypeError(f"{model.__name__} は実データの行を持たないモデルです")
    return queryset


def operator_overview() -> OperatorOverview:
    """管理画面のトップの、運営向けの全ワークスペースの概況（W2 T2）。

    全ワークスペースを読む集計なので、Q4 の例外（`admin_real_queryset` は admin.py だけ）の中に置く。
    テンプレートタグ（`templatetags/admin_overview.py`）はこれを呼ぶだけ。数字は管理画面の一覧と同じ行を
    数えるので、リンク先の一覧と件数がそろう（ペナントの世界の行は数えない）。
    クエリ数はワークスペースの数によらず一定（7本）。ワークスペースが1つも無くても、すべて0で返す。
    """
    workspaces = Workspace.objects.aggregate(
        total=Count("pk"),
        public=Count("pk", filter=Q(visibility=Visibility.PUBLIC.value)),
        private=Count("pk", filter=Q(visibility=Visibility.PRIVATE.value)),
    )
    games_of_workspace = (
        _real_rows(Game)
        .filter(home_team__league__workspace=OuterRef("pk"))
        .order_by()
        .values("home_team__league__workspace")
        .annotate(n=Count("pk"))
        .values("n")
    )
    top = (
        Workspace.objects.select_related("owner")
        .annotate(game_count=Coalesce(Subquery(games_of_workspace, output_field=IntegerField()), Value(0)))
        .order_by("-game_count", "id")[:OPERATOR_TOP_WORKSPACES]
    )
    return OperatorOverview(
        workspace_count=workspaces["total"],
        public_workspace_count=workspaces["public"],
        private_workspace_count=workspaces["private"],
        user_count=get_user_model().objects.count(),
        league_count=_real_rows(League).count(),
        team_count=_real_rows(Team).count(),
        player_count=_real_rows(Player).count(),
        game_count=_real_rows(Game).count(),
        top_workspaces=[
            OperatorWorkspaceRow(
                workspace_id=workspace.pk,
                key=workspace.key,
                name=workspace.name,
                owner_name=workspace.owner.get_username(),
                game_count=workspace.game_count,
            )
            for workspace in top
        ],
    )


def _row_scope(league) -> WorldScope:
    """行が属するリーグのワークスペースの、実データの範囲。リポジトリ・組み立て口を行ごとに作るのに使う。"""
    return WorldScope.real(league.workspace_id)


class RealDataOnlyMixin:
    """一覧・編集画面・選択肢を、実データの行だけに絞る ModelAdmin / Inline の部品。"""

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        scoped = _real_queryset(self.model)
        return queryset if scoped is None else queryset.filter(pk__in=scoped.values("pk"))

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        # 選択肢（プルダウン・検索）にペナントの行を出さない
        scoped = _real_queryset(db_field.remote_field.model)
        if scoped is not None and "queryset" not in kwargs:
            # チームの選択肢は「同じワークスペースか」の検査でリーグを読むので、先に引く
            kwargs["queryset"] = scoped.select_related("league") if db_field.remote_field.model is Team else scoped
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


class RealDataRelatedFieldListFilter(admin.RelatedFieldListFilter):
    """右側のフィルタの選択肢を、実データの行だけにする。"""

    def field_choices(self, field, request, model_admin):
        scoped = _real_queryset(field.remote_field.model)
        limit = Q(pk__in=scoped.values("pk")) if scoped is not None else None
        ordering = self.field_admin_ordering(field, request, model_admin)
        return field.get_choices(include_blank=False, ordering=ordering, limit_choices_to=limit)


# ワークスペースをまたぐ組み合わせを弾くときの文言。選択肢を絞るのは導線で、検査（clean）が本体
_CROSS_WORKSPACE = "「{name}」は別のワークスペースに属しています。同じワークスペースのものを選んでください。"
_NOT_IN_WORKSPACE_CHOICE = "正しい選択肢を選択してください。選べるのは、同じワークスペースの実データだけです。"


def _narrow_choices(fields, name, queryset):
    """フォームの外部キー・多対多の欄の選択肢を、与えた QuerySet の範囲に絞る（欄が無ければ何もしない）。

    追加画面では行のワークスペースが決まっていないので絞れない。絞るのは変更画面の導線で、
    範囲をまたぐ組み合わせを本当に弾くのは各フォームの `clean()`。
    """
    field = fields.get(name)
    if field is None:
        return
    field.queryset = field.queryset.filter(pk__in=queryset.values("pk"))
    field.error_messages["invalid_choice"] = _NOT_IN_WORKSPACE_CHOICE


def _player_workspace_ids(player, *, excluding_stint_pk=None, excluding_captaincy_pk=None) -> set[int]:
    """選手の保存済みの在籍・主将在任が属すワークスペースの id。どちらも無ければ空（どこにも縛られない）。

    主将在任も数える（在籍の無い選手に過去の主将在任だけを足せるため、在籍だけを見ると
    あとから別のワークスペースの在籍を足して、選手が2つの範囲にまたがってしまう）。
    """
    if player is None or not player.pk:
        return set()
    stints = PlayerStint.objects.filter(player=player)
    if excluding_stint_pk is not None:
        stints = stints.exclude(pk=excluding_stint_pk)
    captaincies = Captaincy.objects.filter(player=player)
    if excluding_captaincy_pk is not None:
        captaincies = captaincies.exclude(pk=excluding_captaincy_pk)
    ids = set(stints.values_list("team__league__workspace_id", flat=True))
    ids.update(captaincies.values_list("team__league__workspace_id", flat=True))
    ids.discard(None)
    return ids


def _formset_workspace_ids(formset) -> tuple[set[int], set[int]]:
    """在籍・主将在任のフォームセットの、(送信後の行のワークスペース, 送信に含まれなかった保存済みの行のもの)。

    送信後の行は、削除する行を除く（入力エラーのある行は保存済みの値で見る）。送信に含まれなかった保存済みの行
    （POST から行を落とした場合）も、DB に残るので数える。どちらも、選択肢を絞る処理に頼らずに検査するため。
    フォームセット全体の `is_valid()` は呼ばない（在籍と主将在任が互いに呼ぶと循環する）。
    """
    submitted: set[int] = set()
    kept_pks = set()
    for form in formset.forms:
        if form.instance.pk:
            kept_pks.add(form.instance.pk)
        if form.is_valid():
            data = form.cleaned_data
            team = None if data.get("DELETE") else data.get("team")
        else:
            team = form.instance.team if form.instance.pk else None
        if team is not None:
            submitted.add(team.league.workspace_id)
    omitted: set[int] = set()
    parent = formset.instance
    if parent.pk:
        rows = formset.model.objects.filter(player=parent).exclude(pk__in=kept_pks)
        omitted = {pk for pk in rows.values_list("team__league__workspace_id", flat=True) if pk is not None}
    return submitted, omitted


def _ensure_rows_share_workspace(formset, other_formset) -> None:
    """選手の在籍・主将在任の行は、すべて同じワークスペースのチームでなければならない（選手は1つの範囲に属す）。

    比べる相手は、もう一方のフォームセットの送信後の行・どちらにも含まれなかった保存済みの行・
    このフォームセットの先の行。選択肢を絞る処理（`PlayerWorkspaceChoicesMixin`）が外れても、これが本体として弾く。
    """
    others: set[int] = set()
    if other_formset is not None:
        submitted, omitted = _formset_workspace_ids(other_formset)
        others |= submitted | omitted
    others |= _formset_workspace_ids(formset)[1]
    for form in formset.forms:
        if not form.is_valid() or form.cleaned_data.get("DELETE"):
            continue
        team = form.cleaned_data.get("team")
        if team is None:
            continue
        workspace_id = team.league.workspace_id
        if others and workspace_id not in others:
            form.add_error("team", _CROSS_WORKSPACE.format(name=team.name))
        else:
            others.add(workspace_id)


class PlayerWorkspaceChoicesMixin:
    """選手の下に並ぶ行（在籍・主将在任）の「チーム」の選択肢を、選手の在籍と同じワークスペースに絞る。

    選手がちょうど1つのワークスペースにだけ在籍しているときだけ絞る（新しい選手や、
    在籍が無い選手は決まらないので絞らない。そのときは `clean()` が本体）。
    """

    def add_fields(self, form, index):
        super().add_fields(form, index)
        if "_workspace_id_set" not in self.__dict__:
            self._workspace_id_set = _player_workspace_ids(self.instance)
        if len(self._workspace_id_set) == 1:
            (workspace_id,) = self._workspace_id_set
            _narrow_choices(form.fields, "team", teams_in(WorldScope.real(workspace_id)))


class GroupedChangeList(ChangeList):
    """区切りを保ったまま並べ替える一覧。

    Django の既定では列を押すと並びがその列だけで決まり、まとまりが崩れて
    全行が混ざる。まとまりの順序を常に先に置き、利用者が指定した並びは
    その中で効かせる。

    手動で並べ替える一覧では使えない（ManualOrderAdminMixin を使う）。
    """

    def get_ordering(self, request, queryset):
        ordering = super().get_ordering(request, queryset)
        prefix = list(getattr(self.model_admin, "group_ordering", ()))
        if not prefix:
            return ordering

        fixed = {field.lstrip("-") for field in prefix}
        return prefix + [f for f in ordering if f.lstrip("-") not in fixed]


class GroupedAdminMixin:
    """group_by で区切る一覧に、区切りを保つ並べ替えを組み込む。"""

    def get_changelist(self, request, **kwargs):
        return GroupedChangeList


class HomeTeamOrderedChangeList(ChangeList):
    """球場を、本拠地とするチームの並び順で並べる一覧。

    ModelAdmin.get_ordering はチームの編集画面の本拠地プルダウンにも使われる。
    そちらは注釈の無い問い合わせなので、注釈を前提にした並びを ModelAdmin 側に
    置くと壊れる。一覧に閉じてここで指定する。
    """

    # 並べ替えの手がかり。ModelAdmin 側の annotate と対になっている
    HOME_TEAM_ORDER = (
        "home_league_order",
        "home_league_name",
        "home_team_order",
        "home_team_name",
    )

    def get_ordering(self, request, queryset):
        ordering = super().get_ordering(request, queryset)
        # 列を押したときは、その指定をそのまま使う
        if ORDER_VAR in self.params:
            return ordering

        # 本拠地の無い球場はどの手がかりも空になるため末尾へ回す。
        # そのあとは ModelAdmin の並び（球場名）が効く
        return [F(field).asc(nulls_last=True) for field in self.HOME_TEAM_ORDER] + list(ordering)


class ManualOrderAdminMixin:
    """行をドラッグして並べ替える一覧。列での並べ替えは持たない。

    ドラッグした順がそのまま保存される順なので、列で並べ替えると
    見えている順と保存される順が食い違い、ドラッグを止めるほかなくなる。
    2つの並べ方は両立しないため、この一覧では手動の順だけを扱う。
    """

    # 列見出しの並べ替えリンクを出さない。型は BaseModelAdmin の宣言に合わせる
    sortable_by: ClassVar[list[str] | tuple[str, ...] | None] = ()

    def changelist_view(self, request, extra_context=None):
        """URL に並べ替えが残っていたら落として開き直す。

        古いリンクや履歴から入っても、並べ替えられない状態に迷い込ませない。
        無視するだけでは絞り込みのリンクなどに紛れて残ってしまうため、
        URL そのものから消す。絞り込みなど他の指定は保つ。
        """
        if request.method == "GET" and ORDER_VAR in request.GET:
            params = request.GET.copy()
            del params[ORDER_VAR]
            query = params.urlencode()
            return HttpResponseRedirect(f"{request.path}?{query}" if query else request.path)
        return super().changelist_view(request, extra_context)


def _delete_selected_unless_blocked(modeladmin, request, queryset):
    """一括削除。消せない行（`deletion_blocker` が理由を返す行）が含まれていれば、理由だけを出して何も消さない。"""
    blocked = [f"{row}: {reason}" for row in queryset if (reason := modeladmin.deletion_blocker(row))]
    if blocked:
        label = modeladmin.model._meta.verbose_name
        modeladmin.message_user(
            request, f"削除できない{label}が含まれているため、何も削除しませんでした。", level=messages.ERROR
        )
        for line in blocked:
            modeladmin.message_user(request, line, level=messages.ERROR)
        return None
    return delete_selected(modeladmin, request, queryset)


class BlockedDeletionAdmin(admin.ModelAdmin):
    """消せない行の削除の導線（ボタン・削除画面）を出さず、一括削除でも何も消さない ModelAdmin（部品として継承する）。

    サブクラスが `deletion_blocker(obj)` で消せない理由を返す（消せるなら空文字か None）。
    拒否したのに「削除しました」と出るのを避けるため、導線ごと消す（削除画面を直接開いても 403）。
    """

    def deletion_blocker(self, obj) -> str | None:
        raise NotImplementedError

    def has_delete_permission(self, request, obj=None):
        if obj is not None and self.deletion_blocker(obj):
            return False
        return super().has_delete_permission(request, obj)

    def get_actions(self, request):
        actions = super().get_actions(request)
        if "delete_selected" in actions:
            _, name, description = actions["delete_selected"]
            actions["delete_selected"] = (_delete_selected_unless_blocked, name, description)
        return actions


class SetupRulesDeletionAdmin(BlockedDeletionAdmin):
    """リーグ・チーム・球場の削除を、サイトの登録画面と同じ規則で扱う（W3 T5。#79。2026-10-25 決定 3）。

    消せるかは、サイトの登録画面と同じ参照クエリ（`build_setup_query`）の行の `deletion_blocker` で決める
    （数え方は `league_usage`・`team_usage`・`stadium_usage`、規則は `domain/setup.py`。どちらもサイトと共有し、
    管理画面に別の数え方を作らない）。消すのも登録のサービス（`build_league_setup_service` の `delete_*`）で、
    リポジトリが規則をもう一度通してから範囲で絞った1行を消す。
    Django の collector の連鎖（在籍・試合まで消える）では消さない。
    """

    _BLOCKER_CACHE = "_setup_deletion_blocker"

    def setup_row(self, obj):
        """`obj` の登録画面の行（`LeagueSetupRow` など。`deletion_blocker` を持つ）。"""
        raise NotImplementedError

    def delete_by_setup_rules(self, obj) -> None:
        raise NotImplementedError

    def deletion_blocker(self, obj) -> str | None:
        # 1回の画面で何度も聞かれる（削除のボタン・削除画面）ので、行に覚えておく（クエリは行ごとに1本）
        if not hasattr(obj, self._BLOCKER_CACHE):
            setattr(obj, self._BLOCKER_CACHE, self.setup_row(obj).deletion_blocker)
        return getattr(obj, self._BLOCKER_CACHE)

    def delete_model(self, request, obj):
        self.delete_by_setup_rules(obj)

    def delete_queryset(self, request, queryset):
        # 一括削除は消す前に全行を検査済み（`_delete_selected_unless_blocked`）。途中で弾かれたら何も消さない
        with transaction.atomic():
            for obj in queryset:
                self.delete_by_setup_rules(obj)


def _grouped_count(queryset, group_field):
    """親1件ぶんの件数を返す副問い合わせを組み立てる。

    一覧の各行で数えると行数だけ問い合わせが増えるため、
    annotate と組み合わせて1回のSQLにまとめるために使う。
    """
    return queryset.order_by().values(group_field).annotate(n=Count("pk")).values("n")[:1]


class TeamInlineForm(forms.ModelForm):
    """表示順は画面に出さない。

    数値そのものに意味は無く、ドラッグした結果が入るだけなので、
    列として見せると読み手を混乱させる。ただし送信は必要なので
    hidden で残す（JavaScript もこの入力欄を目印に行を見つける）。
    """

    class Meta:
        model = Team
        fields = ("display_order", "name", "home_stadium")
        widgets = {"display_order": forms.HiddenInput()}


class TeamInlineFormSet(forms.BaseInlineFormSet):
    """リーグのチーム。本拠地の球場は、そのリーグと同じワークスペースのものに限る。

    チームの削除（行の「削除」）とチーム名の一意は、チームの変更画面（`TeamAdmin`・`TeamForm`）と同じ規則で検査する
    （W3 T5。#79）。消せないチームの行には「削除」の欄を出さず、送られてきても検査で弾く。消すのは登録のサービスで、
    Django の collector の連鎖（在籍・試合まで消える）では消さない。
    """

    def add_fields(self, form, index):
        super().add_fields(form, index)
        workspace_id = self.instance.workspace_id
        if workspace_id is not None:
            _narrow_choices(form.fields, "home_stadium", stadiums_in(WorldScope.real(workspace_id)))
        if DELETION_FIELD_NAME in form.fields and self._deletion_blocker(form.instance.pk):
            # 消せないチームは「削除」の導線を出さない（値は送るが常に偽。偽造して真を送っても `clean` が弾く）
            form.fields[DELETION_FIELD_NAME].widget = forms.HiddenInput()

    def _deletion_blocker(self, team_id) -> str | None:
        """このリーグのチーム（`team_id`）を消せない理由。サイトの登録画面と同じ行（`build_setup_query`）から読む。"""
        if team_id is None:
            return None
        if not hasattr(self, "_team_rows"):
            # ワークスペースのチームをクエリ1本で読む（行ごとに引かない）
            workspace_id = self.instance.workspace_id
            rows = build_setup_query(WorldScope.real(workspace_id)).teams() if workspace_id is not None else []
            self._team_rows = {row.team_id: row for row in rows}
        row = self._team_rows.get(team_id)
        return row.deletion_blocker if row is not None else None

    def clean(self):
        super().clean()
        workspace_id = self.instance.workspace_id
        if workspace_id is None:
            return
        for form in self.forms:
            stadium = getattr(form, "cleaned_data", {}).get("home_stadium")
            if stadium is not None and stadium.workspace_id != workspace_id:
                form.add_error("home_stadium", _CROSS_WORKSPACE.format(name=stadium.name))
        self._check_team_names(workspace_id)
        blocked = [
            f"「{form.instance.name}」: {reason}"
            for form in self.deleted_forms
            if (reason := self._deletion_blocker(form.instance.pk))
        ]
        if blocked:
            raise forms.ValidationError(blocked)

    def _check_team_names(self, workspace_id) -> None:
        """新しい行と名前を変えた行の名前が、ワークスペースの中で重ならないか（`TeamForm` と同じ規則）。

        保存した後の状態で比べる: このリーグの行（消す行を除く）どうしと、このリーグの外のワークスペースのチーム。
        行どうしの入れ替え（A↔B の改名）は重ならないので通す。
        名前を変えない行は咎めない（`TeamRepository.save` と同じ）。
        """
        deleted = set(self.deleted_forms)
        kept = [form for form in self.forms if form not in deleted and getattr(form, "cleaned_data", {}).get("name")]
        counts = Counter(form.cleaned_data["name"] for form in kept)
        others = teams_in(WorldScope.real(workspace_id)).exclude(
            pk__in=[form.instance.pk for form in self.forms if form.instance.pk is not None]
        )
        for form in kept:
            if form.instance.pk is not None and "name" not in form.changed_data:
                continue
            name = form.cleaned_data["name"]
            try:
                if counts[name] > 1:
                    raise duplicate_team_name(name)
                ensure_team_name_free(others, name, None)
            except DomainError as error:
                form.add_error("name", str(error))

    def delete_existing(self, obj, commit=True):
        """行の「削除」は登録のサービスで消す（リポジトリが規則をもう一度通し、そのチームの1行だけを消す）。"""
        if commit:
            build_league_setup_service(WorldScope.real(self.instance.workspace_id)).delete_team(obj.pk)


class TeamInline(RealDataOnlyMixin, admin.TabularInline):
    """リーグに所属するチーム。行をドラッグして表示順を並べ替えられる。"""

    model = Team
    form = TeamInlineForm
    formset = TeamInlineFormSet
    extra = 0
    fields = ("display_order", "name", "home_stadium")
    ordering = ("display_order", "name")
    show_change_link = True


class WorkspaceRequiredForm(forms.ModelForm):
    """ワークスペースを必須にするフォーム。

    リーグの列は（ペナントのリーグが持たないので）null 可のままだが、実データのリーグ・球場は
    必ずどこかに属す。フォームに含めて必須にすれば、選び忘れが 500 ではなくフォームのエラーになる。
    同名の重複は、モデルの UniqueConstraint がフォームの検証で日本語のエラーにする。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 登録済みの行ではワークスペースを読み取り専用にする（その場合、欄は無い）
        if "workspace" in self.fields:
            self.fields["workspace"].required = True

    def _get_validation_exclusions(self):
        """読み取り専用で欄が無い `workspace` も、一意制約の検査に含める。

        含めないと、ModelForm は（ワークスペース, 名前）の一意制約を丸ごと飛ばし、同名の重複が
        フォームのエラーではなく DB の IntegrityError（500）になる。行の値は保存済みなのでそのまま使える。
        """
        exclude = super()._get_validation_exclusions()
        if "workspace" not in self.fields:
            exclude.discard("workspace")
        return exclude


class LeagueForm(WorkspaceRequiredForm):
    class Meta:
        model = League
        fields = "__all__"
        # ペナントのリーグは世界側で作る。管理画面から world を付けて workspace との排他を崩さない
        exclude = ("world",)


@admin.register(League)
class LeagueAdmin(SetupRulesDeletionAdmin, RealDataOnlyMixin, ManualOrderAdminMixin, admin.ModelAdmin):
    # display_order は行をドラッグすると書き換わる。数値そのものに意味は無いが、
    # JavaScript が動かない環境でも直接入力できるよう残してある
    list_display = ("name", "workspace", "display_order", "teams_accordion", "created_at")
    list_display_links = ("name",)
    list_editable = ("display_order",)
    list_filter = ("workspace",)
    list_select_related = ("workspace",)
    search_fields = ("name",)
    # 手動の並び順を既定にする。未設定どうしは名前で安定させる
    ordering = ("display_order", "name")
    inlines = [TeamInline]
    form = LeagueForm
    fieldsets = (
        (None, {"fields": ("name", "workspace", "display_order")}),
        (
            "外国人枠",
            {
                "description": (
                    "1軍（出場選手登録）に置ける外国人選手の上限です"
                    f"（野球協約 第82条の2 は{ForeignPlayerRules.NPB.label}）。"
                    "保有の人数は縛りません。投手か野手かは登録位置で数えます。空欄なら無制限として扱います。"
                ),
                "fields": ("foreign_active_limit", "foreign_active_pitcher_limit", "foreign_active_batter_limit"),
            },
        ),
        (
            "支配下枠",
            {
                "description": "空欄なら無制限として扱います。育成選手は数えません。",
                "fields": ("registered_player_limit",),
            },
        ),
    )

    class Media:
        js = ("myapp/js/admin-inline-sortable.js",)
        css = {"all": ("myapp/css/admin-theme.css",)}

    # 表示順の列は隠してあるので、操作方法を画面上で伝える
    change_list_template = "admin/sortable_change_list.html"

    def get_readonly_fields(self, request, obj=None):
        """登録済みのリーグはワークスペースを変えられない。

        球団・試合の帰属はリーグ経由で決まるので、付け替えると中身ごと別のワークスペースへ移ってしまう。
        リポジトリも帰属を書き換えない。
        """
        fixed = super().get_readonly_fields(request, obj)
        return (*fixed, "workspace") if obj is not None else fixed

    def setup_row(self, obj):
        # チームが1つも無いときだけ消せる（`league_deletion_blocker`）
        return build_setup_query(_row_scope(obj)).league(obj.pk)

    def delete_by_setup_rules(self, obj) -> None:
        build_league_setup_service(_row_scope(obj)).delete_league(obj.pk)

    def get_queryset(self, request):
        """所属チームを先読みする。行ごとに引くと一覧で N+1 になる。"""
        teams = orm_models.Team.objects.annotate(
            active_players=Count("stints", filter=Q(stints__to_year__isnull=True))
        ).order_by("display_order", "name")
        return super().get_queryset(request).prefetch_related(Prefetch("teams", queryset=teams))

    @admin.display(description="所属チーム")
    def teams_accordion(self, obj):
        """所属チームを折りたたんで表示する。

        details 要素を使えば JavaScript なしで開閉できる。行数の多い一覧で
        全チームを常に出すと縦に長くなるため、既定では畳んでおく。
        """
        teams = list(obj.teams.all())
        if not teams:
            return "—"

        items = format_html_join(
            "",
            '<li><a href="{}">{}</a><span class="team-accordion-meta">{}名</span></li>',
            (
                (
                    reverse("admin:myapp_team_change", args=[team.id]),
                    team.name,
                    team.active_players,
                )
                for team in teams
            ),
        )
        return format_html(
            '<details class="team-accordion"><summary>{}チーム</summary><ul>{}</ul></details>',
            len(teams),
            items,
        )


class TeamForm(forms.ModelForm):
    """チームの編集。リーグと本拠地の球場は、同じワークスペースのものに限る。

    変更画面は、編集中のチームのワークスペースに選択肢を絞る。追加画面はまだ決まっていないので
    絞れず、`clean()` が「リーグと球場が同じワークスペースか」を見る（検査が本体）。

    チーム名の一意と所属リーグの変更は、サイトの登録画面と同じ規則で検査する（W3 T5。#79）: 名前は
    `ensure_team_name_free`（`TeamRepository.save` と同じ関数。新しいチームと改名のときだけ）、所属リーグは
    登録画面の行の `league_change_blocker`（試合のあるチームは変えられない。変えたときだけ）。
    """

    class Meta:
        model = Team
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            workspace_id = self.instance.league.workspace_id
            _narrow_choices(self.fields, "league", leagues_in(WorldScope.real(workspace_id)))
            _narrow_choices(self.fields, "home_stadium", stadiums_in(WorldScope.real(workspace_id)))

    def clean(self):
        cleaned = super().clean()
        league, stadium, name = cleaned.get("league"), cleaned.get("home_stadium"), cleaned.get("name")
        if league is not None and stadium is not None and league.workspace_id != stadium.workspace_id:
            self.add_error("home_stadium", _CROSS_WORKSPACE.format(name=stadium.name))
        if league is None:
            return cleaned
        scope = WorldScope.real(league.workspace_id)
        if name and (self.instance.pk is None or "name" in self.changed_data):
            try:
                ensure_team_name_free(teams_in(scope), name, self.instance.pk)
            except DomainError as error:
                self.add_error("name", str(error))
        if self.instance.pk is not None and "league" in self.changed_data:
            reason = build_setup_query(scope).team(self.instance.pk).league_change_blocker
            if reason is not None:
                self.add_error("league", reason)
        return cleaned


@admin.register(Team)
class TeamAdmin(SetupRulesDeletionAdmin, RealDataOnlyMixin, ManualOrderAdminMixin, admin.ModelAdmin):
    form = TeamForm
    # display_order は行をドラッグすると書き換わる。リーグ編集画面からだけでなく
    # この一覧でも並べ替えられるようにしてある
    list_display = ("name", "workspace", "display_order", "home_stadium", "active_player_count", "game_count")
    list_display_links = ("name",)
    list_editable = ("display_order",)
    list_filter = (("league", RealDataRelatedFieldListFilter), "league__workspace")
    search_fields = ("name", "home_stadium__name")
    autocomplete_fields = ("home_stadium",)
    # 担当者はここで割り当てる。左右2ペインの選択肢の方が M2M の既定より選びやすい
    filter_horizontal = ("managers",)
    # リーグごとにまとまるよう並べる。リーグ内は手動の表示順を尊重する。
    # この並びが区切り表示の前提（同じリーグの行が続いていないと見出しが何度も出る）
    ordering = ("league__display_order", "league__name", "display_order", "name")
    list_select_related = ("league", "league__workspace", "home_stadium")

    class Media:
        js = ("myapp/js/admin-inline-sortable.js",)
        css = {"all": ("myapp/css/admin-theme.css",)}

    # 表示順の列は隠してあるので、操作方法を画面上で伝える
    change_list_template = "admin/sortable_change_list.html"

    def get_queryset(self, request):
        """一覧に出す件数を、すべて副問い合わせで先に数えておく。

        行ごとに数えると表示する行数だけ問い合わせが増える。
        """
        return (
            super()
            .get_queryset(request)
            .annotate(
                league_team_count=Subquery(
                    orm_models.League.objects.filter(pk=OuterRef("league_id"))
                    .annotate(n=Count("teams"))
                    .values("n")[:1]
                ),
                active_players_count=Coalesce(
                    Subquery(
                        _grouped_count(
                            orm_models.PlayerStint.objects.filter(team=OuterRef("pk"), to_year__isnull=True),
                            "team",
                        ),
                        output_field=IntegerField(),
                    ),
                    0,
                ),
                played_games_count=(
                    Coalesce(
                        Subquery(
                            _grouped_count(
                                orm_models.Game.objects.filter(
                                    stage_condition("", StageFilter.REGULAR_ONLY), home_team=OuterRef("pk")
                                ),
                                "home_team",
                            ),
                            output_field=IntegerField(),
                        ),
                        0,
                    )
                    + Coalesce(
                        Subquery(
                            _grouped_count(
                                orm_models.Game.objects.filter(
                                    stage_condition("", StageFilter.REGULAR_ONLY), away_team=OuterRef("pk")
                                ),
                                "away_team",
                            ),
                            output_field=IntegerField(),
                        ),
                        0,
                    )
                ),
            )
        )

    # 行をリーグごとに区切る。見出しに出るのでリーグ列は list_display から外した。
    # 表示順はリーグの中でしか意味が無いため、ドラッグも区切りをまたげない。
    # 区切りが成立するのは ordering がリーグ単位でまとめているから
    group_by = staticmethod(lambda team: f"{team.league.name}（{team.league_team_count}チーム）")

    @admin.display(description="ワークスペース", ordering="league__workspace__name")
    def workspace(self, obj):
        # リーグ経由（チームは自分では帰属を持たない）
        return obj.league.workspace

    @admin.display(description="現役選手", ordering="active_players_count")
    def active_player_count(self, obj):
        # 在籍中＝退団年が空の在籍。件数は get_queryset で数えてある
        return obj.active_players_count

    @admin.display(description="試合数", ordering="played_games_count")
    def game_count(self, obj):
        return obj.played_games_count

    def setup_row(self, obj):
        # 在籍・主将の在任・試合が1つも無いときだけ消せる（`team_deletion_blocker`。試合は区分を問わず数える）
        return build_setup_query(_row_scope(obj.league)).team(obj.pk)

    def delete_by_setup_rules(self, obj) -> None:
        build_league_setup_service(_row_scope(obj.league)).delete_team(obj.pk)


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
            value_object = self.build_value_object(cleaned)
            self.extra_clean(cleaned, value_object)
        except DomainError as error:
            raise forms.ValidationError(str(error)) from None
        return cleaned

    def build_value_object(self, cleaned):
        raise NotImplementedError

    def extra_clean(self, cleaned, value_object) -> None:
        """既定では何もしない。サブクラスが追加のドメイン検査を差し込む。"""


class StadiumForm(WorkspaceRequiredForm, DomainCheckedForm):
    """球場の編集。本拠地とするチームもここから決められる。

    所属の出典は Team.home_stadium の1か所だけ。この欄は同じ関係を
    球場の側から編むためのもので、別に持つわけではない。
    """

    home_teams = forms.ModelMultipleChoiceField(
        queryset=_real_teams()
        .select_related("league")
        .order_by("league__display_order", "league__name", "display_order", "name"),
        required=False,
        label="本拠地とするチーム",
        help_text="ここで選ぶと、そのチームの本拠地がこの球場になります。"
        "他の球場を本拠地にしていたチームは、こちらへ移ります。",
        widget=FilteredSelectMultiple("チーム", is_stacked=False),
    )

    class Meta:
        model = Stadium
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields["home_teams"].initial = _real_teams().filter(home_stadium=self.instance)
            # 変更画面では、この球場と同じワークスペースのチームだけを並べる
            _narrow_choices(
                self.fields,
                "home_teams",
                teams_in(WorldScope.real(self.instance.workspace_id)),
            )

    def clean(self):
        cleaned = super().clean()
        # 登録済みの球場はワークスペースが読み取り専用（欄が無い）ので、行の値で見る
        workspace = cleaned.get("workspace")
        workspace_id = workspace.pk if workspace is not None else self.instance.workspace_id
        if workspace_id is None:
            return cleaned
        for team in cleaned.get("home_teams") or []:
            if team.league.workspace_id != workspace_id:
                self.add_error("home_teams", _CROSS_WORKSPACE.format(name=team.name))
        return cleaned

    def build_value_object(self, cleaned):
        return StadiumProfile(
            city=cleaned.get("city", ""),
            capacity=cleaned.get("capacity"),
            surface=cleaned.get("surface", ""),
            opened_year=cleaned.get("opened_year"),
            roof=cleaned.get("roof", ""),
        )


@admin.register(Stadium)
class StadiumAdmin(SetupRulesDeletionAdmin, RealDataOnlyMixin, admin.ModelAdmin):
    form = StadiumForm
    list_display = ("name", "workspace", "city", "capacity", "surface", "roof", "opened_year", "home_team_names")
    list_select_related = ("workspace",)
    search_fields = ("name", "city")
    list_filter = ("workspace", "surface", "roof")
    # 一覧では本拠地チームの並びが先に効き、その中でこの順になる。
    # プルダウンなど一覧以外の場所ではこれがそのまま使われる
    ordering = ("name",)

    fieldsets = (
        (None, {"fields": ("name", "workspace", "city")}),
        (
            "設備",
            {
                "description": "分かっているものだけ入力してください。すべて任意です。",
                "fields": ("capacity", "surface", "roof", "opened_year"),
            },
        ),
        (
            "本拠地",
            {
                "description": "この球場を本拠地とするチーム。チームの編集画面からも同じ設定ができます。",
                "fields": ("home_teams",),
            },
        ),
    )

    def get_changelist(self, request, **kwargs):
        return HomeTeamOrderedChangeList

    def get_readonly_fields(self, request, obj=None):
        """登録済みの球場はワークスペースを変えられない（リポジトリも帰属を書き換えない）。"""
        fixed = super().get_readonly_fields(request, obj)
        return (*fixed, "workspace") if obj is not None else fixed

    def setup_row(self, obj):
        # どのチームも本拠地にしていないときだけ消せる（`stadium_deletion_blocker`。ペナントの世界の球団も数える）
        return build_setup_query(WorldScope.real(obj.workspace_id)).stadium(obj.pk)

    def delete_by_setup_rules(self, obj) -> None:
        build_league_setup_service(WorldScope.real(obj.workspace_id)).delete_stadium(obj.pk)

    def get_queryset(self, request):
        """本拠地チームを先読みし、並べ替えの手がかりも一緒に引く。

        行ごとに引くと N+1 になるため、どちらも副問い合わせでまとめる。
        本拠地が複数あるときは、チームの並びで先頭に来るものを代表とする。
        """
        home_teams = (
            _real_teams()
            .filter(home_stadium=OuterRef("pk"))
            .order_by("league__display_order", "league__name", "display_order", "name")
        )

        return (
            super()
            .get_queryset(request)
            .prefetch_related(Prefetch("home_teams", queryset=_real_teams()))
            .annotate(
                home_league_order=Subquery(home_teams.values("league__display_order")[:1]),
                home_league_name=Subquery(home_teams.values("league__name")[:1]),
                home_team_order=Subquery(home_teams.values("display_order")[:1]),
                home_team_name=Subquery(home_teams.values("name")[:1]),
            )
        )

    @admin.display(description="本拠地とするチーム")
    def home_team_names(self, obj):
        names = [team.name for team in obj.home_teams.all()]
        return "、".join(names) if names else "—"

    def save_related(self, request, form, formsets, change):
        """本拠地の割り当てを Team 側へ書き戻す。

        持ち主は Team.home_stadium なので、球場の側で選んだ結果を
        そちらへ反映する。外されたチームは本拠地なしに戻す。
        """
        super().save_related(request, form, formsets, change)

        stadium = form.instance
        selected = form.cleaned_data.get("home_teams")
        if selected is None:
            return

        # ペナントの球団の本拠地は、実データの球場の編集では変えない
        _real_teams().filter(home_stadium=stadium).exclude(pk__in=[team.pk for team in selected]).update(
            home_stadium=None
        )
        _real_teams().filter(pk__in=[team.pk for team in selected]).update(home_stadium=stadium)


def _jersey_or_none(value):
    """入力された背番号を値オブジェクトにする。空欄なら None。"""
    return JerseyNumber(value) if value not in (None, "") else None


def _clean_jersey(value):
    """背番号の欄の検証。表記のまま（「00」は「00」）ドメインの `JerseyNumber` に判定させる。空欄は None。"""
    if value in (None, ""):
        return None
    try:
        return JerseyNumber(value).value
    except DomainError as error:
        raise forms.ValidationError(str(error)) from None


def _to_domain_stint(row: PlayerStint) -> DomainStint:
    """保存済みの在籍をドメインの在籍にする。

    保存済みの行が不正（管理画面を通さずに書かれた区分違いなど）でも画面を落とさず、
    他の検査と同じく入力エラーとして知らせる。
    """
    try:
        return DomainStint(
            team_id=row.team_id,
            number=JerseyNumber(row.number),
            from_year=row.from_year,
            to_year=row.to_year,
            signed_as=ContractStatus.from_label(row.signed_as),
            promoted_year=row.promoted_year,
            number_before_promotion=_jersey_or_none(row.number_before_promotion),
            acquired_via=AcquisitionRoute.from_label(row.acquired_via) if row.acquired_via else None,
        )
    except DomainError as error:
        raise forms.ValidationError(f"保存済みの在籍（{row.player.name}）が不正です: {error}") from None


def _domain_declarations_of(player, *, excluding_pk=None) -> list[DomainFreeAgentDeclaration]:
    """保存済みの FA 宣言をドメインの宣言にする。"""
    rows = PlayerFreeAgentDeclaration.objects.filter(player=player)
    if excluding_pk is not None:
        rows = rows.exclude(pk=excluding_pk)
    return [DomainFreeAgentDeclaration(year=d.year, kind=FreeAgencyKind.from_label(d.kind)) for d in rows]


def _domain_stints_of(player, *, excluding_pk=None) -> list[DomainStint]:
    """保存済みの在籍をドメインの在籍にする。"""
    rows = PlayerStint.objects.filter(player=player).select_related("player")
    if excluding_pk is not None:
        rows = rows.exclude(pk=excluding_pk)
    return [_to_domain_stint(row) for row in rows]


def ensure_player_free_agency_consistent(stints, declarations, name="") -> None:
    """在籍と FA 宣言の全体の整合（宣言の不変条件と、経路 FA の根拠）。ドメインの規則に任せ、入力エラーに直す。"""
    try:
        ensure_declarations_valid(stints, declarations, name)
        ensure_free_agent_acquisitions(stints, declarations, name)
    except DomainError as error:
        raise forms.ValidationError(str(error)) from None


def _draft_of_player(player) -> DraftPick | None:
    """選手の行（未保存の入力を含む）のドラフト。ドラフト年が無い・不正（選手の欄の検査が別に弾く）なら None。"""
    if player is None:
        return None
    try:
        return draft_of(player.draft_year, player.draft_round, is_development=player.is_development_draft)
    except DomainError:
        return None


def ensure_player_draft_fits(draft: DraftPick | None, stints) -> None:
    """ドラフトと最初の在籍の経路の整合。ドメインの規則（`ensure_draft_fits_career`）に任せ、入力エラーに直す。"""
    try:
        ensure_draft_fits_career(draft, stints)
    except DomainError as error:
        raise forms.ValidationError(str(error)) from None


class PlayerStintForm(forms.ModelForm):
    """在籍の入力検証。

    管理画面はドメインを経由しないため、放っておくと「同じチームで同じ背番号の
    選手が同時に2人」といった状態を作れてしまう。判定そのものはドメインの
    Stint に任せ、ここでは既存の在籍と突き合わせるだけにする。
    """

    # 選手の管理画面のインラインでは、送信後の在籍と宣言の全体をフォームセット側が検査する
    # （保存済みの宣言と突き合わせると、同じ送信での宣言の削除・追加を見落とす）。単独の在籍の管理画面では False
    checked_with_declarations = False

    class Meta:
        model = PlayerStint
        # 退団の経路はペナントの締める処理だけが書く（実データの管理画面には出さない。#168）。
        # 出すと、在籍中の在籍に経路を付ける保存がドメインの検査（`Stint`）を通らずに入り、集約が読めなくなる
        exclude = ("left_via",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # ドラフト年の翌年で埋められるので、入力そのものは必須にしない
        self.fields["from_year"].required = False
        self.fields["from_year"].help_text = "空欄なら選手のドラフト年の翌年を使います。"
        # 空欄なら支配下として扱う（モデルの既定と同じ）
        self.fields["signed_as"].required = False
        # 変更画面では、この在籍と同じワークスペースのチームだけを並べる（インラインは formset が絞る）
        if self.instance.pk:
            _narrow_choices(
                self.fields,
                "team",
                teams_in(WorldScope.real(self.instance.team.league.workspace_id)),
            )

    def _ensure_same_workspace(self, cleaned) -> bool:
        """選手のほかの在籍と同じワークスペースのチームか。違えば「チーム」欄のエラーにして False を返す。

        選手は在籍から1つのワークスペースに属す（選手に帰属の列は無い）。違うワークスペースのチームへ
        在籍を足すと、選手が複数の範囲に現れてしまう。送信全体を見るインラインは formset が検査する。
        """
        team = cleaned.get("team")
        if self.checked_with_declarations or team is None:
            return True
        others = _player_workspace_ids(cleaned.get("player"), excluding_stint_pk=self.instance.pk)
        if others and team.league.workspace_id not in others:
            self.add_error("team", _CROSS_WORKSPACE.format(name=team.name))
            return False
        return True

    def clean_number(self):
        return _clean_jersey(self.cleaned_data.get("number"))

    def clean_number_before_promotion(self):
        return _clean_jersey(self.cleaned_data.get("number_before_promotion"))

    def clean_signed_as(self):
        return self.cleaned_data.get("signed_as") or ContractStatus.REGISTERED.value

    def clean_acquired_via(self):
        # 空欄は不明（None）。推測で埋めない
        return self.cleaned_data.get("acquired_via") or None

    def clean(self):
        cleaned = super().clean()
        if not self._ensure_same_workspace(cleaned):
            return cleaned
        self._fill_from_year(cleaned)

        team = cleaned.get("team")
        number = cleaned.get("number")
        from_year = cleaned.get("from_year")
        if not (team and number is not None and from_year is not None):
            return cleaned

        try:
            candidate = DomainStint(
                team_id=team.id,
                number=JerseyNumber(number),
                from_year=from_year,
                to_year=cleaned.get("to_year"),
                signed_as=ContractStatus.from_label(cleaned.get("signed_as") or ContractStatus.REGISTERED.value),
                promoted_year=cleaned.get("promoted_year"),
                number_before_promotion=_jersey_or_none(cleaned.get("number_before_promotion")),
                acquired_via=AcquisitionRoute.from_label(cleaned["acquired_via"])
                if cleaned.get("acquired_via")
                else None,
            )
            # 区分と背番号の食い違い（支配下は 00・0〜99、育成は3桁の 100〜999）はドメインに判定させる
            candidate.ensure_number_matches_contract()
        except DomainError as error:
            raise forms.ValidationError(str(error)) from None

        # 昇格した在籍は2つの番号を持つので、どちらかが一致する他人の在籍を候補にする
        numbers = [n.value for n, _, _ in candidate.number_periods()]
        others = PlayerStint.objects.filter(
            Q(number__in=numbers) | Q(number_before_promotion__in=numbers), team=team
        ).select_related("player")
        if self.instance.pk:
            others = others.exclude(pk=self.instance.pk)
        # 選手の新規登録では、在籍と一緒に送られてくる選手がまだ保存されていない。
        # 未保存のまま絞り込むと落ちるうえ、そもそも既存の在籍を持たないので
        # 除外する必要も無い
        player = cleaned.get("player")
        if player is not None and player.pk:
            others = others.exclude(player=player)

        for other in others:
            existing = _to_domain_stint(other)
            shared = candidate.shared_number(existing)
            if shared is not None:
                raise forms.ValidationError(
                    f"背番号 {shared} は {other.player.name} が {existing} に"
                    f"使用しています。期間が重なる同じ背番号は登録できません。"
                )

        self._ensure_free_agency_consistent(candidate, player)
        self._ensure_draft_fits(candidate, player)
        self._ensure_registered_limit(team, candidate)
        return cleaned

    def _ensure_free_agency_consistent(self, candidate, player):
        """この在籍を保存した後の、その選手の在籍と保存済みの FA 宣言の整合（経路 FA の根拠・宣言した年の在籍）。

        期間の短縮や経路の変更で、宣言や FA 入団の根拠が崩れるのを保存前に弾く。判定はドメインの関数。
        """
        if self.checked_with_declarations or player is None or not player.pk:
            return
        others = _domain_stints_of(player, excluding_pk=self.instance.pk)
        ensure_player_free_agency_consistent(
            [*others, candidate], _domain_declarations_of(player), f"「{player.name}」"
        )

    def _ensure_draft_fits(self, candidate, player):
        """この在籍を保存した後の、その選手の最初の在籍の経路と保存済みのドラフトの整合（`ensure_draft_fits_career`）。

        選手の管理画面のインラインでは、送信後の在籍の全体とドラフトをフォームセットが検査する。
        """
        if self.checked_with_declarations or player is None or not player.pk:
            return
        others = _domain_stints_of(player, excluding_pk=self.instance.pk)
        ensure_player_draft_fits(_draft_of_player(player), [*others, candidate])

    def _ensure_registered_limit(self, team, candidate):
        """支配下の選手が新たにこのチームの人数に加わる場合、上限を超えないか確認する。

        育成のまま・退団済みの在籍は数えない。既に支配下として数えられている在籍の
        編集（背番号の変更など）は人数が増えないので通す。育成からの昇格は増える。
        """
        if not candidate.is_current or candidate.contract_now is not ContractStatus.REGISTERED:
            return

        if self.instance.pk:
            stored = PlayerStint.objects.filter(pk=self.instance.pk).first()
            counted = (
                stored is not None
                and stored.team_id == team.id
                and stored.to_year is None
                and _to_domain_stint(stored).contract_now is ContractStatus.REGISTERED
            )
            if counted:
                return  # 既に支配下として数えられている

        domain_team = DjangoTeamRepository(_row_scope(team.league)).find_by_id(team.id)
        league = DjangoLeagueRepository(_row_scope(team.league)).find_by_id(team.league_id)
        try:
            # 判定とメッセージはドメイン（Team）の出典を使う
            domain_team.ensure_room_for_registered(league.registered_player_limit)
        except DomainError as error:
            raise forms.ValidationError(str(error)) from None

    def _fill_from_year(self, cleaned):
        """加入年が空欄なら、選手のドラフト年の翌年（`DraftPick.entry_year`）で埋める。

        最初の在籍では加入年＝ドラフトの翌年になることがほとんどで、同じことを
        二度入力させる意味が無い。どちらも空欄のときだけ入力を求める。
        同じ画面で入力した未保存のドラフト年も使う（親フォームの未保存の選手から拾う）。
        """
        if cleaned.get("from_year") is not None:
            return

        draft = _draft_of_player(cleaned.get("player"))
        if draft is None:
            self.add_error(
                "from_year",
                "加入年を入力してください。選手にドラフト年が登録されていれば、空欄でもその翌年を使います。",
            )
            return

        cleaned["from_year"] = draft.entry_year


class PlayerStintFormSet(PlayerWorkspaceChoicesMixin, forms.BaseInlineFormSet):
    """同じ選手の在籍どうしの食い違いを見る。

    1行ずつの検証では、同時に送られた他の行が見えない。まとめて登録すると
    「同じチームに期間が重なって在籍している」経歴が通ってしまうため、
    ここで突き合わせる。

    別チームどうしの重なりは弾かない。シーズン途中の移籍では、移籍元と
    移籍先の在籍が同じ年を共有するのが普通のため。
    """

    def _construct_form(self, i, **kwargs):
        form = super()._construct_form(i, **kwargs)
        # 宣言との整合は、選手の管理画面の FA 宣言のフォームセットが送信後の全体で検査する
        form.checked_with_declarations = True
        return form

    def submitted_stints(self) -> list[DomainStint]:
        """送信された在籍（削除する行・空の行を除く）をドメインの在籍にしたもの。検証済みであること。"""
        stints = []
        for form in self.forms:
            data = getattr(form, "cleaned_data", None)
            if not data or data.get("DELETE"):
                continue
            team, number, from_year = data.get("team"), data.get("number"), data.get("from_year")
            if not (team and number is not None and from_year is not None):
                continue
            stints.append(
                DomainStint(
                    team_id=team.id,
                    number=JerseyNumber(number),
                    from_year=from_year,
                    to_year=data.get("to_year"),
                    signed_as=ContractStatus.from_label(data.get("signed_as") or ContractStatus.REGISTERED.value),
                    promoted_year=data.get("promoted_year"),
                    number_before_promotion=_jersey_or_none(data.get("number_before_promotion")),
                    acquired_via=AcquisitionRoute.from_label(data["acquired_via"])
                    if data.get("acquired_via")
                    else None,
                )
            )
        return stints

    captaincy_formset = None

    def link_captaincies(self, captaincy_formset) -> None:
        """主将在任のフォームセットをつなぐ（ワークスペースの突き合わせに使う）。"""
        self.captaincy_formset = captaincy_formset

    def clean(self):
        super().clean()
        _ensure_rows_share_workspace(self, self.captaincy_formset)
        if any(self.errors):
            return

        checked = []
        for form in self.forms:
            data = getattr(form, "cleaned_data", None)
            if not data or data.get("DELETE"):
                continue
            team = data.get("team")
            number = data.get("number")
            from_year = data.get("from_year")
            if not (team and number is not None and from_year is not None):
                continue

            current = DomainStint(
                team_id=team.id,
                number=JerseyNumber(number),
                from_year=from_year,
                to_year=data.get("to_year"),
            )
            for other in checked:
                if other.team_id == current.team_id and current.overlaps(other):
                    form.add_error(
                        "from_year",
                        f"{team.name} の在籍 {other} と期間が重なっています。"
                        f"同じチームに同時に2度在籍することはできません。",
                    )
                    break
            checked.append(current)

        # ドラフト（本指名か育成か）と最初の在籍の経路の整合は、送信後の在籍の全体と、同じ画面で入力した
        # 選手のドラフトで見る（1行ずつでは最初の在籍が分からない）
        ensure_player_draft_fits(_draft_of_player(self.instance), self.submitted_stints())


class PlayerStintInline(RealDataOnlyMixin, admin.TabularInline):
    """在籍。所属と背番号はここが出典で、移籍すると行が増える。"""

    model = PlayerStint
    form = PlayerStintForm
    formset = PlayerStintFormSet
    extra = 0
    fields = (
        "team",
        "number",
        "from_year",
        "to_year",
        "signed_as",
        "promoted_year",
        "number_before_promotion",
        "acquired_via",
    )
    ordering = ("-from_year",)
    autocomplete_fields = ("team",)
    verbose_name_plural = "在籍（経歴）"


class FreeAgentDeclarationForm(forms.ModelForm):
    """FA 宣言の入力検証。判定はドメインの `ensure_declarations_valid` に任せる。

    同じ年に2回・宣言した年にどこにも在籍していない、を弾く（管理画面はドメインを経由しないため）。
    選手の管理画面のインラインでは、送信後の在籍と宣言の全体をフォームセットが検査するので、
    ここでは見ない（保存済みの在籍と突き合わせると、同じ送信での在籍の追加・削除を見落とす）。
    """

    checked_with_stints = False

    class Meta:
        model = PlayerFreeAgentDeclaration
        fields = "__all__"

    def clean(self):
        cleaned = super().clean()
        player, year, kind = cleaned.get("player"), cleaned.get("year"), cleaned.get("kind")
        if self.checked_with_stints or player is None or not player.pk or year is None or not kind:
            return cleaned

        declarations = [
            *_domain_declarations_of(player, excluding_pk=self.instance.pk),
            DomainFreeAgentDeclaration(year=year, kind=FreeAgencyKind.from_label(kind)),
        ]
        try:
            ensure_declarations_valid(_domain_stints_of(player), declarations, f"「{player.name}」")
        except DomainError as error:
            raise forms.ValidationError(str(error)) from None
        return cleaned


class FreeAgentDeclarationFormSet(forms.BaseInlineFormSet):
    """FA 宣言の行を、**送信後の在籍と宣言の全体**でまとめて検査する（H2）。

    宣言の削除・年の変更・在籍の削除や期間の短縮は、1行ずつの検証や保存済みの値との突き合わせでは
    見えない（削除する行は検証から外れる）。`PlayerAdmin` が在籍のフォームセットをつなぐので、
    削除する行を除き変更後の値から選手を組み立て、ドメインの規則（宣言した年に在籍がある・経路 FA の根拠）で見る。
    """

    stint_formset = None

    def link_stints(self, stint_formset) -> None:
        """在籍のフォームセットをつなぐ。つないだら、1行ずつの在籍との突き合わせはフォームセットが引き受ける。"""
        self.stint_formset = stint_formset
        for form in self.forms:
            form.checked_with_stints = True  # type: ignore[attr-defined]  # FreeAgentDeclarationForm の属性

    def clean(self):
        super().clean()
        stint_formset = self.stint_formset
        if stint_formset is None or any(self.errors):
            return
        # 在籍側に入力エラーがあれば、そちらを先に直してもらう（検証済みの値が無い）
        if not stint_formset.is_valid():
            return

        declarations = []
        for form in self.forms:
            data = getattr(form, "cleaned_data", None)
            if not data or data.get("DELETE") or data.get("year") is None or not data.get("kind"):
                continue
            declarations.append(
                DomainFreeAgentDeclaration(year=data["year"], kind=FreeAgencyKind.from_label(data["kind"]))
            )
        name = f"「{self.instance.name}」" if self.instance.pk else ""
        ensure_player_free_agency_consistent(stint_formset.submitted_stints(), declarations, name)


class FreeAgentDeclarationInline(admin.TabularInline):
    """FA 宣言。結果（残留・移籍）は在籍から導くので入力しない。"""

    model = PlayerFreeAgentDeclaration
    form = FreeAgentDeclarationForm
    formset = FreeAgentDeclarationFormSet
    extra = 0
    fields = ("year", "kind")
    ordering = ("-year",)
    verbose_name_plural = "FA 宣言"


class CaptaincyForm(forms.ModelForm):
    """主将在任の入力検証。PlayerStintForm と同じパターンの相互検証。"""

    # 選手の管理画面のインラインでは、送信後の在籍とのワークスペースの突き合わせをフォームセットが行う
    # （保存済みの在籍だけを見ると、同じ送信で足す在籍を見落とす）。単独で使うときは False
    checked_with_stints = False

    class Meta:
        model = Captaincy
        fields = "__all__"

    def clean(self):
        cleaned = super().clean()
        team, from_year = cleaned.get("team"), cleaned.get("from_year")
        # 主将は、その選手が在籍しているチームと同じワークスペースのチームの主将でなければならない
        workspaces = (
            set()
            if self.checked_with_stints
            else _player_workspace_ids(cleaned.get("player"), excluding_captaincy_pk=self.instance.pk)
        )
        if team is not None and workspaces and team.league.workspace_id not in workspaces:
            self.add_error("team", _CROSS_WORKSPACE.format(name=team.name))
            return cleaned
        if not (team and from_year is not None):
            return cleaned

        try:
            candidate = DomainCaptaincy(team_id=team.id, from_year=from_year, to_year=cleaned.get("to_year"))
        except DomainError as error:
            raise forms.ValidationError(str(error)) from None

        # 同チームの他選手の在任と重ならないか(PlayerStintForm と同じ突き合わせ)
        others = Captaincy.objects.filter(team=team).select_related("player")
        if self.instance.pk:
            others = others.exclude(pk=self.instance.pk)
        player = cleaned.get("player")
        if player is not None and player.pk:
            others = others.exclude(player=player)

        for other in others:
            existing = DomainCaptaincy(team_id=other.team_id, from_year=other.from_year, to_year=other.to_year)
            if candidate.overlaps(existing):
                raise forms.ValidationError(
                    f"{other.player.name} が {existing} に主将です。期間が重なる主将指名はできません。"
                )

        # 現在(to_year 空)の指名は、現在在籍中の選手にしか行えない。過去の在任行
        # (to_year 入力済み、退団済み選手の履歴入力など)には適用しない
        if (
            cleaned.get("to_year") is None
            and player is not None
            and player.pk
            and not PlayerStint.objects.filter(player=player, team=team, to_year__isnull=True).exists()
        ):
            raise forms.ValidationError(f"{player.name} は現在「{team.name}」に在籍していないため主将にできません。")
        return cleaned


class CaptaincyFormSet(PlayerWorkspaceChoicesMixin, forms.BaseInlineFormSet):
    """同じ選手の複数行を同時編集する際のクロスチェック(PlayerStintFormSet と同型)。"""

    stint_formset = None

    def link_stints(self, stint_formset) -> None:
        """在籍のフォームセットをつなぐ。つないだら、在籍とのワークスペースの突き合わせは送信後の全体で行う。"""
        self.stint_formset = stint_formset
        for form in self.forms:
            form.checked_with_stints = True  # type: ignore[attr-defined]  # CaptaincyForm の属性

    def clean(self):
        super().clean()
        _ensure_rows_share_workspace(self, self.stint_formset)
        if any(self.errors):
            return

        checked = []
        for form in self.forms:
            data = getattr(form, "cleaned_data", None)
            if not data or data.get("DELETE"):
                continue
            team = data.get("team")
            from_year = data.get("from_year")
            if not (team and from_year is not None):
                continue

            current = DomainCaptaincy(team_id=team.id, from_year=from_year, to_year=data.get("to_year"))
            for other in checked:
                if other.team_id == current.team_id and current.overlaps(other):
                    form.add_error("from_year", f"{team.name} の主将在任 {other} と期間が重なっています。")
                    break
            checked.append(current)


class CaptaincyInline(RealDataOnlyMixin, admin.TabularInline):
    """主将在任歴。在籍とは別軸の期間として管理する。"""

    model = Captaincy
    form = CaptaincyForm
    formset = CaptaincyFormSet
    extra = 0
    fields = ("team", "from_year", "to_year")
    ordering = ("-from_year",)
    autocomplete_fields = ("team",)
    verbose_name_plural = "主将在任歴"


class PlayerForm(DomainCheckedForm):
    class Meta:
        model = Player
        fields = "__all__"

    def build_value_object(self, cleaned):
        draft_year, draft_round = cleaned.get("draft_year"), cleaned.get("draft_round")
        is_development = cleaned.get("is_development_draft", False)
        # 年の無い順位・育成を拒む規則は domain（`DraftPick.from_inputs`。サイトと共通）。年の欄のエラーにする
        try:
            draft = DraftPick.from_inputs(draft_year, draft_round, is_development=is_development)
        except DomainError as error:
            if draft_year is None:
                raise forms.ValidationError({"draft_year": str(error)}) from None
            raise
        return DomainProfile(
            birth_date=cleaned.get("birth_date"),
            throws=cleaned.get("throws") or None,
            bats=cleaned.get("bats") or None,
            height_cm=cleaned.get("height_cm"),
            weight_kg=cleaned.get("weight_kg"),
            birthplace=cleaned.get("birthplace", ""),
            draft=draft,
            high_school=cleaned.get("high_school", ""),
            university=cleaned.get("university", ""),
            corporate_team=cleaned.get("corporate_team", ""),
            nationality=cleaned.get("nationality", ""),
            is_foreign_player=cleaned.get("is_foreign_player", False),
        )


@admin.register(Player)
class PlayerAdmin(RealDataOnlyMixin, admin.ModelAdmin):
    form = PlayerForm
    list_display = ("name", "position", "current_team", "current_number", "appearances")
    list_filter = (
        "position",
        "stints__team__league__workspace",
        ("stints__team__league", RealDataRelatedFieldListFilter),
        ("stints__team", RealDataRelatedFieldListFilter),
    )
    search_fields = ("name", "name_kana", "back_name")
    ordering = ("name",)
    inlines = [PlayerStintInline, FreeAgentDeclarationInline, CaptaincyInline]

    fieldsets = (
        (
            None,
            {
                "fields": ("name", "name_kana", "back_name", "position"),
                "description": "所属チームと背番号は下の「在籍」で管理します。",
            },
        ),
        (
            "プロフィール",
            {
                "description": "分かっているものだけ入力してください。すべて任意です。",
                "fields": (
                    "birth_date",
                    ("throws", "bats"),
                    ("height_cm", "weight_kg"),
                    "birthplace",
                    ("draft_year", "draft_round", "is_development_draft"),
                    "nationality",
                    "is_foreign_player",
                ),
            },
        ),
        (
            "プロ入り前の経歴",
            {
                "description": "通った所だけ入力してください。高校からそのままプロ、"
                "大学を経ずに社会人へ、といった順路にも対応します。",
                "fields": ("high_school", "university", "corporate_team"),
            },
        ),
    )

    def _create_formsets(self, request, obj, change):
        """在籍と FA 宣言・主将在任のフォームセットをつなぐ。

        宣言の検査は、送信後の在籍の全体を見る必要がある（宣言の削除・在籍の削除や短縮が互いの根拠を壊すため）。
        """
        formsets, inline_instances = super()._create_formsets(request, obj, change)
        stints = next((fs for fs in formsets if fs.model is PlayerStint), None)
        for formset in formsets:
            if isinstance(formset, FreeAgentDeclarationFormSet | CaptaincyFormSet) and stints is not None:
                formset.link_stints(stints)
        captaincies = next((fs for fs in formsets if fs.model is Captaincy), None)
        if stints is not None and captaincies is not None:
            stints.link_captaincies(captaincies)
        return formsets, inline_instances

    @admin.display(description="現在の所属")
    def current_team(self, obj):
        stint = self._current(obj)
        return stint.team.name if stint else "—"

    @admin.display(description="背番号")
    def current_number(self, obj):
        stint = self._current(obj)
        return stint.number if stint else "—"

    @admin.display(description="出場試合")
    def appearances(self, obj):
        """成績そのものは試合側にあるので、ここでは出場数だけ示す（公式戦だけ。チームの試合数と揃える）。"""
        regular = stage_condition("game", StageFilter.REGULAR_ONLY)
        count = obj.game_batting.filter(regular).count() + obj.game_pitching.filter(regular).count()
        return count or "—"

    @staticmethod
    def _current(obj):
        for stint in obj.stints.all():
            if stint.to_year is None:
                return stint
        return None

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("stints__team")


@admin.register(PlayerStint)
class PlayerStintAdmin(BlockedDeletionAdmin, RealDataOnlyMixin, GroupedAdminMixin, admin.ModelAdmin):
    """在籍そのものの一覧。チームごとの名簿として使える。"""

    group_ordering = ("team__league__display_order", "team__league__name", "team__name")

    form = PlayerStintForm
    list_display = ("number_label", "player", "workspace", "from_year", "to_year")
    list_filter = (
        "team__league__workspace",
        ("team__league", RealDataRelatedFieldListFilter),
        ("team", RealDataRelatedFieldListFilter),
    )
    search_fields = ("player__name",)
    # 背番号は文字列の列なので、そのままでは「10」が「2」より前に来る。数値に直した並びのキーを足す
    # （00 → 0 → 1 …。出典は `JerseyNumber.sort_key`。食い違わないことを test_admin が確かめる）
    # （注釈は `ordering` の検査に通らないので get_ordering で返す）
    list_select_related = ("player", "team", "team__league", "team__league__workspace")
    autocomplete_fields = ("player", "team")

    # 行をチームごとに区切る
    group_by = staticmethod(lambda s: f"{s.team.league.name} · {s.team.name}")

    def get_ordering(self, request):
        return ("team__league__name", "team__name", "number_order")

    def get_queryset(self, request):
        # 親の get_queryset は並び（注釈の前に評価される）を付けてしまうので、注釈を付けてから並べる。
        # 親（RealDataOnlyMixin）を通らないので、実データの行だけに絞るのもここで行う
        rows = self.model._default_manager.get_queryset()
        scoped = _real_queryset(self.model)
        if scoped is not None:
            rows = rows.filter(pk__in=scoped.values("pk"))
        return rows.annotate(
            number_order=Case(
                When(number=JerseyNumber.DOUBLE_ZERO, then=Value(-1)),
                default=Cast("number", IntegerField()),
                output_field=IntegerField(),
            )
        ).order_by(*self.get_ordering(request))

    @admin.display(description="背番号", ordering="number_order")
    def number_label(self, obj):
        return obj.number

    @admin.display(description="ワークスペース", ordering="team__league__workspace__name")
    def workspace(self, obj):
        # チーム → リーグ経由（在籍は自分では帰属を持たない）
        return obj.team.league.workspace

    def deletion_blocker(self, obj) -> str:
        """この在籍を消すと FA 宣言（宣言した年の在籍）か FA 入団の根拠が崩れるなら、その理由。消せるなら空文字。

        消せない在籍は削除の導線ごと出さず、一括削除でも何も消さない（`BlockedDeletionAdmin`）。
        """
        try:
            ensure_player_free_agency_consistent(
                _domain_stints_of(obj.player, excluding_pk=obj.pk),
                _domain_declarations_of(obj.player),
                f"「{obj.player.name}」",
            )
        except forms.ValidationError as error:
            return str(error.message)
        return ""


class DerivedLinesInline(admin.TabularInline):
    """打席から導いた明細。閲覧だけで、追加・変更・削除はさせない。

    管理画面はスコアブックの照合（`Game` 集約の保存前の検査）を通らない。ここで数字を
    書き換えると、打席の記録と食い違ったまま保存されて集計がずれる。
    記録はスコアブック画面（`/games/<id>/edit/`）で行う。
    """

    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class GameInningScoreInline(DerivedLinesInline):
    """イニングスコア。打席から導いた値。"""

    model = GameInningScore


class GameBattingLineInline(DerivedLinesInline):
    """試合ごとの打撃成績。打席から導いた値。"""

    model = GameBattingLine


class GamePitchingLineInline(DerivedLinesInline):
    """試合ごとの投球成績。打席から導いた値。"""

    model = GamePitchingLine


class GameFieldingLineInline(DerivedLinesInline):
    """試合ごとの守備成績。打席から導いた値。"""

    model = GameFieldingLine


class GameForm(DomainCheckedForm):
    """試合の編集。開始・終了の時刻と中断は、ドメインの `GameTimes` に検査させる。

    片方だけの時刻・終了が開始より前などの規則は値オブジェクトが唯一の出典で、ここには書かない。
    """

    class Meta:
        model = Game
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 空欄は0分（中断なし）。モデルの既定値がそのまま入る
        self.fields["interruption_minutes"].required = False

    def clean(self):
        cleaned = super().clean()
        # 追加画面の対戦カードは同じワークスペースのチームどうしでなければならない
        # （登録済みの試合は対戦カードが読み取り専用なので、欄が無く検査しない）
        home, away = cleaned.get("home_team"), cleaned.get("away_team")
        if home is not None and away is not None and home.league.workspace_id != away.league.workspace_id:
            self.add_error("away_team", _CROSS_WORKSPACE.format(name=away.name))
        return cleaned

    def clean_interruption_minutes(self):
        # 空欄は0分。None のまま保存すると NOT NULL 制約で落ちる
        return self.cleaned_data.get("interruption_minutes") or 0

    def build_value_object(self, cleaned):
        return GameTimes.from_inputs(
            cleaned.get("start_time"),
            cleaned.get("end_time"),
            cleaned.get("ends_next_day", False),
            cleaned.get("interruption_minutes"),
        )


@admin.register(Game)
class GameAdmin(RealDataOnlyMixin, admin.ModelAdmin):
    """試合。チームの勝敗も選手の通算成績も、すべてここから集計される。

    管理画面で直せるのは試合日・シーズンなどの基本情報だけ。得点・イニングスコア・
    成績は打席から導く値なので閲覧のみ（スコアブック画面で記録する）。
    """

    form = GameForm
    list_display = ("played_on", "year", "workspace", "matchup", "score", "result")
    list_filter = ("year", "home_team__league__workspace", ("home_team__league", RealDataRelatedFieldListFilter))
    date_hierarchy = "played_on"
    ordering = ("-played_on",)
    list_select_related = ("home_team", "home_team__league", "home_team__league__workspace", "away_team")
    inlines = [GameInningScoreInline, GameBattingLineInline, GamePitchingLineInline, GameFieldingLineInline]
    fieldsets = (
        (
            None,
            {
                "description": "試合の記録（打席・得点・成績）はスコアブック画面（試合詳細の「記録を編集」、"
                "試合の登録は「試合を登録」）で行います。管理画面で直せるのは基本情報だけです。"
                "試合時間は「終了 − 開始 − 中断」から自動で決まります。",
                "fields": (
                    "year",
                    "played_on",
                    "home_team",
                    "away_team",
                    "start_time",
                    "end_time",
                    "ends_next_day",
                    "interruption_minutes",
                    "attendance",
                ),
            },
        ),
        (
            "得点（打席から導いた値）",
            {
                "description": "打席の記録から自動で決まるため、ここでは直せません。",
                "fields": ("home_score", "away_score"),
            },
        ),
    )
    readonly_fields = ("home_score", "away_score")

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        # 観客数が本拠地の収容人数を超えていても保存は止めず、見直しを促す（警告の経路は保存 API と同じ）
        for warning in build_recording_service(_row_scope(obj.home_team.league)).attendance_warnings_for(
            obj.home_team_id, obj.attendance
        ):
            self.message_user(request, warning.message, level=messages.WARNING)

    def get_readonly_fields(self, request, obj=None):
        """登録済みの試合は対戦カードも固定する。

        明細はホーム・ビジターのどちらに属すかを持つので、後から入れ替えると食い違う。
        """
        fixed = super().get_readonly_fields(request, obj)
        return (*fixed, "home_team", "away_team") if obj is not None else fixed

    @admin.display(description="ワークスペース", ordering="home_team__league__workspace__name")
    def workspace(self, obj):
        # ホームチーム → リーグ経由（試合は自分では帰属を持たない）
        return obj.home_team.league.workspace

    @admin.display(description="対戦")
    def matchup(self, obj):
        return f"{obj.home_team.name} vs {obj.away_team.name}"

    @admin.display(description="スコア")
    def score(self, obj):
        return f"{obj.home_score} - {obj.away_score}"

    @admin.display(description="結果")
    def result(self, obj):
        # 勝敗の判定はドメインの関数が唯一の出典。ここで比較を書き直さない
        winner = winning_team_id(obj.home_team_id, obj.away_team_id, obj.home_score, obj.away_score)
        if winner is None:
            return "引分"
        return f"{(obj.home_team if winner == obj.home_team_id else obj.away_team).name} の勝ち"


def _new_workspace_key() -> str:
    """まだ使われていない key を作る。推測されないよう OS の乱数源（`SystemRandom`）で引く。"""
    rng = random.SystemRandom()
    while True:
        key = generate_workspace_key(rng)
        if not Workspace.objects.filter(key=key).exists():
            return key


class WorkspaceForm(forms.ModelForm):
    """ワークスペースの作成。key は空欄ならサーバーが作る（登録後は読み取り専用で変えられない）。"""

    class Meta:
        model = Workspace
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 登録済みの行では key が読み取り専用になり、欄が無い
        if "key" in self.fields:
            self.fields["key"].required = False
            self.fields[
                "key"
            ].help_text = "空欄なら自動で作ります（推測されない10文字）。URL に入るので、作成後は変えられません。"

    def clean_key(self):
        key = (self.cleaned_data.get("key") or "").strip()
        if not key:
            return _new_workspace_key()
        try:
            return validate_workspace_key(key)
        except DomainError as error:
            raise forms.ValidationError(str(error)) from None


class WorkspaceEditorFormSet(forms.BaseInlineFormSet):
    """編集者の行。持ち主は編集者に入れない（持ち主は全権を持つ。移行 0061 も持ち主を除いた）。"""

    def clean(self):
        super().clean()
        owner_id = self.instance.owner_id
        if owner_id is None:
            return
        for form in self.forms:
            data = getattr(form, "cleaned_data", None)
            user = data.get("user") if data and not data.get("DELETE") else None
            if user is not None and user.pk == owner_id:
                form.add_error("user", "持ち主は編集者に加えられません。持ち主はもともと全権を持っています。")


class WorkspaceEditorInline(admin.TabularInline):
    """編集者。持ち主のほかに、ワークスペース全体を編集できるユーザー。"""

    model = WorkspaceEditor
    formset = WorkspaceEditorFormSet
    extra = 0
    verbose_name_plural = "編集者"


@admin.register(Workspace)
class WorkspaceAdmin(admin.ModelAdmin):
    """ワークスペース。空の DB で運営者が最初の1つを作る入口（リーグ・球場はこれに属す）。"""

    form = WorkspaceForm
    list_display = ("name", "key", "owner", "visibility", "created_at")
    list_filter = ("visibility",)
    search_fields = ("name", "key")
    inlines = [WorkspaceEditorInline]

    def get_readonly_fields(self, request, obj=None):
        """key は URL に入る識別子なので、作った後は変えさせない。"""
        fixed = super().get_readonly_fields(request, obj)
        return (*fixed, "key") if obj is not None else fixed
