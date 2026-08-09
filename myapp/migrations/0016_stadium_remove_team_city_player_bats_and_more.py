"""球場を導入し、チームの本拠地（city）を球場へ移す。

所在地は球場が持つものとし、チーム側には地名を残さない。
同じ事実の出典を2つ作らないため。

city には既に地名が入っているので、削除する前に球場へ移し替える。
球場名は実在の名称が分からないため、後から直す前提の仮の名前を入れる。
"""

import django.db.models.deletion
from django.db import migrations, models


def city_to_stadium(apps, schema_editor):
    """チームごとに仮の球場を作り、city をその所在地として移す。"""
    Team = apps.get_model('myapp', 'Team')
    Stadium = apps.get_model('myapp', 'Stadium')

    for team in Team.objects.exclude(city='').exclude(city=None):
        stadium, _ = Stadium.objects.get_or_create(
            name=f'{team.name}の本拠地（球場名未設定）',
            defaults={'city': team.city},
        )
        team.home_stadium = stadium
        team.save(update_fields=['home_stadium'])


def stadium_to_city(apps, schema_editor):
    """巻き戻し。球場の所在地をチームへ書き戻す。"""
    Team = apps.get_model('myapp', 'Team')
    for team in Team.objects.filter(home_stadium__isnull=False).select_related('home_stadium'):
        team.city = team.home_stadium.city
        team.save(update_fields=['city'])


class Migration(migrations.Migration):

    dependencies = [
        ('myapp', '0015_remove_playerstats_player_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='Stadium',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100, unique=True, verbose_name='球場名')),
                ('city', models.CharField(blank=True, max_length=100, verbose_name='所在地')),
                ('capacity', models.PositiveIntegerField(blank=True, null=True, verbose_name='収容人数')),
                ('surface', models.CharField(blank=True, choices=[('天然芝', '天然芝'), ('人工芝', '人工芝'), ('土', '土')], max_length=10, verbose_name='グラウンド')),
                ('opened_year', models.IntegerField(blank=True, null=True, verbose_name='開場年')),
            ],
            options={
                'verbose_name': '球場',
                'verbose_name_plural': '球場',
                'ordering': ['name'],
            },
        ),
        # city を消す前に参照先を用意し、データを移し替える
        migrations.AddField(
            model_name='team',
            name='home_stadium',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='home_teams', to='myapp.stadium', verbose_name='本拠地球場'),
        ),
        migrations.RunPython(city_to_stadium, stadium_to_city),
        migrations.RemoveField(
            model_name='team',
            name='city',
        ),
        migrations.AddField(
            model_name='player',
            name='bats',
            field=models.CharField(blank=True, choices=[('右', '右'), ('左', '左'), ('両', '両')], max_length=2, verbose_name='打'),
        ),
        migrations.AddField(
            model_name='player',
            name='birth_date',
            field=models.DateField(blank=True, null=True, verbose_name='生年月日'),
        ),
        migrations.AddField(
            model_name='player',
            name='birthplace',
            field=models.CharField(blank=True, max_length=100, verbose_name='出身地'),
        ),
        migrations.AddField(
            model_name='player',
            name='debut_year',
            field=models.IntegerField(blank=True, null=True, verbose_name='入団年'),
        ),
        migrations.AddField(
            model_name='player',
            name='height_cm',
            field=models.PositiveIntegerField(blank=True, null=True, verbose_name='身長(cm)'),
        ),
        migrations.AddField(
            model_name='player',
            name='throws',
            field=models.CharField(blank=True, choices=[('右', '右'), ('左', '左'), ('両', '両')], max_length=2, verbose_name='投'),
        ),
        migrations.AddField(
            model_name='player',
            name='weight_kg',
            field=models.PositiveIntegerField(blank=True, null=True, verbose_name='体重(kg)'),
        ),
    ]
