"""ワークスペースへの招待の表を作る（スキーマだけ。データの移行は無い。W4 T1。#80）。

トークンは SHA-256 のダイジェストだけを持つ。役割の choices はドメインの `InvitedRole` から写す。
招待できるのは持ち主だけなので、作った人の列は持たない（持ち主と同じ事実の出典が2つになる）。
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('myapp', '0063_team_managers_help_text'),
    ]

    operations = [
        migrations.CreateModel(
            name='WorkspaceInvitation',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('token_digest', models.CharField(max_length=64, unique=True, verbose_name='トークンのダイジェスト')),
                ('role', models.CharField(choices=[('edit_all', '全体を編集'), ('edit_teams', '担当チームを編集')], max_length=10, verbose_name='役割')),
                ('expires_at', models.DateTimeField(verbose_name='期限')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='作成日時')),
                ('teams', models.ManyToManyField(blank=True, help_text='役割が「担当チームを編集」のときに、受諾した人が担当になるチーム（このワークスペースの実データのチーム）。', related_name='invitations', to='myapp.team', verbose_name='担当チーム')),
                ('workspace', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='invitations', to='myapp.workspace', verbose_name='ワークスペース')),
            ],
            options={
                'verbose_name': 'ワークスペースへの招待',
                'verbose_name_plural': 'ワークスペースへの招待',
                'ordering': ['id'],
            },
        ),
    ]
