# 期中の支配下登録に区分（昇格・獲得）を足す（#169 F3）。既存の行はすべて育成からの昇格（#170 P3a・P3b）。

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("myapp", "0065_pennant_foreign_signing_plan"),
    ]

    operations = [
        migrations.AddField(
            model_name="pennantmidseasonregistration",
            name="kind",
            field=models.CharField(
                choices=[("昇格", "昇格"), ("獲得", "獲得")],
                default="昇格",
                max_length=4,
                verbose_name="区分",
            ),
            preserve_default=False,
        ),
    ]
