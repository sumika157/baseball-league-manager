"""ワークスペースの台帳（`DjangoWorkspaceAccessQuery`・`DjangoWorkspaceListQuery`。W2 T1）。

key と利用者から、ワークスペースと権限の事実を引く。判定は domain の `WorkspaceAccess` が行うので、ここでは
「どの表の事実から役割が決まるか」「担当チームはそのワークスペースの実データのチームだけか」「クエリ数」を確かめる。
権限の真理値表は `tests/domain/test_workspace_access.py`。
"""

from django.contrib.auth.models import User

from myapp.domain.exceptions import WorkspaceNotFound
from myapp.domain.workspace import Visibility, WorkspaceRole
from myapp.infrastructure import orm_models

from ..helpers import build_workspace_access_query, build_workspace_list_query, make_league, make_workspace
from .base import BaseCase


class WorkspaceLedgerCase(BaseCase):
    """A（`BaseCase` の既定のワークスペース。公開）と B（非公開）、A の世界の球団。

    - A: 持ち主（`owner`）・編集者（`editor`）・`team` の担当（`manager`）。
    - B: 非公開。チーム `b_team` の担当（`b_manager`）と編集者（`b_editor`）。
    - A の世界の球団の担当（`world_manager`）: 世界の球団は実データのチームではないので、担当に数えない。
    - 部外者（`outsider`）: どこにも入っていない。
    """

    owner: User
    editor: User
    manager: User
    outsider: User
    world_manager: User
    b_manager: User
    b_editor: User
    workspace_b: orm_models.Workspace
    b_team: orm_models.Team
    world_team: orm_models.Team

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.owner = cls.workspace.owner
        cls.editor = User.objects.create_user(username="a-editor", password="x")
        orm_models.WorkspaceEditor.objects.create(workspace=cls.workspace, user=cls.editor)
        cls.manager = User.objects.create_user(username="a-manager", password="x")
        cls.team.managers.add(cls.manager)
        cls.outsider = User.objects.create_user(username="outsider", password="x", is_staff=True, is_superuser=True)

        world = orm_models.PennantWorld.objects.create(
            name="Aの世界", workspace=cls.workspace, seed=1, start_year=2026
        )
        world_league = make_league("Aの世界のリーグ", world=world)
        cls.world_team = orm_models.Team.objects.create(league=world_league, name="Aの世界の球団")
        cls.world_manager = User.objects.create_user(username="world-manager", password="x")
        cls.world_team.managers.add(cls.world_manager)

        cls.workspace_b = make_workspace(name="Bのワークスペース", visibility=Visibility.PRIVATE)
        b_league = make_league("Bのリーグ", workspace=cls.workspace_b)
        cls.b_team = orm_models.Team.objects.create(league=b_league, name="Bの球団")
        cls.b_manager = User.objects.create_user(username="b-manager", password="x")
        cls.b_team.managers.add(cls.b_manager)
        cls.b_editor = User.objects.create_user(username="b-editor", password="x")
        orm_models.WorkspaceEditor.objects.create(workspace=cls.workspace_b, user=cls.b_editor)

    def setUp(self):
        super().setUp()
        self.query = build_workspace_access_query()
        self.list_query = build_workspace_list_query()

    def resolve(self, workspace: orm_models.Workspace, user: User | None):
        return self.query.resolve(workspace.key, None if user is None else user.id)


class ResolveTest(WorkspaceLedgerCase):
    def test_the_key_resolves_the_workspace(self):
        context = self.resolve(self.workspace, None)
        self.assertEqual(
            (context.workspace_id, context.key, context.name, context.visibility),
            (self.workspace.id, self.workspace.key, self.workspace.name, Visibility.PUBLIC),
        )
        b = self.resolve(self.workspace_b, None)
        self.assertEqual((b.workspace_id, b.visibility), (self.workspace_b.id, Visibility.PRIVATE))

    def test_an_unknown_key_is_not_found(self):
        for user_id in (None, self.owner.id):
            with self.subTest(user_id=user_id), self.assertRaises(WorkspaceNotFound):
                self.query.resolve("zzzzzzzzzz", user_id)

    def test_each_role_comes_from_the_facts_in_the_tables(self):
        cases = [
            (self.workspace, None, WorkspaceRole.VIEWER),
            (self.workspace, self.owner, WorkspaceRole.OWNER),
            (self.workspace, self.editor, WorkspaceRole.EDITOR),
            (self.workspace, self.manager, WorkspaceRole.TEAM_MANAGER),
            (self.workspace, self.outsider, WorkspaceRole.VIEWER),
            (self.workspace_b, None, WorkspaceRole.NONE),
            (self.workspace_b, self.outsider, WorkspaceRole.NONE),
            (self.workspace_b, self.workspace_b.owner, WorkspaceRole.OWNER),
            (self.workspace_b, self.b_editor, WorkspaceRole.EDITOR),
            (self.workspace_b, self.b_manager, WorkspaceRole.TEAM_MANAGER),
            # A の持ち主・編集者・担当も、B では部外者（非公開なので見えない）
            (self.workspace_b, self.owner, WorkspaceRole.NONE),
            (self.workspace_b, self.editor, WorkspaceRole.NONE),
            (self.workspace_b, self.manager, WorkspaceRole.NONE),
        ]
        for workspace, user, role in cases:
            with self.subTest(workspace=workspace.name, user=getattr(user, "username", None)):
                self.assertIs(self.resolve(workspace, user).access.role, role)

    def test_the_staff_flags_give_nothing(self):
        """`outsider` は is_staff かつ is_superuser。サイトの権限には何も足さない（特権の廃止）。"""
        access = self.resolve(self.workspace_b, self.outsider).access
        self.assertFalse(access.can_view())
        self.assertFalse(self.resolve(self.workspace, self.outsider).access.can_edit_teams([self.team.id]))

    def test_a_manager_of_a_private_workspace_can_view_it(self):
        access = self.resolve(self.workspace_b, self.b_manager).access
        self.assertTrue(access.can_view())
        self.assertTrue(access.can_edit_teams([self.b_team.id]))
        self.assertFalse(access.can_edit_all())


