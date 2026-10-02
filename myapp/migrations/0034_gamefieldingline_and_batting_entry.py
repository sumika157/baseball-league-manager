import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """守備成績のテーブルと、ラインアップに持たせるチーム・出場した打席を足す。

    チームは既存の行に値が無いので、まず空を許して足し、次のマイグレーションで埋めてから
    必須に変える（0035 → 0036）。スキーマ変更とデータ移行は別ファイルに分ける。
    """

    dependencies = [
        ('myapp', '0033_gamebattingline_caught_stealing_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='GameFieldingLine',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('putouts', models.IntegerField(default=0, verbose_name='刺殺')),
                ('assists', models.IntegerField(default=0, verbose_name='補殺')),
                ('errors', models.IntegerField(default=0, verbose_name='失策')),
                ('double_plays_turned', models.IntegerField(default=0, verbose_name='併殺参加')),
                ('game', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='fielding_lines', to='myapp.game', verbose_name='試合')),
                ('player', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='game_fielding', to='myapp.player', verbose_name='選手')),
            ],
            options={
                'verbose_name': '守備成績',
                'verbose_name_plural': '守備成績',
                'constraints': [models.UniqueConstraint(fields=('game', 'player'), name='unique_game_fielding')],
            },
        ),
        migrations.AddField(
            model_name='gamebattingline',
            name='entered_sequence',
            field=models.PositiveIntegerField(blank=True, help_text='試合に入った最初の打席の通し番号。スタメンは空欄（試合開始から）。途中出場で空欄なら不明です。', null=True, verbose_name='出場した打席'),
        ),
        migrations.AddField(
            model_name='gamebattingline',
            name='team',
            field=models.ForeignKey(help_text='試合のホームかビジター。守備位置を選手に引くのに使います。', null=True, on_delete=django.db.models.deletion.CASCADE, related_name='game_batting_lines', to='myapp.team', verbose_name='チーム'),
        ),
    ]
