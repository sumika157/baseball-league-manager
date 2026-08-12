"""入力フォーム。

これまで views.py が request.POST.get() の生文字列をそのままモデルへ渡していたため、
数値欄に文字列が入ると例外になっていた。型変換と必須チェックはここで完結させ、
アプリケーション層には検証済みの値だけを渡す。
"""

from django import forms

from ..application.dto import LineupSlot
from ..domain.entities import FieldingError, PlateAppearance, RunnerAdvance
from ..domain.exceptions import InvalidPosition
from ..domain.value_objects import (
    AdvanceReason,
    Base,
    ErrorKind,
    FieldingPosition,
    PlateAppearanceResult,
    Position,
)

POSITION_CHOICES = [(position.value, position.value) for position in Position]
# 試合で就いた守備位置。未記録も許すので空の選択肢を先頭に置く
FIELDING_POSITION_CHOICES = [("", "—")] + [(p.value, p.value) for p in FieldingPosition]
# 1試合の回数。延長を含めても12回までを入力できるようにする
MAX_INNINGS = 12


class PlayerRegistrationForm(forms.Form):
    """新入団選手の登録。"""

    name = forms.CharField(label="選手名", max_length=100)
    number = forms.IntegerField(label="背番号", min_value=0, max_value=999)
    position = forms.ChoiceField(
        label="守備位置",
        choices=POSITION_CHOICES,
        initial=Position.INFIELDER.value,
    )


class PlayerUpdateForm(PlayerRegistrationForm):
    """選手情報の更新。基本情報の項目は登録時と同じ。"""


class GameForm(forms.Form):
    """試合の基本情報。

    対戦カードの妥当性（同一チーム同士でないか）はドメインが判定するので、
    ここでは型と必須だけを見る。
    """

    year = forms.IntegerField(label="シーズン", min_value=1900, max_value=2100)
    played_on = forms.DateField(label="試合日", widget=forms.DateInput(attrs={"type": "date"}))
    home_team = forms.IntegerField(label="ホーム", widget=forms.HiddenInput)
    away_team = forms.IntegerField(label="ビジター", widget=forms.HiddenInput)
    home_score = forms.IntegerField(label="ホーム得点", min_value=0, initial=0)
    away_score = forms.IntegerField(label="ビジター得点", min_value=0, initial=0)


class InningScoreForm(forms.Form):
    """1回ぶんの得点。表（ビジター）と裏（ホーム）を並べて入力する。

    勝敗・セーブ・ホールドは継投した時点のスコアで決まるので、回ごとの経過が
    無いと日本プロ野球の規則どおりに決められない。
    """

    # min/max を付けているのは、JSON API（presentation/api.py）が手作りの
    # リクエストを受け取れるため。範囲外の回番号で巨大なタプルを作られたり、
    # 位置ではなく回番号で値を割り当てる処理が取り違えたりしないようにする。
    inning = forms.IntegerField(widget=forms.HiddenInput, min_value=1, max_value=MAX_INNINGS)
    away = forms.IntegerField(label="表", min_value=0, required=False)
    home = forms.IntegerField(label="裏", min_value=0, required=False)

    def is_blank(self) -> bool:
        """両方とも未入力なら、その回は行われていないとみなす。

        0 と未入力は区別する（0点で終えた回と、まだ無い回は違う）。
        """
        return self.cleaned_data.get("away") is None and self.cleaned_data.get("home") is None


