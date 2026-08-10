"""並べ替え用の見出しリンクを組み立てるテンプレートタグ。

現在の URL のクエリを保ったまま sort と dir だけを差し替える。
pos=pitcher のような他の条件を落とさないようにするため。
"""

from django import template
from django.utils.html import format_html

register = template.Library()

ASCENDING_MARK = "↑"
DESCENDING_MARK = "↓"


@register.simple_tag(takes_context=True)
def sort_header(context, key, label, align="start", default_desc=True):
    """並べ替え可能な見出しセルを描画する。

    key      … 並べ替えキー（URL の sort に入る値）
    label    … 見出しの文言
    align    … 'start' か 'end'。数値列は 'end'
    default_desc … その列を初めて押したときに降順にするか
    """
    request = context.get("request")
    current = context.get("current_sort")
    current_desc = context.get("current_descending")

    is_active = current == key

    # 同じ列を押したら向きを反転、別の列なら既定の向き
    next_desc = (not current_desc) if is_active else bool(default_desc)

    params = request.GET.copy() if request else {}
    if hasattr(params, "setlist"):
        params["sort"] = key
        params["dir"] = "desc" if next_desc else "asc"
        query = params.urlencode()
    else:  # pragma: no cover - request が無い場合の保険
        query = f"sort={key}&dir=" + ("desc" if next_desc else "asc")

    mark = ""
    if is_active:
        mark = DESCENDING_MARK if current_desc else ASCENDING_MARK

    return format_html(
        '<th class="{}"><a class="sort-link{}" href="?{}">{}<span class="sort-mark">{}</span></a></th>',
        "text-end" if align == "end" else "",
        " is-active" if is_active else "",
        query,
        label,
        mark,
    )
