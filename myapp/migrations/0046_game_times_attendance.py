from django.db import migrations, models


class Migration(migrations.Migration):
    """試合に開始・終了の時刻・中断時間・観客数を足す（スキーマだけ。既存の試合は未記録のまま）。"""

    dependencies = [
        ('myapp', '0045_pennant_club_plan'),
    ]

    operations = [
        migrations.AddField(
            model_name='game',
            name='start_time',
            field=models.TimeField(blank=True, null=True, verbose_name='開始時刻'),
        ),
        migrations.AddField(
            model_name='game',
            name='end_time',
            field=models.TimeField(blank=True, null=True, verbose_name='終了時刻'),
        ),
        migrations.AddField(
            model_name='game',
            name='ends_next_day',
            field=models.BooleanField(default=False, verbose_name='終了が翌日'),
        ),
        migrations.AddField(
            model_name='game',
            name='interruption_minutes',
            field=models.PositiveIntegerField(default=0, help_text='雨天・停電などの中断。試合時間から差し引きます。', verbose_name='中断時間（分）'),
        ),
        migrations.AddField(
            model_name='game',
            name='attendance',
            field=models.PositiveIntegerField(blank=True, null=True, verbose_name='観客数'),
        ),
    ]