class ManagedTeamsTest(WorkspaceLedgerCase):
    """担当チームは、そのワークスペースの実データのチームだけ（ほかのワークスペース・ペナントの球団は入らない）。"""

    def test_the_managed_teams_are_read(self):
        self.assertEqual(self.resolve(self.workspace, self.manager).access.managed_team_ids, frozenset({self.team.id}))

    def test_a_team_of_a_world_does_not_make_a_manager(self):
        access = self.resolve(self.workspace, self.world_manager).access
        self.assertIs(access.role, WorkspaceRole.VIEWER)
        self.assertEqual(access.managed_team_ids, frozenset())

    def test_a_team_of_another_workspace_does_not_make_a_manager(self):
        access = self.resolve(self.workspace, self.b_manager).access
        self.assertIs(access.role, WorkspaceRole.VIEWER)
        self.assertFalse(access.can_edit_teams([self.b_team.id]))

    def test_only_this_workspaces_real_teams_are_mixed_in(self):
        self.world_team.managers.add(self.manager)
        self.b_team.managers.add(self.manager)
        self.rival.managers.add(self.manager)
        self.assertEqual(
            self.resolve(self.workspace, self.manager).access.managed_team_ids,
            frozenset({self.team.id, self.rival.id}),
        )
        self.assertEqual(
            self.resolve(self.workspace_b, self.manager).access.managed_team_ids, frozenset({self.b_team.id})
        )

    def test_an_editor_who_also_manages_a_team_is_an_editor(self):
        self.team.managers.add(self.editor)
        access = self.resolve(self.workspace, self.editor).access
        self.assertIs(access.role, WorkspaceRole.EDITOR)
        self.assertEqual(access.managed_team_ids, frozenset())


class ResolveQueryCountTest(WorkspaceLedgerCase):
    """持ち主・編集者・未ログインは1クエリ（編集者の行は同じ問い合わせの注釈）。それ以外は担当チームの1本が加わる。"""

    def test_the_number_of_queries_by_role(self):
        cases = [
            ("未ログイン", None, 1),
            ("持ち主", self.owner, 1),
            ("編集者", self.editor, 1),
            ("担当", self.manager, 2),
            ("ログインした部外者", self.outsider, 2),
        ]
        for label, user, count in cases:
            with self.subTest(role=label), self.assertNumQueries(count):
                self.resolve(self.workspace, user)

    def test_an_unknown_key_reads_once(self):
        with self.assertNumQueries(1), self.assertRaises(WorkspaceNotFound):
            self.query.resolve("zzzzzzzzzz", self.manager.id)


class WorkspaceListTest(WorkspaceLedgerCase):
    """`/` の一覧は自分が入っているワークスペースだけ（公開でも、入っていなければ出さない）。"""

    def rows(self, user: User):
        return [(row.key, row.role) for row in self.list_query.list_for(user.id)]

    def test_each_member_sees_the_workspaces_they_belong_to(self):
        self.assertEqual(self.rows(self.owner), [(self.workspace.key, WorkspaceRole.OWNER)])
        self.assertEqual(self.rows(self.editor), [(self.workspace.key, WorkspaceRole.EDITOR)])
        self.assertEqual(self.rows(self.manager), [(self.workspace.key, WorkspaceRole.TEAM_MANAGER)])
        self.assertEqual(self.rows(self.b_manager), [(self.workspace_b.key, WorkspaceRole.TEAM_MANAGER)])

    def test_a_public_workspace_is_not_listed_for_an_outsider(self):
        """A は公開だが、部外者の一覧には出さない（公開ワークスペースの一覧は作らない。2026-10-03 Q3）。"""
        self.assertEqual(self.rows(self.outsider), [])

    def test_the_team_of_a_world_does_not_list_the_workspace(self):
        self.assertEqual(self.rows(self.world_manager), [])

    def test_rows_carry_the_name_and_the_visibility_and_are_sorted_by_name(self):
        orm_models.WorkspaceEditor.objects.create(workspace=self.workspace_b, user=self.manager)
        rows = self.list_query.list_for(self.manager.id)
        self.assertEqual([row.name for row in rows], sorted([self.workspace.name, self.workspace_b.name]))
        by_key = {row.key: row for row in rows}
        self.assertEqual(
            (by_key[self.workspace_b.key].role, by_key[self.workspace_b.key].visibility),
            (WorkspaceRole.EDITOR, Visibility.PRIVATE),
        )
        self.assertEqual(by_key[self.workspace.key].role, WorkspaceRole.TEAM_MANAGER)

    def test_the_role_follows_the_same_priority(self):
        orm_models.WorkspaceEditor.objects.create(workspace=self.workspace, user=self.owner)
        self.team.managers.add(self.owner, self.editor)
        self.assertEqual(self.rows(self.owner), [(self.workspace.key, WorkspaceRole.OWNER)])
        self.assertEqual(self.rows(self.editor), [(self.workspace.key, WorkspaceRole.EDITOR)])

    def test_one_query_however_many_workspaces(self):
        for index in range(3):
            make_workspace(self.manager, f"持っている{index}")
        orm_models.WorkspaceEditor.objects.create(workspace=self.workspace_b, user=self.manager)
        with self.assertNumQueries(1):
            rows = self.list_query.list_for(self.manager.id)
        self.assertEqual(len(rows), 5)
