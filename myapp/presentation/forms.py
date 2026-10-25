"""入力フォーム。

これまで views.py が request.POST.get() の生文字列をそのままモデルへ渡していたため、
数値欄に文字列が入ると例外になっていた。型変換と必須チェックはここで完結させ、
アプリケーション層には検証済みの値だけを渡す。
"""

from collections.abc import Sequence
from dataclasses import asdict
from typing import Any, cast

from django import forms
from django.core.exceptions import ValidationError

from ..application.dto import LeagueTeams, LineupSlot, ProfileInput
from ..domain.entities import FieldingError, PlateAppearance, RunnerAdvance
from ..domain.exceptions import DomainError, InvalidJerseyNumber, InvalidPosition
from ..domain.pennant.draft_order import DraftOrderRule
from ..domain.pennant.postseason import PostseasonFormat
from ..domain.pennant.world import MAX_NAME_LENGTH, MAX_SEED, MAX_SOURCE_LEAGUES, MAX_START_YEAR
from ..domain.value_objects import (
    BACK_NAME_MAX_LENGTH,
    PROFILE_TEXT_MAX_LENGTH,
    AdvanceReason,
    Base,
    ContractStatus,
    ErrorKind,
    FieldingPosition,
    GameTimes,
    Handedness,
    JerseyNumber,
    PlateAppearanceResult,
    Position,
    Season,
)

POSITION_CHOICES = [(position.value, position.value) for position in Position]
# 投打の選択肢はドメインの Handedness が唯一の出典
HANDEDNESS_CHOICES = [(label, label) for label in Handedness.labels()]
# 試合で就いた守備位置。未記録も許すので空の選択肢を先頭に置く
FIELDING_POSITION_CHOICES = [("", "—")] + [(p.value, p.value) for p in FieldingPosition]
# 1試合の回数。延長を含めても12回までを入力できるようにする
MAX_INNINGS = 12


class JerseyNumberField(forms.CharField):
    """背番号の入力欄。表記のまま（「00」を「0」にしない）ドメインの `JerseyNumber` で検証する。

    受け付ける範囲と文言はドメインが唯一の出典で、ここには書かない。
    """

    def __init__(self, **kwargs: Any) -> None:
        # 入力欄はテンプレートが書くので、ここにウィジェットの属性（入力の目安の pattern）は持たない
        super().__init__(max_length=3, strip=True, **kwargs)

    def clean(self, value: Any) -> str:
        text = super().clean(value)
        try:
            return JerseyNumber(text).value
        except InvalidJerseyNumber as error:
            raise forms.ValidationError(str(error)) from error


class PlayerUpdateForm(forms.Form):
    """選手情報の更新。基本情報の項目は登録時と同じ（契約区分を除く）。

    契約区分は更新では変えない（昇格でしか変わらない）ので、欄を持たない。
    """

    name = forms.CharField(label="選手名", max_length=100)
    number = JerseyNumberField(label="背番号")
    position = forms.ChoiceField(
        label="守備位置",
        choices=POSITION_CHOICES,
        initial=Position.INFIELDER.value,
    )


class PlayerRegistrationForm(PlayerUpdateForm):
    """新入団選手の登録。基本情報に加えて、加入時の契約区分を選ぶ。"""

    # 選択肢はドメインの ContractStatus が唯一の出典。省略時は支配下
    contract = forms.ChoiceField(
        label="契約区分",
        choices=[(label, label) for label in ContractStatus.labels()],
        initial=ContractStatus.REGISTERED.value,
        required=False,
    )

    def clean_contract(self) -> str:
        return self.cleaned_data.get("contract") or ContractStatus.REGISTERED.value


# 選手の編集画面のプロフィールの節の目印（隠し欄）。これが送られてきた要求でだけプロフィールを更新する。
# 退団・主将などのボタンや、節を持たない古いフォームの送信で、プロフィールを空で上書きしないため
PROFILE_SECTION_MARKER = "profile_section"


