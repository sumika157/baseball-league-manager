"""管理画面トップに概況を差し込むためのテンプレートタグ。

AdminSite を差し替えずに済ませるため、テンプレート側から呼び出す形にしている。
概況は運営向けの全ワークスペースの数字（W2 T2）。全ワークスペースを読む集計は、管理画面だけに許した例外
（決定 2026-10-07 Q4）の中に置くため `admin.py` の `operator_overview()` が持ち、ここでは呼ぶだけにする。
"""

from django import template

register = template.Library()


@register.inclusion_tag("admin/_overview.html")
def admin_overview():
    # 管理画面のモジュールはテンプレートを描く時点で読み込み済み。循環を避けるため、ここで引く
    from ..admin import operator_overview

    return {"overview": operator_overview()}
