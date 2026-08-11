"""一覧の並べ替え・絞り込みリンクを組み立てるテンプレートタグ。

どちらも**現在の URL のクエリを保ったまま**一部のキーだけを差し替える。
並べ替えと絞り込みは同じ画面で併用するので、片方を押したときにもう片方の
指定が落ちると操作が噛み合わなくなる。
"""

from django import template
from django.http import QueryDict
from django.utils.html import format_html

register = template.Library()

ASCENDING_MARK = "↑"
DESCENDING_MARK = "↓"


def _query_with(request, **overrides) -> str:
    """今のクエリの一部を差し替えた `?...` を返す。空文字を渡したキーは取り除く。"""
    params = request.GET.copy() if request else QueryDict(mutable=True)
    for key, value in overrides.items():
        if value in (None, ""):
            params.pop(key, None)
        else:
            params[key] = value
    query = params.urlencode()
    return f"?{query}" if query else "?"


@register.simple_tag(takes_context=True)
def query_with(context, **overrides) -> str:
    """絞り込みリンクの href。渡したキーだけを差し替え、他の条件は保つ。

    既定に戻したいキーは空文字を渡す（`{% query_with qualified='' %}`）。
    """
    return _query_with(context.get("request"), **overrides)


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

    query = _query_with(request, sort=key, dir="desc" if next_desc else "asc").removeprefix("?")

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
