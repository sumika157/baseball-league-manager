"""管理画面トップに概況を差し込むためのテンプレートタグ。

AdminSite を差し替えずに済ませるため、テンプレート側から呼び出す形にしている。
件数の算出はアプリケーション層に委ね、ここでは組み立てない。
"""

from django import template

from ..presentation.views import _real_scope, build_service

register = template.Library()


@register.inclusion_tag("admin/_overview.html")
def admin_overview():
    return {"overview": build_service(_real_scope()).get_admin_overview()}
