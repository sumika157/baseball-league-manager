"""管理画面トップに概況を差し込むためのテンプレートタグ。

AdminSite を差し替えずに済ませるため、テンプレート側から呼び出す形にしている。
件数の算出はアプリケーション層に委ね、ここでは組み立てない。

W1 の間は今の URL と同じワークスペース（`_real_scope`）の実データの概況を出す。
運営向けの概況（ワークスペース数など）への置き換えは W2。
"""

from django import template
from django.http import Http404

from ..presentation.views import _real_scope, build_service

register = template.Library()


@register.inclusion_tag("admin/_overview.html", takes_context=True)
def admin_overview(context):
    try:
        scope = _real_scope(context["request"])
    except Http404:
        # ワークスペースがまだ無い（運営者が最初の1つを作る前）。管理画面のトップは開けるようにする
        return {"overview": None}
    return {"overview": build_service(scope).get_admin_overview()}