class BattingEntryForm(forms.Form):
    """ロスター1人ぶんの打撃成績。未入力なら「出場していない」とみなす。"""

    player_id = forms.IntegerField(widget=forms.HiddenInput)
    # 打線での位置づけ。ボックススコアの並びに使う
    batting_order = forms.IntegerField(label="打順", min_value=1, max_value=9, required=False)
    slot_sequence = forms.IntegerField(
        label="交代",
        min_value=0,
        max_value=9,
        required=False,
        help_text="0 がスタメン。1以上は同じ打順への途中出場。",
    )
    fielding_position = forms.ChoiceField(label="守備位置", choices=FIELDING_POSITION_CHOICES, required=False)
    at_bats = forms.IntegerField(label="打数", min_value=0, required=False)
    singles = forms.IntegerField(label="単打", min_value=0, required=False)
    doubles = forms.IntegerField(label="二塁打", min_value=0, required=False)
    triples = forms.IntegerField(label="三塁打", min_value=0, required=False)
    home_runs = forms.IntegerField(label="本塁打", min_value=0, required=False)
    runs_batted_in = forms.IntegerField(label="打点", min_value=0, required=False)
    walks = forms.IntegerField(label="四球", min_value=0, required=False)
    hit_by_pitch = forms.IntegerField(label="死球", min_value=0, required=False)
    sacrifice_flies = forms.IntegerField(label="犠飛", min_value=0, required=False)

    STAT_FIELDS = (
        "at_bats",
        "singles",
        "doubles",
        "triples",
        "home_runs",
        "runs_batted_in",
        "walks",
        "hit_by_pitch",
        "sacrifice_flies",
    )

    def counts(self) -> dict:
        return {f: (self.cleaned_data.get(f) or 0) for f in self.STAT_FIELDS}

    def lineup(self) -> tuple:
        """(打順, 交代の順, 守備位置)。ドメインの値に直して返す。"""
        return (
            self.cleaned_data.get("batting_order"),
            self.cleaned_data.get("slot_sequence") or 0,
            FieldingPosition.from_label(self.cleaned_data.get("fielding_position") or ""),
        )

    def is_blank(self) -> bool:
        """すべて未入力・0なら出場していないとみなす。

        全て0の行を残すと「出場したが無安打」と「出場していない」が
        区別できなくなる。打順や守備位置だけの入力も出場とみなす
        （無安打でスタメンだった選手を残せるようにするため）。
        """
        if any(self.counts().values()):
            return False
        order, _, position = self.lineup()
        return order is None and position is None


class PitchingEntryForm(forms.Form):
    """ロスター1人ぶんの投球成績。

    勝利・敗戦・セーブ・ホールドの欄は持たない。イニングスコアと登板した回から
    日本プロ野球の規則で一意に決まるものなので、手入力させると記録どうしが
    食い違う（勝利投手が2人いる、負けチームの投手に勝利が付くなど）。
    """

    player_id = forms.IntegerField(widget=forms.HiddenInput)
    # 何回から投げたか。セーブ・ホールドの条件は登板した時点のスコアで決まる
    entered_inning = forms.IntegerField(
        label="登板",
        min_value=1,
        max_value=MAX_INNINGS,
        required=False,
        help_text="何回から投げたか。先発は1。",
    )
    # 5.2（5回と2/3）のような野球表記を受け取る。解釈は InningsPitched が担う
    innings_pitched = forms.DecimalField(label="投球回", min_value=0, decimal_places=1, required=False)
    earned_runs = forms.IntegerField(label="自責点", min_value=0, required=False)
    strikeouts = forms.IntegerField(label="奪三振", min_value=0, required=False)
    hits_allowed = forms.IntegerField(label="被安打", min_value=0, required=False)
    walks_allowed = forms.IntegerField(label="与四球", min_value=0, required=False)
    home_runs_allowed = forms.IntegerField(label="被本塁打", min_value=0, required=False)
    hit_by_pitch_allowed = forms.IntegerField(label="与死球", min_value=0, required=False)

    COUNT_FIELDS = (
        "earned_runs",
        "strikeouts",
        "hits_allowed",
        "walks_allowed",
        "home_runs_allowed",
        "hit_by_pitch_allowed",
    )

    def counts(self) -> dict:
        return {f: (self.cleaned_data.get(f) or 0) for f in self.COUNT_FIELDS}

    def innings(self):
        return self.cleaned_data.get("innings_pitched") or 0

    def entered(self) -> int:
        return self.cleaned_data.get("entered_inning") or 1

    def is_blank(self) -> bool:
        """投げていなければ出場していないとみなす。

        登板した回だけの入力は出場とみなさない（既定値の1が残るため、
        入力の有無を判別できない）。
        """
        return not self.innings() and not any(self.counts().values())


BattingEntryFormSet = forms.formset_factory(BattingEntryForm, extra=0)
PitchingEntryFormSet = forms.formset_factory(PitchingEntryForm, extra=0)
InningScoreFormSet = forms.formset_factory(InningScoreForm, extra=0)


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

    def to_slot(self) -> LineupSlot:
        return LineupSlot(
            team_id=self.cleaned_data["team_id"],
            player_id=self.cleaned_data["player_id"],
            batting_order=self.cleaned_data["batting_order"],
            slot_sequence=self.cleaned_data.get("slot_sequence") or 0,
            fielding_position=FieldingPosition.from_label(self.cleaned_data.get("fielding_position") or ""),
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
