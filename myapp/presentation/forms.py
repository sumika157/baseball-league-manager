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


class BattingStatsForm(forms.Form):
    """野手成績の入力。"""

    at_bats = forms.IntegerField(label='打数', min_value=0, required=False)
    singles = forms.IntegerField(label='単打', min_value=0, required=False)
    doubles = forms.IntegerField(label='二塁打', min_value=0, required=False)
    triples = forms.IntegerField(label='三塁打', min_value=0, required=False)
    home_runs = forms.IntegerField(label='本塁打', min_value=0, required=False)
    runs_batted_in = forms.IntegerField(label='打点', min_value=0, required=False)
    walks = forms.IntegerField(label='四球', min_value=0, required=False)
    hit_by_pitch = forms.IntegerField(label='死球', min_value=0, required=False)
    sacrifice_flies = forms.IntegerField(label='犠飛', min_value=0, required=False)

    def cleaned_counts(self) -> dict:
        """未入力を 0 に均した値を返す。"""
        return {key: (value or 0) for key, value in self.cleaned_data.items()}


class PitchingStatsForm(forms.Form):
    """投手成績の入力。"""

    # 5.2（5回と2/3）のような野球表記を受け取る。解釈は InningsPitched が担う。
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

    def cleaned_counts(self) -> dict:
        data = dict(self.cleaned_data)
        innings = data.pop('innings_pitched', None)
        counts = {key: (value or 0) for key, value in data.items()}
        counts['innings_pitched'] = innings if innings is not None else 0
        return counts


class PlayerUpdateForm(PlayerRegistrationForm):
    """選手情報の更新。基本情報の項目は登録時と同じ。"""
