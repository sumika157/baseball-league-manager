"""入力フォーム。

これまで views.py が request.POST.get() の生文字列をそのままモデルへ渡していたため、
数値欄に文字列が入ると例外になっていた。型変換と必須チェックはここで完結させ、
アプリケーション層には検証済みの値だけを渡す。
"""

from django import forms

from ..domain.value_objects import FieldingPosition, Position

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
