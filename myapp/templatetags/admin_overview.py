"""管理画面トップに概況を差し込むためのテンプレートタグ。

AdminSite を差し替えずに済ませるため、テンプレート側から呼び出す形にしている。
件数の算出はアプリケーション層に委ね、ここでは組み立てない。
"""

from django import template

from ..application.services import TeamApplicationService
from ..infrastructure.queries import DjangoTeamListQuery
from ..infrastructure.repositories import DjangoTeamRepository

register = template.Library()


@register.inclusion_tag("admin/_overview.html")
def admin_overview():
    service = TeamApplicationService(teams=DjangoTeamRepository(), team_list_query=DjangoTeamListQuery())
    return {"overview": service.get_admin_overview()}
