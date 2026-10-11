"""共有テンプレートの URL を、実データとペナントの世界で引き分けるテンプレートタグ。

同じテンプレート（順位表・試合詳細など）を実データでもペナントの世界でも使うので、リンクの引き方を
ここに集める。context に `world`（`WorldContext`）があれば、世界の範囲の URL（`pennant_<名前>`、
世界の id が先頭の引数）を、無ければ `{% url %}` と同じ結果を返す。

DTO に URL を載せる案は採らない（全 DTO に presentation の事情が入るため）。
"""

from django import template
from django.urls import reverse

register = template.Library()


@register.simple_tag(takes_context=True)
def scoped_url(context, name, *args):
    """`{% scoped_url 'player_list' team.id %}`。世界の中では `pennant_player_list`（世界の id が先頭）を引く。"""
    world = context.get("world")
    if world is None:
        return reverse(name, args=args)
    return reverse(f"pennant_{name}", args=(world.world_id, *args))
