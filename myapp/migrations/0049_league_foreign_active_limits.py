# 外国人枠を野球協約 第82条の2 に合わせる（#204 段階A）。1軍登録の枠（計・投手・野手）の3列を足す。
# 既存のリーグ（実データ・ペナントの世界）は既定値の 4／3／3 で埋まる（協約どおりにするユーザー決定。
# 古い登録枠・出場枠からの値の写しはしない）。古い2列は 0050 で消す。

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('myapp', '0048_pennant_release_plan'),
    ]

    operations = [
        migrations.AddField(
            model_name='league',
            name='foreign_active_limit',
            field=models.PositiveIntegerField(blank=True, default=4, help_text='1軍（出場選手登録）に置ける外国人選手の上限人数（野球協約 第82条の2 は4人）。空欄なら無制限。保有（育成を含む在籍）の人数は縛りません。実データの試合では、出場した外国人選手に当てて検査します。', null=True, verbose_name='外国人選手の1軍登録枠'),
        ),
        migrations.AddField(
            model_name='league',
            name='foreign_active_pitcher_limit',
            field=models.PositiveIntegerField(blank=True, default=3, help_text='1軍に置ける外国人選手のうち、登録位置が投手の選手の上限人数（協約は3人）。空欄なら無制限。', null=True, verbose_name='外国人選手の1軍登録枠（うち投手）'),
        ),
        migrations.AddField(
            model_name='league',
            name='foreign_active_batter_limit',
            field=models.PositiveIntegerField(blank=True, default=3, help_text='1軍に置ける外国人選手のうち、登録位置が投手でない選手の上限人数（協約は3人）。空欄なら無制限。', null=True, verbose_name='外国人選手の1軍登録枠（うち野手）'),
        ),
        migrations.AlterField(
            model_name='player',
            name='is_foreign_player',
            field=models.BooleanField(default=False, help_text='外国人枠の対象として数える選手かどうか。日本国籍を持たない選手のうち、野球協約 第82条 (1)〜(3)・(5)（日本の学校・居住・社会人の年数など）に当たらない選手に付けます。', verbose_name='外国人選手'),
        ),
    ]