class PlayerProfileForm(forms.Form):
    """選手のプロフィール（14項目。ドラフトは年・順位・育成の3欄）。すべて任意。

    欄の型と長さだけをここで見る。値の規則（身長・体重の上限、ドラフトの年と順位、年の無い順位・育成、
    育成ドラフトと最初の在籍の経路）は domain が出典で、`RosterService.update_player` の中で検査される
    （管理画面の選手の編集と同じ規則）。投打の選択肢はドメインの `Handedness` が出典。
    """

    name_kana = forms.CharField(label="よみがな", max_length=PROFILE_TEXT_MAX_LENGTH, required=False)
    back_name = forms.CharField(label="背ネーム", max_length=BACK_NAME_MAX_LENGTH, required=False)
    birth_date = forms.DateField(
        label="生年月日", required=False, widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")
    )
    throws = forms.ChoiceField(label="投", choices=[("", "—")] + HANDEDNESS_CHOICES, required=False)
    bats = forms.ChoiceField(label="打", choices=[("", "—")] + HANDEDNESS_CHOICES, required=False)
    height_cm = forms.IntegerField(label="身長(cm)", required=False)
    weight_kg = forms.IntegerField(label="体重(kg)", required=False)
    birthplace = forms.CharField(label="出身地", max_length=PROFILE_TEXT_MAX_LENGTH, required=False)
    draft_year = forms.IntegerField(label="ドラフト年", required=False)
    draft_round = forms.IntegerField(label="ドラフト順位", required=False)
    is_development_draft = forms.BooleanField(label="育成ドラフト", required=False)
    high_school = forms.CharField(label="出身高校", max_length=PROFILE_TEXT_MAX_LENGTH, required=False)
    university = forms.CharField(label="出身大学", max_length=PROFILE_TEXT_MAX_LENGTH, required=False)
    corporate_team = forms.CharField(label="出身社会人チーム", max_length=PROFILE_TEXT_MAX_LENGTH, required=False)
    nationality = forms.CharField(label="国籍", max_length=PROFILE_TEXT_MAX_LENGTH, required=False)
    is_foreign_player = forms.BooleanField(label="外国人選手（外国人枠の対象）", required=False)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # 見た目は画面の他の欄（Bootstrap）に揃える。欄はテンプレートが {{ form.欄 }} で描く
        for field in self.fields.values():
            if isinstance(field, forms.BooleanField):
                field.widget.attrs["class"] = "form-check-input"
            elif isinstance(field, forms.ChoiceField):
                field.widget.attrs["class"] = "form-select"
            else:
                field.widget.attrs["class"] = "form-control"

    @classmethod
    def initial_of(cls, profile: ProfileInput) -> "PlayerProfileForm":
        """保存済みのプロフィールを初期値にした（未送信の）フォーム。"""
        return cls(initial=asdict(profile))

    def to_input(self) -> ProfileInput:
        """検証済みの値を application に渡す形にする。`is_valid()` の後に呼ぶ。"""
        return ProfileInput(**self.cleaned_data)


class GameForm(forms.Form):
    """試合の基本情報。

    得点は受け取らない（打席から導く値。登録時は 0-0 で作り、スコアブックの保存で決まる）。
    対戦カードの妥当性（同一チーム同士でないか）はドメインが判定するので、
    ここでは型と必須だけを見る。
    """

    year = forms.IntegerField(label="シーズン", min_value=1900, max_value=2100)
    played_on = forms.DateField(label="試合日", widget=forms.DateInput(attrs={"type": "date"}))
    home_team = forms.IntegerField(label="ホーム", widget=forms.HiddenInput)
    away_team = forms.IntegerField(label="ビジター", widget=forms.HiddenInput)


