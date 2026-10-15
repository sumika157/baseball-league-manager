# 外国人選手の登録枠（保有の上限）と出場枠（1試合の上限）を消す（#204 段階A）。協約 第82条の2 は保有を縛らず、
# 1試合の出場者は1軍の中から出るので、1軍登録の枠（0049 の3列）1つにする。
# 破壊的な変更なので、適用前に db.sqlite3 をバックアップする。逆方向は列を既定値（登録枠5・出場枠3）で戻す。

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('myapp', '0049_league_foreign_active_limits'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='league',
            name='foreign_player_game_limit',
        ),
        migrations.RemoveField(
            model_name='league',
            name='foreign_player_roster_limit',
        ),
    ]
