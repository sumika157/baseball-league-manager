"""ワークスペースの制約（スキーマだけ。帰属の backfill は 0061）。

球場・世界のワークスペースを null 不可にし、リーグ名・球場名の一意をワークスペースの中に替え、
リーグが「ワークスペースか世界のどちらか一方」に属すことを CheckConstraint で保つ。

逆方向（0061 まで戻す）は、リーグ名・球場名がワークスペースをまたいで重複していると、
一意制約を戻すところで失敗する（重複が無いときだけ成功する）。
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("myapp", "0061_backfill_default_workspace"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="league",
            name="unique_real_league_name",
        ),
        migrations.AlterField(
            model_name="pennantworld",
            name="workspace",
            field=models.ForeignKey(
                help_text="この世界が属すワークスペース。",
                on_delete=django.db.models.deletion.PROTECT,
                related_name="pennant_worlds",
                to="myapp.workspace",
                verbose_name="ワークスペース",
            ),
        ),
        migrations.AlterField(
            model_name="stadium",
            name="workspace",
            field=models.ForeignKey(
                help_text="この球場が属すワークスペース。",
                on_delete=django.db.models.deletion.PROTECT,
                related_name="stadiums",
                to="myapp.workspace",
                verbose_name="ワークスペース",
            ),
        ),
        migrations.AlterField(
            model_name="stadium",
            name="name",
            field=models.CharField(max_length=100, verbose_name="球場名"),
        ),
        migrations.AddConstraint(
            model_name="league",
            constraint=models.UniqueConstraint(
                fields=("workspace", "name"),
                name="unique_workspace_league_name",
                violation_error_message="同じワークスペースに同じ名前のリーグがあります。",
            ),
        ),
        migrations.AddConstraint(
            model_name="league",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("workspace__isnull", False), ("world__isnull", True)),
                    models.Q(("workspace__isnull", True), ("world__isnull", False)),
                    _connector="OR",
                ),
                name="league_workspace_xor_world",
                violation_error_message="リーグはワークスペースか世界のどちらか一方にだけ属します。",
            ),
        ),
        migrations.AddConstraint(
            model_name="stadium",
            constraint=models.UniqueConstraint(
                fields=("workspace", "name"),
                name="unique_workspace_stadium_name",
                violation_error_message="同じワークスペースに同じ名前の球場があります。",
            ),
        ),
    ]