# 打席まわりの選択肢は、どれもドメインの値オブジェクトが唯一の出典。
# ここに文字列を並べ直すと、種別を増やしたときに片方だけ古いまま静かにずれる。
PLATE_APPEARANCE_RESULT_CHOICES = [(r.value, r.value) for r in PlateAppearanceResult]
ADVANCE_REASON_CHOICES = [(r.value, r.value) for r in AdvanceReason]
ERROR_KIND_CHOICES = [(k.value, k.value) for k in ErrorKind]
# 塁は順序そのものが意味を持つ（大小比較が「進んだか」）ため数値で受け取る
BASE_CHOICES = [(base.value, base.label) for base in Base]
# 打球の処理経路（スコアブックの 6-3）は守備位置をこの記号で連ねた1列で受け取る
FIELDED_BY_SEPARATOR = "-"


class ScorebookGameForm(forms.Form):
    """スコアブックを保存するときの試合の基本情報。

    `GameForm` と違って**得点を受け取らない**。打席から導ける値なので、受け取ると
    「記録と食い違う得点」を保存できてしまう（導出できるものは入力させない）。
    """

    year = forms.IntegerField(label="シーズン", min_value=1900, max_value=2100)
    played_on = forms.DateField(label="試合日", widget=forms.DateInput(attrs={"type": "date"}))
    home_team = forms.IntegerField(label="ホーム", widget=forms.HiddenInput)
    away_team = forms.IntegerField(label="ビジター", widget=forms.HiddenInput)
    # 打席からは導けない手入力の事実。空は未記録（キーが無いのはここでなく API が弾く）
    start_time = forms.TimeField(label="開始時刻", required=False)
    end_time = forms.TimeField(label="終了時刻", required=False)
    ends_next_day = forms.BooleanField(label="終了が翌日", required=False)
    interruption_minutes = forms.IntegerField(label="中断時間（分）", min_value=0, required=False)
    attendance = forms.IntegerField(label="観客数", min_value=0, required=False)

    def to_times(self) -> GameTimes | None:
        """開始・終了・翌日・中断から `GameTimes` を組み立てる。両方空なら未記録（None）。

        規則（終了は開始より後、中断は長さより短い、など）はドメインが唯一の出典で、
        ここは `DomainError` を `ValidationError` に訳すだけ。
        """
        data = self.cleaned_data
        try:
            return GameTimes.from_inputs(
                data.get("start_time"),
                data.get("end_time"),
                data.get("ends_next_day", False),
                data.get("interruption_minutes"),
            )
        except DomainError as error:
            raise ValidationError(str(error)) from None

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        # 時刻や数の型が不正なら、先にそのエラーを出す（ここでは重ねない）
        if not any(name in self.errors for name in ("start_time", "end_time", "interruption_minutes")):
            try:
                cleaned["times"] = self.to_times()
            except ValidationError as error:
                self.add_error("end_time", error)
        return cleaned


class LineupSlotForm(forms.Form):
    """打順の1枠。誰が何番でどこを守ったか。成績は含めない（打席から導く）。"""

    team_id = forms.IntegerField(widget=forms.HiddenInput)
    player_id = forms.IntegerField(widget=forms.HiddenInput)
    batting_order = forms.IntegerField(label="打順", min_value=1, max_value=9)
    slot_sequence = forms.IntegerField(
        label="交代",
        min_value=0,
        max_value=9,
        required=False,
        help_text="0 がスタメン。1以上は同じ打順への途中出場。",
    )
    fielding_position = forms.ChoiceField(label="守備位置", choices=FIELDING_POSITION_CHOICES, required=False)
    entered_inning = forms.IntegerField(
        label="出場した回",
        min_value=1,
        max_value=MAX_INNINGS,
        required=False,
        help_text="途中出場の行だけ。守備固めなど、入った半回（回と表裏）。代打・代走は打席から導くので空欄でよい。",
    )
    entered_is_bottom = forms.BooleanField(label="出場したのは裏", required=False)
    entered_batter = forms.IntegerField(
        label="出場した打者番号",
        min_value=1,
        required=False,
        help_text="その半回で何人目の打者から出場したか（1始まり）。半回の頭からなら 1。",
    )

    def to_slot(self) -> LineupSlot:
        return LineupSlot(
            team_id=self.cleaned_data["team_id"],
            player_id=self.cleaned_data["player_id"],
            batting_order=self.cleaned_data["batting_order"],
            slot_sequence=self.cleaned_data.get("slot_sequence") or 0,
            fielding_position=FieldingPosition.from_label(self.cleaned_data.get("fielding_position") or ""),
            entered_inning=self.cleaned_data.get("entered_inning"),
            entered_is_bottom=bool(self.cleaned_data.get("entered_is_bottom")),
            entered_batter=self.cleaned_data.get("entered_batter") or 1,
        )


