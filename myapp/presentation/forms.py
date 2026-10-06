"""入力フォーム。

これまで views.py が request.POST.get() の生文字列をそのままモデルへ渡していたため、
数値欄に文字列が入ると例外になっていた。型変換と必須チェックはここで完結させ、
アプリケーション層には検証済みの値だけを渡す。
"""

from typing import Any

from django import forms

from ..application.dto import LineupSlot
from ..domain.entities import FieldingError, PlateAppearance, RunnerAdvance
from ..domain.exceptions import InvalidJerseyNumber, InvalidPosition
from ..domain.value_objects import (
    AdvanceReason,
    Base,
    ContractStatus,
    ErrorKind,
    FieldingPosition,
    JerseyNumber,
    PlateAppearanceResult,
    Position,
)

POSITION_CHOICES = [(position.value, position.value) for position in Position]
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
