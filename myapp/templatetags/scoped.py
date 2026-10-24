"""ワークスペースの中の画面への URL を引くテンプレートタグ。

- `{% scoped_url %}`: 共有テンプレート（順位表・試合詳細など）を実データでもペナントの世界でも使うので、
  リンクの引き方をここに集める。context に `world`（`WorldContext`）があれば、世界の範囲の URL
  （`pennant_<名前>`、世界の id が key の次の引数）を、無ければ実データの同名の URL を返す。
- `{% workspace_url %}`: 名前を切り替えない。ペナント専用（`pennant_club` など）・実データ専用
  （`game_create` など）の画面へのリンク。

ワークスペースの中の画面（`myapp/urls.py` の `workspace_urlpatterns`）へは、素の `{% url %}` ではなく
このどちらかで引く（`test_templates.py` が検査する）。どちらも context の `workspace`（コンテキストプロセッサが
渡す、今の要求のワークスペース）の key を先頭の引数に足す（W2 T4。#78）。`workspace` が無ければ例外にする
（黙って key の無い URL や、ほかのワークスペースの URL を作らない）。ワークスペースの外の画面（`/`・ログイン・
404）のテンプレートは、`{% if workspace %}` で囲んでから使う。

DTO に URL を載せる案は採らない（全 DTO に presentation の事情が入るため）。
"""

from django import template
from django.urls import reverse

register = template.Library()


class WorkspaceMissing(LookupError):
    """ワークスペースの外の描画で、中の画面の URL を引こうとした（テンプレートの誤り）。"""


def _workspace_reverse(context, name: str, args) -> str:
    """ワークスペースの中の URL を引く。context の `workspace` の key を先頭の引数に足す。"""
    workspace = context.get("workspace")
    if workspace is None:
        raise WorkspaceMissing(
            f"ワークスペースの外で、中の画面（{name}）の URL を引こうとしました。"
            "{% if workspace %} で囲んでください。"
        )
    return reverse(name, args=(workspace.key, *args))


@register.simple_tag(takes_context=True)
def scoped_url(context, name, *args):
    """`{% scoped_url 'player_list' team.id %}`。世界の中では `pennant_player_list`（世界の id が key の次）を引く。"""
    world = context.get("world")
    if world is None:
        return _workspace_reverse(context, name, args)
    return _workspace_reverse(context, f"pennant_{name}", (world.world_id, *args))


@register.simple_tag(takes_context=True)
def workspace_url(context, name, *args):
    """`{% workspace_url 'pennant_club' world.world_id %}`。名前は切り替えず、ワークスペースの中の URL を引く。"""
    return _workspace_reverse(context, name, args)


@register.simple_tag
def workspace_entry_url(key: str) -> str:
    """`{% workspace_entry_url row.key %}`。ワークスペースの外（`/` の一覧）から、指した key のワークスペースの入口へ。

    今の要求のワークスペースではなく、渡した key の入口（ダッシュボード）だけを引く。中のほかの画面へは使わない。
    """
    return reverse("dashboard", args=(key,))