class RunnerAdvanceForm(forms.Form):
    """走者1人ぶんの進塁。打者自身も走者として送られてくる（進塁前が「打者席」）。"""

    runner_id = forms.IntegerField(widget=forms.HiddenInput)
    from_base = forms.TypedChoiceField(label="進塁前", choices=BASE_CHOICES, coerce=int)
    to_base = forms.TypedChoiceField(label="進塁後", choices=BASE_CHOICES, coerce=int)
    reason = forms.ChoiceField(label="理由", choices=ADVANCE_REASON_CHOICES)
    error_index = forms.IntegerField(
        label="失策の位置",
        min_value=0,
        required=False,
        help_text="失策に起因する進塁なら、同じ打席の何番目の失策か。",
    )

    def to_advance(self) -> RunnerAdvance:
        """進塁として組み立てる。塁と理由の食い違いはドメインが弾く。"""
        return RunnerAdvance(
            runner_id=self.cleaned_data["runner_id"],
            from_base=Base(self.cleaned_data["from_base"]),
            to_base=Base(self.cleaned_data["to_base"]),
            reason=AdvanceReason.from_label(self.cleaned_data["reason"]),
            error_index=self.cleaned_data.get("error_index"),
        )


class FieldingErrorForm(forms.Form):
    """失策。誰がどこで何をしたか。"""

    player_id = forms.IntegerField(widget=forms.HiddenInput)
    position = forms.ChoiceField(label="守備位置", choices=FIELDING_POSITION_CHOICES)
    kind = forms.ChoiceField(label="失策の種類", choices=ERROR_KIND_CHOICES)

    def clean_position(self) -> str:
        position = self.cleaned_data["position"]
        if not position:
            raise forms.ValidationError("失策には守備位置が必要です。")
        return position

    def to_error(self) -> FieldingError:
        position = FieldingPosition.from_label(self.cleaned_data["position"])
        assert position is not None, "clean_position が空を弾いている"
        return FieldingError(
            player_id=self.cleaned_data["player_id"],
            position=position,
            kind=ErrorKind.from_label(self.cleaned_data["kind"]),
        )


