# 入団年（debut_year）をドラフトの年・順位・育成に置き換える（#108）。3/3: 入団年の列を消す

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("myapp", "0053_backfill_player_draft_year"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="player",
            name="debut_year",
        ),
    ]
