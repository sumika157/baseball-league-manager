"""0061: 既存のリーグ・球場・世界を既定のワークスペースに帰属させるデータ移行の検査。"""

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

BEFORE = [("myapp", "0060_workspace")]
AFTER = [("myapp", "0061_backfill_default_workspace")]


class BackfillDefaultWorkspaceMigrationTest(TransactionTestCase):
    def tearDown(self):
        # 後続のテストのために最新の状態へ戻す
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def _migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        return MigrationExecutor(connection).loader.project_state(targets).apps

    def test_backfill_and_reverse(self):
        old = self._migrate(BEFORE)
        user = old.get_model("auth", "User")
        user.objects.create(username="staff_a", is_staff=True)
        super_late = user.objects.create(username="super_late", is_staff=True, is_superuser=True)
        user.objects.create(username="super_first", is_staff=True, is_superuser=True)
        user.objects.create(username="staff_b", is_staff=True)
        plain = user.objects.create(username="plain")
        # staff でない superuser も、今は全権を持つので編集者になる。無効なユーザーはならない
        user.objects.create(username="super_only", is_superuser=True)
        user.objects.create(username="inactive_staff", is_staff=True, is_active=False)
        # 最小 id の superuser は super_late（staff_a が先に作られていても superuser を優先する）
        league = old.get_model("myapp", "League")
        real_a = league.objects.create(name="実リーグA")
        league.objects.create(name="実リーグB")
        world = old.get_model("myapp", "PennantWorld").objects.create(
            name="世界", owner_id=plain.id, seed=1, start_year=2030
        )
        league.objects.create(name="ペナントのリーグ", world=world)
        stadium = old.get_model("myapp", "Stadium")
        stadium.objects.create(name="球場1")
        stadium.objects.create(name="球場2")
        team = old.get_model("myapp", "Team").objects.create(league=real_a, name="チーム")
        team.managers.add(plain)

        new = self._migrate(AFTER)

        Workspace = new.get_model("myapp", "Workspace")
        League = new.get_model("myapp", "League")
        self.assertEqual(Workspace.objects.count(), 1)
        workspace = Workspace.objects.get()
        self.assertEqual(workspace.visibility, "public")
        self.assertRegex(workspace.key, r"^[a-z0-9]{10}$")
        # 帰属: 実データのリーグ・全球場・全世界。ペナントのリーグは付かない
        self.assertEqual(
            sorted(League.objects.filter(workspace=workspace).values_list("name", flat=True)),
            ["実リーグA", "実リーグB"],
        )
        self.assertIsNone(League.objects.get(name="ペナントのリーグ").workspace_id)
        Stadium = new.get_model("myapp", "Stadium")
        self.assertEqual(Stadium.objects.count(), 2)
        self.assertEqual(Stadium.objects.filter(workspace=workspace).count(), 2)
        self.assertEqual(new.get_model("myapp", "PennantWorld").objects.get().workspace_id, workspace.id)
        # 持ち主は最小 id の superuser。ほかの staff は編集者で、staff でない人は編集者にならない
        self.assertEqual(workspace.owner_id, super_late.id)
        editors = new.get_model("myapp", "WorkspaceEditor").objects.filter(workspace=workspace)
        self.assertEqual(
            sorted(editors.values_list("user__username", flat=True)),
            ["staff_a", "staff_b", "super_first", "super_only"],
        )
        # チームの担当はそのまま
        self.assertEqual(
            list(new.get_model("myapp", "Team").objects.get(id=team.id).managers.values_list("id", flat=True)),
            [plain.id],
        )

        # 逆方向は何もしない: ワークスペース・編集者・帰属はそのまま残る
        back = self._migrate(BEFORE)
        self.assertEqual(back.get_model("myapp", "Workspace").objects.count(), 1)
        self.assertEqual(back.get_model("myapp", "WorkspaceEditor").objects.count(), 4)
        self.assertEqual(back.get_model("myapp", "League").objects.filter(workspace__isnull=False).count(), 2)
        self.assertEqual(back.get_model("myapp", "Stadium").objects.filter(workspace__isnull=False).count(), 2)
        self.assertEqual(back.get_model("myapp", "PennantWorld").objects.filter(workspace__isnull=False).count(), 1)

        # 戻したあとに別のワークスペースができていても、再び適用して増減・混入が起きない
        back.get_model("myapp", "Workspace").objects.create(
            key="zzzzzzzzzz", name="あとから", owner_id=plain.id, visibility="private"
        )
        again = self._migrate(AFTER)
        self.assertEqual(again.get_model("myapp", "Workspace").objects.count(), 2)
        self.assertEqual(again.get_model("myapp", "WorkspaceEditor").objects.count(), 4)
        self.assertEqual(again.get_model("myapp", "Stadium").objects.filter(workspace__key="zzzzzzzzzz").count(), 0)
        self.assertEqual(again.get_model("myapp", "Workspace").objects.get(id=workspace.id).owner_id, super_late.id)

    def test_without_superuser_the_lowest_staff_is_the_owner(self):
        old = self._migrate(BEFORE)
        user = old.get_model("auth", "User")
        user.objects.create(username="plain")
        first = user.objects.create(username="staff_first", is_staff=True)
        user.objects.create(username="staff_second", is_staff=True)
        old.get_model("myapp", "Stadium").objects.create(name="球場")

        new = self._migrate(AFTER)

        workspace = new.get_model("myapp", "Workspace").objects.get()
        self.assertEqual(workspace.owner_id, first.id)
        editors = new.get_model("myapp", "WorkspaceEditor").objects.values_list("user__username", flat=True)
        self.assertEqual(list(editors), ["staff_second"])

    def test_stops_with_a_japanese_error_without_superuser_or_staff(self):
        old = self._migrate(BEFORE)
        old.get_model("auth", "User").objects.create(username="plain")
        old.get_model("myapp", "League").objects.create(name="リーグ")

        with self.assertRaisesMessage(RuntimeError, "先に createsuperuser を実行してください"):
            self._migrate(AFTER)

        # 失敗したので適用されず、ワークスペースもできない
        self.assertNotIn(AFTER[0], MigrationExecutor(connection).loader.applied_migrations)
        self.assertEqual(old.get_model("myapp", "Workspace").objects.count(), 0)
        # tearDown が最新まで進めるときに同じ理由で止まらないよう、持ち主になれる人を足す
        old.get_model("auth", "User").objects.create(username="admin", is_superuser=True)

    def test_empty_database_creates_nothing(self):
        # ユーザーだけがいて、リーグ・球場・世界が無い
        old = self._migrate(BEFORE)
        old.get_model("auth", "User").objects.create(username="admin", is_staff=True, is_superuser=True)

        new = self._migrate(AFTER)

        self.assertEqual(new.get_model("myapp", "Workspace").objects.count(), 0)
        self.assertEqual(new.get_model("myapp", "WorkspaceEditor").objects.count(), 0)