class PlateAppearanceForm(forms.Form):
    """1打席。走者の動き（進塁・失策）は別のフォームで受け取り、ここで束ねる。"""

    sequence = forms.IntegerField(label="打席の順番", min_value=1)
    inning = forms.IntegerField(label="回", min_value=1, max_value=MAX_INNINGS)
    is_bottom = forms.BooleanField(label="ホームの攻撃", required=False)
    batter_id = forms.IntegerField(widget=forms.HiddenInput)
    pitcher_id = forms.IntegerField(widget=forms.HiddenInput)
    batting_order = forms.IntegerField(label="打順", min_value=1, max_value=9)
    slot_sequence = forms.IntegerField(label="交代", min_value=0, max_value=9, required=False)
    result = forms.ChoiceField(label="結果", choices=PLATE_APPEARANCE_RESULT_CHOICES)
    fielded_by = forms.CharField(
        label="打球の処理",
        required=False,
        max_length=30,
        help_text="守備位置を「-」で連ねます（遊ゴロ併殺なら 遊-二-一）。",
    )

    def clean_fielded_by(self) -> str:
        """守備位置として読めない記号が混ざっていないか確かめる。"""
        value = self.cleaned_data.get("fielded_by") or ""
        try:
            self._to_fielded_by(value)
        except InvalidPosition as error:
            raise forms.ValidationError(str(error)) from None
        return value

    @staticmethod
    def _to_fielded_by(value: str) -> tuple[FieldingPosition, ...]:
        positions = (FieldingPosition.from_label(label) for label in value.split(FIELDED_BY_SEPARATOR))
        return tuple(position for position in positions if position is not None)

    def to_plate_appearance(self, advances: list[RunnerAdvance], errors: list[FieldingError]) -> PlateAppearance:
        """打席として組み立てる。結果と進塁の食い違いはドメインが弾く。"""
        return PlateAppearance(
            sequence=self.cleaned_data["sequence"],
            inning=self.cleaned_data["inning"],
            is_bottom=self.cleaned_data["is_bottom"],
            batter_id=self.cleaned_data["batter_id"],
            pitcher_id=self.cleaned_data["pitcher_id"],
            batting_order=self.cleaned_data["batting_order"],
            slot_sequence=self.cleaned_data.get("slot_sequence") or 0,
            result=PlateAppearanceResult.from_label(self.cleaned_data["result"]),
            fielded_by=self._to_fielded_by(self.cleaned_data.get("fielded_by") or ""),
            advances=advances,
            errors=errors,
        )


def _postseason_format_field(*, initial: PostseasonFormat | None = None) -> forms.ChoiceField:
    """ポストシーズンの方式の項目（作成フォームと設定の画面で共有）。

    選択肢は domain の `PostseasonFormat` が出典（送る値は名前、表示は `label`）。
    """
    return forms.ChoiceField(
        label="ポストシーズン",
        choices=[(option.name, option.label) for option in PostseasonFormat],
        initial=initial.name if initial is not None else None,
        widget=forms.RadioSelect(attrs={"class": "form-check-input"}),
    )


def _postseason_choices(form: forms.Form) -> list[tuple[Any, str]]:
    """方式の選択肢（ラジオボタン）と、その1行の説明（`PostseasonFormat.description`）の組。"""
    return [(radio, PostseasonFormat[radio.data["value"]].description) for radio in form["postseason_format"]]


def _draft_order_field(*, initial: DraftOrderRule | None = None, required: bool = True) -> forms.ChoiceField:
    """ドラフトの指名順の規則の項目（作成フォームと設定の画面で共有）。

    選択肢は domain の `DraftOrderRule` が出典（送る値は名前、表示は `label`）。
    """
    return forms.ChoiceField(
        label="ドラフトの指名順",
        choices=[(option.name, option.label) for option in DraftOrderRule],
        initial=initial.name if initial is not None else None,
        required=required,
        widget=forms.RadioSelect(attrs={"class": "form-check-input"}),
    )


def _draft_order_choices(form: forms.Form) -> list[tuple[Any, str]]:
    """規則の選択肢（ラジオボタン）と、その1行の説明（`DraftOrderRule.description`）の組。"""
    return [(radio, DraftOrderRule[radio.data["value"]].description) for radio in form["draft_order_rule"]]


class DraftOrderRuleForm(forms.Form):
    """世界の設定の画面の、ドラフトの指名順の規則の変更。初期値は世界のいまの規則（ビューが渡す）。"""

    draft_order_rule = _draft_order_field()

    def clean_draft_order_rule(self) -> DraftOrderRule:
        return DraftOrderRule[self.cleaned_data["draft_order_rule"]]

    def draft_order_choices(self) -> list[tuple[Any, str]]:
        return _draft_order_choices(self)


