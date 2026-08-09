"""選手の所属を「在籍（PlayerStint）」へ移す。

移籍を記録できるようにするため、所属チームと背番号を選手そのものから外し、
「いつからいつまでどのチームに居たか」を持つ在籍に移す。

既存の所属は消す前に在籍へ移し替える。加入年はその選手が出場した最初の
試合の年とし、試合が無ければ入団年、それも無ければ現在の年を使う。
"""

from datetime import date

import django.db.models.deletion
from django.db import migrations, models


def player_to_stint(apps, schema_editor):
    """選手の所属・背番号・在籍状況を、在籍1件に移す。"""
    Player = apps.get_model('myapp', 'Player')
    PlayerStint = apps.get_model('myapp', 'PlayerStint')
    GameBattingLine = apps.get_model('myapp', 'GameBattingLine')
    GamePitchingLine = apps.get_model('myapp', 'GamePitchingLine')

    this_year = date.today().year

    for player in Player.objects.all():
        years = [
            row.game.year
            for row in GameBattingLine.objects.filter(player=player).select_related('game')
        ] + [
            row.game.year
            for row in GamePitchingLine.objects.filter(player=player).select_related('game')
        ]
        from_year = min(years) if years else (player.debut_year or this_year)

        PlayerStint.objects.create(
            player=player,
            team_id=player.team_id,
            number=player.number,
            from_year=from_year,
            # 在籍中なら退団年は空。退団済みなら最後に居た年として今年を入れる
            to_year=None if player.is_active else max(from_year, this_year),
        )


def stint_to_player(apps, schema_editor):
    """巻き戻し。現在（または最後）の在籍を選手へ書き戻す。"""
    Player = apps.get_model('myapp', 'Player')
    for player in Player.objects.all():
        stint = (
            player.stints.filter(to_year__isnull=True).first()
            or player.stints.order_by('-from_year').first()
        )
        if stint is None:
            continue
        player.team_id = stint.team_id
        player.number = stint.number
        player.is_active = stint.to_year is None
        player.save(update_fields=['team', 'number', 'is_active'])


class Migration(migrations.Migration):

    dependencies = [
        ('myapp', '0016_stadium_remove_team_city_player_bats_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='PlayerStint',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('number', models.IntegerField(verbose_name='背番号')),
                ('from_year', models.IntegerField(verbose_name='加入年')),
                ('to_year', models.IntegerField(blank=True, help_text='空欄なら現在も在籍しています。', null=True, verbose_name='退団年')),
                ('player', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='stints', to='myapp.player', verbose_name='選手')),
                ('team', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='stints', to='myapp.team', verbose_name='チーム')),
            ],
            options={
                'verbose_name': '在籍',
                'verbose_name_plural': '在籍',
                'ordering': ['-from_year', 'number'],
                'constraints': [models.UniqueConstraint(fields=('player', 'team', 'from_year'), name='unique_player_team_from')],
            },
        ),
        # 所属を消す前にデータを移し替える
        migrations.RunPython(player_to_stint, stint_to_player),
        migrations.AlterModelOptions(
            name='player',
            options={'ordering': ['name'], 'verbose_name': '選手', 'verbose_name_plural': '選手'},
        ),
        migrations.RemoveField(
            model_name='player',
            name='is_active',
        ),
        migrations.RemoveField(
            model_name='player',
            name='number',
        ),
        migrations.RemoveField(
            model_name='player',
            name='team',
        ),
    ]
