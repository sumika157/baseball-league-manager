"""チームの担当者の help_text を W2 の後の権限に合わせる（文言だけ。スキーマは変わらない。W2 T6）。

管理ユーザー（is_staff）はサイトで何の権限も持たなくなった（W2 T5）ので、「管理ユーザーは常に編集できます」を消した。
"""

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('myapp', '0062_workspace_constraints'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name='team',
            name='managers',
            field=models.ManyToManyField(blank=True, help_text='このチームの選手・試合を編集できるユーザー。ワークスペースの持ち主・編集者は担当に関わらず全チームを編集できます。管理画面に入れること（スタッフ権限）は、サイトでの編集の権限に何も足しません。', related_name='managed_teams', to=settings.AUTH_USER_MODEL, verbose_name='担当者'),
        ),
    ]
