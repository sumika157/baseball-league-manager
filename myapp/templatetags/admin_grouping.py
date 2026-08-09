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

    # 利用者が列で並べ替えたときは、まとまりが崩れて見出しが何度も出るため
    # グループ化をやめる
    if cl.params.get('o'):
        return None

    rows = []
    previous = object()  # 最初の行では必ず見出しを出すための番兵
    for obj, result in zip(cl.result_list, results):
        label = group_by(obj)
        rows.append({'header': label if label != previous else None, 'result': result})
        previous = label
    return rows
