# 入団年（debut_year）をドラフトの年・順位・育成に置き換える（#108）。1/3: 列を足す

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("myapp", "0051_pennant_award"),
    ]

    operations = [
        migrations.AddField(
            model_name="player",
            name="draft_year",
            field=models.IntegerField(
                blank=True, help_text="指名された年。入団はふつうその翌年です。", null=True, verbose_name="ドラフト年"
            ),
        ),
        migrations.AddField(
            model_name="player",
            name="draft_round",
            field=models.PositiveSmallIntegerField(
                blank=True,
                help_text="本指名と育成ドラフトで別に数えます。分からなければ空欄。",
                null=True,
                verbose_name="ドラフト順位",
            ),
        ),
        migrations.AddField(
            model_name="player",
            name="is_development_draft",
            field=models.BooleanField(default=False, verbose_name="育成ドラフト"),
        ),
    ]
