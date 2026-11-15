import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('myapp', '0071_backfill_pitching_fielding_line_team'),
    ]

    operations = [
        migrations.AlterField(
            model_name='gamepitchingline',
            name='team',
            field=models.ForeignKey(help_text='試合のホームかビジター。年の途中で移籍した投手の成績を球団ごとに分けるのに使います。', on_delete=django.db.models.deletion.CASCADE, related_name='game_pitching_lines', to='myapp.team', verbose_name='チーム'),
        ),
        migrations.AlterField(
            model_name='gamefieldingline',
            name='team',
            field=models.ForeignKey(help_text='試合のホームかビジター。年の途中で移籍した選手の成績を球団ごとに分けるのに使います。', on_delete=django.db.models.deletion.CASCADE, related_name='game_fielding_lines', to='myapp.team', verbose_name='チーム'),
        ),
    ]
