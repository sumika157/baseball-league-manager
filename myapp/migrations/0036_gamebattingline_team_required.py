import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """0035 で全行にチームを入れたので、必須に変える。"""

    dependencies = [
        ('myapp', '0035_backfill_batting_team_and_entry'),
    ]

    operations = [
        migrations.AlterField(
            model_name='gamebattingline',
            name='team',
            field=models.ForeignKey(help_text='試合のホームかビジター。守備位置を選手に引くのに使います。', on_delete=django.db.models.deletion.CASCADE, related_name='game_batting_lines', to='myapp.team', verbose_name='チーム'),
        ),
    ]
