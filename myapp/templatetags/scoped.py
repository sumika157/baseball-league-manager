"""ワークスペースの中の画面への URL を引くテンプレートタグ。

- `{% scoped_url %}`: 共有テンプレート（順位表・試合詳細など）を実データでもペナントの世界でも使うので、
  リンクの引き方をここに集める。context に `world`（`WorldContext`）があれば、世界の範囲の URL
  （`pennant_<名前>`、世界の id が先頭の引数）を、無ければ実データの同名の URL を返す。
- `{% workspace_url %}`: 名前を切り替えない。ペナント専用（`pennant_club` など）・実データ専用
  （`game_create` など）の画面へのリンク。

ワークスペースの中の画面（`myapp/urls.py` の `workspace_urlpatterns`）へは、素の `{% url %}` ではなく
このどちらかで引く（`test_templates.py` が検査する）。W2 T3（#78）の時点ではどちらも `{% url %}` と同じ
結果で、T4 で URL の先頭にワークスペースの key を足す（足すのは `_workspace_reverse` の1か所）。

DTO に URL を載せる案は採らない（全 DTO に presentation の事情が入るため）。
"""

from django import template
from django.urls import reverse

register = template.Library()


def _workspace_reverse(context, name: str, args) -> str:
    """ワークスペースの中の URL を引く。T4 で context の `workspace` の key を先頭の引数に足す。"""
    return reverse(name, args=args)


@register.simple_tag(takes_context=True)
def scoped_url(context, name, *args):
    """`{% scoped_url 'player_list' team.id %}`。世界の中では `pennant_player_list`（世界の id が先頭）を引く。"""
    world = context.get("world")
    if world is None:
        return _workspace_reverse(context, name, args)
    return _workspace_reverse(context, f"pennant_{name}", (world.world_id, *args))


@register.simple_tag(takes_context=True)
def workspace_url(context, name, *args):
    """`{% workspace_url 'pennant_club' world.world_id %}`。名前は切り替えず、ワークスペースの中の URL を引く。"""
    return _workspace_reverse(context, name, args)
