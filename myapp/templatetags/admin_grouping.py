"""管理画面の一覧に、区切りの見出し行を差し込むためのタグ。

Django の changelist は平坦な表しか描かないため、リーグごと・チームごとに
まとまって見えない。ModelAdmin に group_by を定義したときだけ、値が変わった
ところで見出し行を挟む。
"""

from django import template

register = template.Library()


@register.simple_tag
def grouped_results(cl, results):
    """(見出し, 行) の並びを返す。グループ化しない場合は None。

    None を返すと呼び出し側は Django 標準の描画に戻る。
    """
    group_by = getattr(cl.model_admin, 'group_by', None)
    if not group_by:
        return None

    # 列で並べ替えても、まとまりの順序が常に先に効くため区切りは崩れない
    # （GroupedChangeList が保証する）。まとまりを保てない一覧では
    # 見出しが何度も出てしまうので、その場合はグループ化をやめる。
    if cl.params.get('o') and not getattr(cl.model_admin, 'group_ordering', None):
        return None

    rows = []
    previous = object()  # 最初の行では必ず見出しを出すための番兵
    for obj, result in zip(cl.result_list, results):
        label = group_by(obj)
        rows.append({'header': label if label != previous else None, 'result': result})
        previous = label
    return rows
