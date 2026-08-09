"""入力フォーム。

これまで views.py が request.POST.get() の生文字列をそのままモデルへ渡していたため、
数値欄に文字列が入ると例外になっていた。型変換と必須チェックはここで完結させ、
アプリケーション層には検証済みの値だけを渡す。
"""

from django import forms

from ..domain.value_objects import Position

POSITION_CHOICES = [(position.value, position.value) for position in Position]


class PlayerRegistrationForm(forms.Form):
    """新入団選手の登録。"""

    name = forms.CharField(label='選手名', max_length=100)
    number = forms.IntegerField(label='背番号', min_value=0, max_value=999)
    position = forms.ChoiceField(
        label='守備位置',
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

    year = forms.IntegerField(label='シーズン', min_value=1900, max_value=2100)
    played_on = forms.DateField(
        label='試合日', widget=forms.DateInput(attrs={'type': 'date'})
    )
    home_team = forms.IntegerField(label='ホーム', widget=forms.HiddenInput)
    away_team = forms.IntegerField(label='ビジター', widget=forms.HiddenInput)
    home_score = forms.IntegerField(label='ホーム得点', min_value=0, initial=0)
    away_score = forms.IntegerField(label='ビジター得点', min_value=0, initial=0)


class BattingEntryForm(forms.Form):
    """ロスター1人ぶんの打撃成績。未入力なら「出場していない」とみなす。"""

    player_id = forms.IntegerField(widget=forms.HiddenInput)
    at_bats = forms.IntegerField(label='打数', min_value=0, required=False)
    singles = forms.IntegerField(label='単打', min_value=0, required=False)
    doubles = forms.IntegerField(label='二塁打', min_value=0, required=False)
    triples = forms.IntegerField(label='三塁打', min_value=0, required=False)
    home_runs = forms.IntegerField(label='本塁打', min_value=0, required=False)
    runs_batted_in = forms.IntegerField(label='打点', min_value=0, required=False)
    walks = forms.IntegerField(label='四球', min_value=0, required=False)
    hit_by_pitch = forms.IntegerField(label='死球', min_value=0, required=False)
    sacrifice_flies = forms.IntegerField(label='犠飛', min_value=0, required=False)

    STAT_FIELDS = (
        'at_bats', 'singles', 'doubles', 'triples', 'home_runs',
        'runs_batted_in', 'walks', 'hit_by_pitch', 'sacrifice_flies',
    )

    def counts(self) -> dict:
        return {f: (self.cleaned_data.get(f) or 0) for f in self.STAT_FIELDS}

    def is_blank(self) -> bool:
        """すべて未入力・0なら出場していないとみなす。

        全て0の行を残すと「出場したが無安打」と「出場していない」が
        区別できなくなる。
        """
        return not any(self.counts().values())


class PitchingEntryForm(forms.Form):
    """ロスター1人ぶんの投球成績。"""

    player_id = forms.IntegerField(widget=forms.HiddenInput)
    # 5.2（5回と2/3）のような野球表記を受け取る。解釈は InningsPitched が担う
    innings_pitched = forms.DecimalField(
        label='投球回', min_value=0, decimal_places=1, required=False
    )
    wins = forms.IntegerField(label='勝利', min_value=0, required=False)
    losses = forms.IntegerField(label='敗戦', min_value=0, required=False)
    saves = forms.IntegerField(label='セーブ', min_value=0, required=False)
    earned_runs = forms.IntegerField(label='自責点', min_value=0, required=False)
    strikeouts = forms.IntegerField(label='奪三振', min_value=0, required=False)
    hits_allowed = forms.IntegerField(label='被安打', min_value=0, required=False)
    walks_allowed = forms.IntegerField(label='与四球', min_value=0, required=False)

    COUNT_FIELDS = (
        'wins', 'losses', 'saves', 'earned_runs',
        'strikeouts', 'hits_allowed', 'walks_allowed',
    )

    def counts(self) -> dict:
        return {f: (self.cleaned_data.get(f) or 0) for f in self.COUNT_FIELDS}

    def innings(self):
        return self.cleaned_data.get('innings_pitched') or 0

    def is_blank(self) -> bool:
        return not self.innings() and not any(self.counts().values())


BattingEntryFormSet = forms.formset_factory(BattingEntryForm, extra=0)
PitchingEntryFormSet = forms.formset_factory(PitchingEntryForm, extra=0)
