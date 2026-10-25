"""既存のリーグ・球場・世界を、既定のワークスペース1つに帰属させる（データ移行）。

スキーマ変更（0060）とは別ファイルにしている。凍結した履歴なので、domain は import せず、
key の長さと文字種（`domain/workspace.py` の `KEY_LENGTH`・`KEY_ALPHABET`）と公開の値をここに写している。
"""

import random
import string

from django.conf import settings
from django.db import migrations
from django.db.models import Q

# domain/workspace.py の KEY_LENGTH・KEY_ALPHABET の写し（履歴は凍結するので import しない）
KEY_LENGTH = 10
KEY_ALPHABET = string.ascii_lowercase + string.digits
# domain/workspace.py の Visibility.PUBLIC の値の写し（同じ理由で import しない）
VISIBILITY_PUBLIC = "public"

DEFAULT_WORKSPACE_NAME = "既定のワークスペース"


def _new_key(workspace_model) -> str:
    rng = random.SystemRandom()
    while True:
        key = "".join(rng.choice(KEY_ALPHABET) for _ in range(KEY_LENGTH))
        if not workspace_model.objects.filter(key=key).exists():
            return key


def backfill_default_workspace(apps, schema_editor):
    League = apps.get_model("myapp", "League")
    Stadium = apps.get_model("myapp", "Stadium")
    PennantWorld = apps.get_model("myapp", "PennantWorld")
    Workspace = apps.get_model("myapp", "Workspace")
    WorkspaceEditor = apps.get_model("myapp", "WorkspaceEditor")
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))

    # すでにワークスペースがあれば何もしない。戻して再び適用しても、あとから作られたワークスペースを
    # 重複させたり、非公開のものを1つの公開ワークスペースに混ぜたりしないため
    if Workspace.objects.exists():
        return

    # リーグ・球場・世界のどれも無ければ、ワークスペースは作らない（最初の1つは管理画面で作る）
    if not (League.objects.exists() or Stadium.objects.exists() or PennantWorld.objects.exists()):
        return

    active_users = User.objects.filter(is_active=True)
    owner = (
        active_users.filter(is_superuser=True).order_by("id").first()
        or active_users.filter(is_staff=True).order_by("id").first()
    )
    if owner is None:
        raise RuntimeError(
            "既存のデータをワークスペースに移すには持ち主が必要です。先に createsuperuser を実行してください"
            "（docker compose -f docker-compose.prod.yml run --rm --no-deps --entrypoint python web "
            "manage.py createsuperuser）。"
        )

    workspace = Workspace.objects.create(
        key=_new_key(Workspace),
        name=DEFAULT_WORKSPACE_NAME,
        owner=owner,
        visibility=VISIBILITY_PUBLIC,
    )
    # これまで全権を持っていた人（staff または superuser）が権限を失わないよう、ほかの人は編集者にする
    editors = active_users.filter(Q(is_staff=True) | Q(is_superuser=True)).exclude(id=owner.id).order_by("id")
    WorkspaceEditor.objects.bulk_create(WorkspaceEditor(workspace=workspace, user=user) for user in editors)
    # ペナントのリーグ（world が空でない）は世界からたどるので、ここでは付けない
    League.objects.filter(world__isnull=True).update(workspace=workspace)
    Stadium.objects.update(workspace=workspace)
    PennantWorld.objects.update(workspace=workspace)


class Migration(migrations.Migration):
    dependencies = [
        ("myapp", "0060_workspace"),
    ]

    operations = [
        # 逆方向は何もしない。0060 では列が残るので帰属はそのままでよく（0060 より前へ戻れば列ごと消える）、
        # 戻して再び適用するときに、あとから作られたワークスペースを消してはならないため
        migrations.RunPython(backfill_default_workspace, migrations.RunPython.noop),
    ]
