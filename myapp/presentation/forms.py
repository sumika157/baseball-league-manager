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