class PostseasonFormatForm(forms.Form):
    """世界の設定の画面の、ポストシーズンの方式の変更。初期値は世界のいまの方式（ビューが渡す）。"""

    postseason_format = _postseason_format_field()

    def clean_postseason_format(self) -> PostseasonFormat:
        return PostseasonFormat[self.cleaned_data["postseason_format"]]

    def postseason_choices(self) -> list[tuple[Any, str]]:
        return _postseason_choices(self)


class PennantWorldForm(forms.Form):
    """ペナントの世界の作成。元にするリーグ（1〜8）と、受け持つ球団（必須）を選ぶ。

    選択肢は実データのリーグ・球団（`groups`）から作る。リーグの数の上限と、受け持つ球団が
    選んだリーグの球団であることはここで確かめる（世界の作成も同じ規則で弾く）。
    """

    name = forms.CharField(
        label="世界の名前", max_length=MAX_NAME_LENGTH, widget=forms.TextInput(attrs={"class": "form-control"})
    )
    leagues = forms.MultipleChoiceField(
        label="元にするリーグ", widget=forms.CheckboxSelectMultiple(attrs={"class": "form-check-input"})
    )
    managed_team = forms.ChoiceField(label="受け持つ球団", widget=forms.Select(attrs={"class": "form-select"}))
    start_year = forms.IntegerField(
        label="開幕年",
        min_value=Season.MIN_YEAR,
        max_value=MAX_START_YEAR,
        widget=forms.NumberInput(attrs={"class": "form-control"}),
    )
    seed = forms.IntegerField(
        label="乱数のシード",
        required=False,
        min_value=0,
        max_value=MAX_SEED,
        widget=forms.NumberInput(attrs={"class": "form-control"}),
    )
    # 新しい世界の既定は NPB 方式
    postseason_format = _postseason_format_field(initial=PostseasonFormat.NPB)
    # 送られなければ既定の NPB 方式（規則を足す前の作成の送信と同じ結果。不正な値は弾く）
    draft_order_rule = _draft_order_field(initial=DraftOrderRule.NPB, required=False)

    def __init__(self, *args, groups: Sequence[LeagueTeams], default_year: int, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._league_of_team = {str(team.id): str(group.league_id) for group in groups for team in group.teams}
        # 宣言した項目は、フォームの作成時に項目の複製（`self.fields`）に置き換わる。複製の側に選択肢を入れる
        self._leagues_field = cast(forms.MultipleChoiceField, self.fields["leagues"])
        self._team_field = cast(forms.ChoiceField, self.fields["managed_team"])
        self._leagues_field.choices = [(str(group.league_id), group.league_name) for group in groups]
        # リーグごとの <optgroup>。受け持つ球団は必須なので、先頭の空の選択肢は選べない
        self._team_field.choices = [("", "選んでください")] + [
            (group.league_name, [(str(team.id), team.name) for team in group.teams]) for group in groups
        ]
        self.fields["start_year"].initial = default_year

    def clean_postseason_format(self) -> PostseasonFormat:
        return PostseasonFormat[self.cleaned_data["postseason_format"]]

    def postseason_choices(self) -> list[tuple[Any, str]]:
        return _postseason_choices(self)

    def clean_draft_order_rule(self) -> DraftOrderRule:
        chosen = self.cleaned_data["draft_order_rule"]
        return DraftOrderRule[chosen] if chosen else DraftOrderRule.NPB

    def draft_order_choices(self) -> list[tuple[Any, str]]:
        return _draft_order_choices(self)

    def clean(self):
        cleaned = super().clean()
        leagues = cleaned.get("leagues") or []
        if len(leagues) > MAX_SOURCE_LEAGUES:
            self.add_error("leagues", f"元にするリーグは{MAX_SOURCE_LEAGUES}つまで選べます。")
        team = cleaned.get("managed_team")
        if team and leagues and self._league_of_team.get(team) not in leagues:
            self.add_error("managed_team", "受け持つ球団は、選んだリーグの球団から選んでください。")
        return cleaned
