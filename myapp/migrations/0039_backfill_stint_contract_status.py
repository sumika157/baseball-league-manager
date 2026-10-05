"""背番号が100以上の既存の在籍を、育成として登録する。

契約区分（signed_as）の追加（0038）より前のデータには区分が無く、すべて既定の
支配下になっている。日本の球団では育成選手が3桁の背番号を使うので、
背番号が100以上の在籍だけ育成に直す。昇格の記録（promoted_year）は元から無いので空のまま。

この判定基準（100）はこの時点で固定する。ドメインの値を参照しない
（データマイグレーションを自己完結させるため）。
"""

from django.db import migrations

DEVELOPMENTAL_MIN_NUMBER = 100


def backfill(apps, schema_editor):
    PlayerStint = apps.get_model('myapp', 'PlayerStint')
    PlayerStint.objects.filter(number__gte=DEVELOPMENTAL_MIN_NUMBER).update(signed_as='育成')


def unset(apps, schema_editor):
    PlayerStint = apps.get_model('myapp', 'PlayerStint')
    PlayerStint.objects.filter(
        number__gte=DEVELOPMENTAL_MIN_NUMBER, signed_as='育成'
    ).update(signed_as='支配下')


class Migration(migrations.Migration):

    dependencies = [
        ('myapp', '0038_contract_status'),
    ]

    operations = [
        migrations.RunPython(backfill, unset),
    ]
