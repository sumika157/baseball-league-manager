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

    値が変わったところで見出しを挟むだけなので、同じまとまりの行が
    続いていることが前提。ModelAdmin 側の ordering がそれを担う。
    """
    group_by = getattr(cl.model_admin, 'group_by', None)
    if not group_by:
        return None

    # 列で並べ替えられる一覧では、まとまりの順序を先に置いて区切りを守る
    # （GroupedChangeList の役目）。それが無いまま並べ替えると同じ見出しが
    # 何度も出るため、その場合はグループ化をやめて標準の描画に戻す
    if cl.params.get('o') and not getattr(cl.model_admin, 'group_ordering', None):
        return None

    rows = []
    previous = object()  # 最初の行では必ず見出しを出すための番兵
    for obj, result in zip(cl.result_list, results):
        label = group_by(obj)
        rows.append({
            'header': label if label != previous else None,
            'result': result,
        })
        previous = label
    return rows
