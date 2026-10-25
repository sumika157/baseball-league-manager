"""ワークスペースの作成・設定（`WorkspaceService`）と、招待・メンバー（`WorkspaceMemberService`）の往復（W4 T1。#80）。

- 作成: 空のワークスペースができる。1人3つまで（`settings.WORKSPACE_MAX_OWNED`）で、4つ目は何も作らない。
- 設定: 名前と公開範囲だけが変わる（メンバーは変わらない）。
- 担当（`Team.managers`）の保存は、このワークスペースの実データのチームの中間表の行だけを集約の状態にする。
  ほかのワークスペースのチーム・ペナントの世界の球団の担当には触れず、範囲の外のチームの id は拒む（何も書かない）。
  管理画面で付けた重なった行（持ち主に担当の行など）は、読むときに落とさない。
- 招待: トークンはダイジェストだけを保存し、期限内に1回だけ使える。受諾の保存と招待を消すことは1つのトランザクション。
- 参照（`WorkspaceManagementQuery`・`InvitationPreviewQuery`）: 人数・招待の数によらないクエリ数で読む。
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from myapp.domain.exceptions import (
    InvalidInvitation,
    InvalidWorkspace,
    MemberNotFound,
    WorkspaceLimitReached,
    WorkspaceNotFound,
)
from myapp.domain.workspace import (
    INVITATION_TTL,
    KEY_LENGTH,
    InvitedRole,
    Visibility,
    WorkspaceRole,
    invitation_digest,
    validate_workspace_key,
)
from myapp.infrastructure import orm_models
from myapp.infrastructure.repositories import DjangoInvitationRepository, DjangoWorkspaceRepository

from ..helpers import (
    build_invitation_preview_query,
    build_workspace_management_query,
    build_workspace_member_service,
    build_workspace_service,
    make_league,
    make_pennant_world,
    make_workspace,
)

MANAGERS = orm_models.Team.managers.through
NOW = datetime(2026, 10, 25, 3, 0, tzinfo=UTC)
CLOCK = "myapp.presentation.views.timezone.now"


def _managers_of(*teams: orm_models.Team) -> set[tuple[int, int]]:
    return set(MANAGERS.objects.filter(team__in=teams).values_list("team_id", "user_id"))


class WorkspaceCase(TestCase):
    """ワークスペース A（持ち主 `owner`。実データのチーム2つと、世界の球団1つ）と B（チーム1つ）。"""

    owner: User
    editor: User
    manager: User
    outsider: User
    workspace: orm_models.Workspace
    team: orm_models.Team
    rival: orm_models.Team
    world_team: orm_models.Team
    other: orm_models.Workspace
    other_team: orm_models.Team

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(username="owner", password="x")
        cls.editor = User.objects.create_user(username="editor", password="x")
        cls.manager = User.objects.create_user(username="manager", password="x")
        cls.outsider = User.objects.create_user(username="outsider", password="x")
        cls.workspace = make_workspace(cls.owner, "A", visibility=Visibility.PRIVATE)
        league = make_league("A のリーグ", workspace=cls.workspace)
        cls.team = orm_models.Team.objects.create(league=league, name="A1", display_order=2)
        cls.rival = orm_models.Team.objects.create(league=league, name="A2", display_order=1)
        world = make_pennant_world("A の世界", workspace=cls.workspace, seed=1, start_year=2026)
        world_league = make_league("世界のリーグ", world=world)
        cls.world_team = orm_models.Team.objects.create(league=world_league, name="世界の球団")
        cls.other = make_workspace(name="B")
        other_league = make_league("B のリーグ", workspace=cls.other)
        cls.other_team = orm_models.Team.objects.create(league=other_league, name="B1")

        orm_models.WorkspaceEditor.objects.create(workspace=cls.workspace, user=cls.editor)
        cls.team.managers.add(cls.manager)
        # 範囲の外の担当（A の保存で変わってはならない）
        cls.other_team.managers.add(cls.manager)
        cls.world_team.managers.add(cls.manager, cls.outsider)

    def ledger(self):
        return DjangoWorkspaceRepository()


class CreateTest(WorkspaceCase):
    def test_create_makes_an_empty_workspace(self):
        key = build_workspace_service().create(
            owner_id=self.outsider.id, name=" 新しいリーグ ", visibility=Visibility.PRIVATE
        )

        self.assertEqual(validate_workspace_key(key), key)
        self.assertEqual(len(key), KEY_LENGTH)
        row = orm_models.Workspace.objects.get(key=key)
        self.assertEqual((row.name, row.owner_id, row.visibility), ("新しいリーグ", self.outsider.id, "private"))
        self.assertFalse(orm_models.WorkspaceEditor.objects.filter(workspace=row).exists())
        self.assertFalse(orm_models.League.objects.filter(workspace=row).exists())

    def test_up_to_three_can_be_created_and_the_fourth_is_refused(self):
        service = build_workspace_service()
        for index in range(3):
            self.assertTrue(service.can_create(self.outsider.id))
            service.create(owner_id=self.outsider.id, name=f"W{index}", visibility=Visibility.PUBLIC)
        self.assertFalse(service.can_create(self.outsider.id))
        before = orm_models.Workspace.objects.count()

        with self.assertRaisesRegex(WorkspaceLimitReached, "1人3つまで"):
            service.create(owner_id=self.outsider.id, name="W3", visibility=Visibility.PUBLIC)
        self.assertEqual(orm_models.Workspace.objects.count(), before)

    def test_the_limit_counts_only_the_owners_workspaces(self):
        """編集者・担当として入っているワークスペースは数えない（持っている数だけ）。"""
        service = build_workspace_service()
        for index in range(3):
            service.create(owner_id=self.editor.id, name=f"W{index}", visibility=Visibility.PUBLIC)
        self.assertEqual(self.ledger().count_owned_by(self.editor.id), 3)
        self.assertEqual(self.ledger().count_owned_by(self.manager.id), 0)

    @override_settings(WORKSPACE_MAX_OWNED=1)
    def test_the_limit_comes_from_the_settings(self):
        service = build_workspace_service()
        self.assertFalse(service.can_create(self.owner.id))
        self.assertEqual(service.limit_message, "ワークスペースは1人1つまで作れます。")
        with self.assertRaises(WorkspaceLimitReached):
            service.create(owner_id=self.owner.id, name="2つ目", visibility=Visibility.PUBLIC)

    def test_a_bad_name_creates_nothing(self):
        before = orm_models.Workspace.objects.count()
        with self.assertRaises(InvalidWorkspace):
            build_workspace_service().create(owner_id=self.outsider.id, name="  ", visibility=Visibility.PUBLIC)
        self.assertEqual(orm_models.Workspace.objects.count(), before)

    def test_add_refuses_a_saved_workspace(self):
        workspace = self.ledger().find_by_id(self.workspace.id)
        with self.assertRaises(InvalidWorkspace):
            self.ledger().add(workspace)


class SettingsTest(WorkspaceCase):
    def test_change_settings_changes_only_the_name_and_the_visibility(self):
        managers = set(MANAGERS.objects.values_list("team_id", "user_id"))

        build_workspace_service().change_settings(self.workspace.id, name="改名", visibility=Visibility.PUBLIC)

        row = orm_models.Workspace.objects.get(id=self.workspace.id)
        self.assertEqual(
            (row.name, row.visibility, row.key, row.owner_id), ("改名", "public", self.workspace.key, self.owner.id)
        )
        self.assertEqual(set(MANAGERS.objects.values_list("team_id", "user_id")), managers)
        self.assertEqual(list(row.editors.values_list("user_id", flat=True)), [self.editor.id])
        self.assertEqual(orm_models.Workspace.objects.get(id=self.other.id).name, "B")

    def test_a_missing_workspace_is_not_found(self):
        with self.assertRaises(WorkspaceNotFound):
            build_workspace_service().change_settings(0, name="x", visibility=Visibility.PUBLIC)


class RepositoryTest(WorkspaceCase):
    def test_find_by_id_reads_only_the_real_teams_of_the_workspace(self):
        workspace = self.ledger().find_by_id(self.workspace.id)

        self.assertEqual((workspace.name, workspace.key, workspace.owner_id), ("A", self.workspace.key, self.owner.id))
        self.assertEqual(workspace.editor_ids, frozenset({self.editor.id}))
        # B のチーム・世界の球団の担当は入らない（外の人 `outsider` は世界の球団の担当だけ）
        self.assertEqual(workspace.team_assignments, {self.manager.id: frozenset({self.team.id})})

    def test_a_missing_workspace_is_not_found(self):
        with self.assertRaises(WorkspaceNotFound):
            self.ledger().find_by_id(0)

    def test_overlapping_rows_are_read_and_dropped_on_save(self):
        """管理画面で付けた重なった行（持ち主に編集者・担当の行、編集者に担当の行）を読んでも落ちない。"""
        orm_models.WorkspaceEditor.objects.create(workspace=self.workspace, user=self.owner)
        self.team.managers.add(self.owner, self.editor)

        workspace = self.ledger().find_by_id(self.workspace.id)
        self.assertEqual(workspace.editor_ids, frozenset({self.editor.id}))
        self.assertEqual(workspace.team_assignments, {self.manager.id: frozenset({self.team.id})})

        self.ledger().save(workspace)
        self.assertEqual(
            set(orm_models.WorkspaceEditor.objects.filter(workspace=self.workspace).values_list("user_id", flat=True)),
            {self.editor.id},
        )
        self.assertEqual(_managers_of(self.team, self.rival), {(self.team.id, self.manager.id)})

    def test_save_writes_only_the_rows_of_the_real_teams(self):
        outside = _managers_of(self.other_team, self.world_team)
        workspace = self.ledger().find_by_id(self.workspace.id)
        workspace.assign_teams(self.manager.id, [self.rival.id])
        workspace.assign_teams(self.outsider.id, [self.team.id, self.rival.id])

        self.ledger().save(workspace)

        self.assertEqual(
            _managers_of(self.team, self.rival),
            {(self.rival.id, self.manager.id), (self.team.id, self.outsider.id), (self.rival.id, self.outsider.id)},
        )
        self.assertEqual(_managers_of(self.other_team, self.world_team), outside, "範囲の外の担当が変わった")

    def test_teams_outside_the_workspace_are_refused_and_nothing_is_written(self):
        for team in (self.other_team, self.world_team):
            with self.subTest(team=team.name):
                editors = list(orm_models.WorkspaceEditor.objects.values_list("workspace_id", "user_id"))
                managers = set(MANAGERS.objects.values_list("team_id", "user_id"))
                workspace = self.ledger().find_by_id(self.workspace.id)
                workspace.rename("書かれてはならない名前")
                workspace.make_editor(self.outsider.id)
                workspace.assign_teams(self.manager.id, [self.team.id, team.id])

                with self.assertRaises(InvalidWorkspace):
                    self.ledger().save(workspace)

                self.assertEqual(orm_models.Workspace.objects.get(id=self.workspace.id).name, "A")
                self.assertEqual(
                    list(orm_models.WorkspaceEditor.objects.values_list("workspace_id", "user_id")), editors
                )
                self.assertEqual(set(MANAGERS.objects.values_list("team_id", "user_id")), managers)

    def test_saving_a_missing_workspace_is_not_found(self):
        workspace = self.ledger().find_by_id(self.workspace.id)
        workspace.id = 0
        with self.assertRaises(WorkspaceNotFound):
            self.ledger().save(workspace)


class InvitationTest(WorkspaceCase):
    def issue(self, role=InvitedRole.EDIT_ALL, team_ids=(), *, workspace=None, now=NOW):
        with patch(CLOCK, return_value=now):
            service = build_workspace_member_service()
        return service.issue_invitation((workspace or self.workspace).id, role=role, team_ids=team_ids)

    def accept(self, token, user, *, now=NOW):
        with patch(CLOCK, return_value=now):
            service = build_workspace_member_service()
        return service.accept_invitation(token, user.id)

    def test_only_the_digest_is_stored(self):
        issued = self.issue(InvitedRole.EDIT_TEAMS, [self.team.id, self.rival.id])

        row = orm_models.WorkspaceInvitation.objects.get(id=issued.invitation_id)
        self.assertEqual(row.token_digest, invitation_digest(issued.token))
        self.assertNotIn(issued.token, str(list(orm_models.WorkspaceInvitation.objects.values())))
        self.assertEqual(row.expires_at, NOW + INVITATION_TTL)
        self.assertEqual(issued.expires_at, NOW + INVITATION_TTL)
        self.assertEqual(set(row.teams.values_list("id", flat=True)), {self.team.id, self.rival.id})
        self.assertEqual((row.workspace_id, row.role), (self.workspace.id, "edit_teams"))

    def test_teams_outside_the_workspace_cannot_be_invited_to(self):
        for team in (self.other_team, self.world_team):
            with self.subTest(team=team.name), self.assertRaises(InvalidInvitation):
                self.issue(InvitedRole.EDIT_TEAMS, [self.team.id, team.id])
        self.assertFalse(orm_models.WorkspaceInvitation.objects.exists())

    def test_issuing_removes_the_expired_invitations_of_the_workspace_only(self):
        old = self.issue(now=NOW - INVITATION_TTL - timedelta(seconds=1))
        other_old = self.issue(workspace=self.other, now=NOW - INVITATION_TTL - timedelta(seconds=1))
        valid = self.issue(now=NOW - timedelta(days=1))

        self.issue()

        ids = set(orm_models.WorkspaceInvitation.objects.values_list("id", flat=True))
        self.assertNotIn(old.invitation_id, ids)
        self.assertIn(other_old.invitation_id, ids, "ほかのワークスペースの招待を消した")
        self.assertIn(valid.invitation_id, ids)

    def test_accepting_makes_an_editor_and_uses_up_the_invitation(self):
        issued = self.issue()

        key = self.accept(issued.token, self.outsider)

        self.assertEqual(key, self.workspace.key)
        self.assertIn(self.outsider.id, self.ledger().find_by_id(self.workspace.id).editor_ids)
        self.assertFalse(orm_models.WorkspaceInvitation.objects.filter(id=issued.invitation_id).exists())
        # 2回目は使えない
        newcomer = User.objects.create_user(username="newcomer", password="x")
        with self.assertRaises(InvalidInvitation):
            self.accept(issued.token, newcomer)
        self.assertFalse(self.ledger().find_by_id(self.workspace.id).is_member(newcomer.id))

    def test_accepting_edit_teams_makes_a_manager_of_the_teams(self):
        issued = self.issue(InvitedRole.EDIT_TEAMS, [self.rival.id])

        self.accept(issued.token, self.outsider)

        self.assertIn((self.rival.id, self.outsider.id), _managers_of(self.rival))
        self.assertEqual(
            self.ledger().find_by_id(self.workspace.id).team_assignments[self.outsider.id], frozenset({self.rival.id})
        )

    def test_an_expired_invitation_cannot_be_accepted(self):
        issued = self.issue()
        with self.assertRaises(InvalidInvitation):
            self.accept(issued.token, self.outsider, now=NOW + INVITATION_TTL)
        self.assertFalse(self.ledger().find_by_id(self.workspace.id).is_member(self.outsider.id))

    def test_members_cannot_accept_and_the_invitation_is_kept(self):
        issued = self.issue(InvitedRole.EDIT_TEAMS, [self.rival.id])
        for user in (self.owner, self.editor, self.manager):
            with self.subTest(user=user.username), self.assertRaises(InvalidInvitation):
                self.accept(issued.token, user)
        self.assertTrue(orm_models.WorkspaceInvitation.objects.filter(id=issued.invitation_id).exists())
        self.assertEqual(_managers_of(self.rival), set())

    def test_an_unknown_token_cannot_be_accepted(self):
        for token in ("", "unknown"):
            with self.subTest(token=token), self.assertRaises(InvalidInvitation):
                self.accept(token, self.outsider)

    def test_an_invitation_whose_teams_were_all_deleted_cannot_be_accepted(self):
        issued = self.issue(InvitedRole.EDIT_TEAMS, [self.rival.id])
        self.rival.delete()
        with self.assertRaises(InvalidInvitation):
            self.accept(issued.token, self.outsider)
        self.assertFalse(self.ledger().find_by_id(self.workspace.id).is_member(self.outsider.id))

    def test_membership_is_rolled_back_when_the_invitation_cannot_be_used_up(self):
        """招待を消せなければ（同時に別の人が使った）、メンバーの保存も戻る（1回だけ使える）。"""
        issued = self.issue()
        with (
            patch.object(DjangoInvitationRepository, "delete", side_effect=InvalidInvitation("使用済み")),
            self.assertRaises(InvalidInvitation),
        ):
            self.accept(issued.token, self.outsider)
        self.assertFalse(orm_models.WorkspaceEditor.objects.filter(user=self.outsider).exists())

    def test_revoke_removes_only_the_invitation_of_the_workspace(self):
        mine = self.issue()
        others = self.issue(workspace=self.other)
        service = build_workspace_member_service()

        with self.assertRaises(InvalidInvitation):
            service.revoke_invitation(self.workspace.id, others.invitation_id)
        service.revoke_invitation(self.workspace.id, mine.invitation_id)

        self.assertEqual(
            list(orm_models.WorkspaceInvitation.objects.values_list("id", flat=True)), [others.invitation_id]
        )
        with self.assertRaises(InvalidInvitation):
            self.accept(mine.token, self.outsider)


class MemberTest(WorkspaceCase):
    def test_change_role_switches_between_editor_and_manager(self):
        service = build_workspace_member_service()
        service.change_role(self.workspace.id, self.editor.id, role=InvitedRole.EDIT_TEAMS, team_ids=[self.rival.id])
        service.change_role(self.workspace.id, self.manager.id, role=InvitedRole.EDIT_ALL)

        workspace = self.ledger().find_by_id(self.workspace.id)
        self.assertEqual(workspace.editor_ids, frozenset({self.manager.id}))
        self.assertEqual(workspace.team_assignments, {self.editor.id: frozenset({self.rival.id})})
        # B のチーム・世界の球団の担当はそのまま
        self.assertIn((self.other_team.id, self.manager.id), _managers_of(self.other_team))
        self.assertIn((self.world_team.id, self.manager.id), _managers_of(self.world_team))

    def test_change_role_needs_a_member(self):
        with self.assertRaises(MemberNotFound):
            build_workspace_member_service().change_role(
                self.workspace.id, self.outsider.id, role=InvitedRole.EDIT_ALL
            )
        self.assertFalse(orm_models.WorkspaceEditor.objects.filter(user=self.outsider).exists())

    def test_change_role_refuses_teams_of_another_workspace(self):
        with self.assertRaises(InvalidWorkspace):
            build_workspace_member_service().change_role(
                self.workspace.id, self.manager.id, role=InvitedRole.EDIT_TEAMS, team_ids=[self.other_team.id]
            )
        self.assertEqual(_managers_of(self.team, self.rival), {(self.team.id, self.manager.id)})

    def test_remove_member_drops_every_assignment_in_the_workspace(self):
        self.rival.managers.add(self.manager)
        service = build_workspace_member_service()

        service.remove_member(self.workspace.id, self.manager.id)
        service.remove_member(self.workspace.id, self.editor.id)

        workspace = self.ledger().find_by_id(self.workspace.id)
        self.assertEqual((workspace.editor_ids, workspace.team_assignments), (frozenset(), {}))
        self.assertEqual(_managers_of(self.team, self.rival), set())
        self.assertIn((self.other_team.id, self.manager.id), _managers_of(self.other_team), "B の担当を外した")

    def test_remove_member_refuses_the_owner_and_outsiders(self):
        service = build_workspace_member_service()
        with self.assertRaises(InvalidWorkspace):
            service.remove_member(self.workspace.id, self.owner.id)
        with self.assertRaises(MemberNotFound):
            service.remove_member(self.workspace.id, self.outsider.id)


class ManagementQueryTest(WorkspaceCase):
    def test_members_are_the_owner_the_editors_and_the_managers(self):
        self.rival.managers.add(self.manager)
        # 重なった行は上の役割だけ
        self.team.managers.add(self.owner, self.editor)

        rows = build_workspace_management_query(self.workspace.id).members()

        self.assertEqual(
            [(row.username, row.role, row.team_names) for row in rows],
            [
                ("owner", WorkspaceRole.OWNER, ()),
                ("editor", WorkspaceRole.EDITOR, ()),
                # 担当チームはチームの表示順（A2 → A1）。B のチーム・世界の球団は出ない
                ("manager", WorkspaceRole.TEAM_MANAGER, ("A2", "A1")),
            ],
        )
        self.assertTrue(rows[0].is_owner)
        self.assertEqual(rows[2].team_ids, (self.rival.id, self.team.id))

    def test_members_are_read_with_a_fixed_number_of_queries(self):
        query = build_workspace_management_query(self.workspace.id)
        with CaptureQueriesContext(connection) as few:
            query.members()
        for index in range(5):
            user = User.objects.create_user(username=f"more{index}", password="x")
            orm_models.WorkspaceEditor.objects.create(workspace=self.workspace, user=user)
            self.rival.managers.add(User.objects.create_user(username=f"manager{index}", password="x"))
        with CaptureQueriesContext(connection) as many:
            self.assertEqual(len(query.members()), 13)
        self.assertEqual(len(few), 3)
        self.assertEqual(len(many), len(few))

    def test_a_missing_workspace_is_not_found(self):
        with self.assertRaises(WorkspaceNotFound):
            build_workspace_management_query(0).members()

    def test_invitations_are_the_valid_ones_of_the_workspace(self):
        with patch(CLOCK, return_value=NOW - INVITATION_TTL - timedelta(days=1)):
            build_workspace_member_service().issue_invitation(
                self.workspace.id, role=InvitedRole.EDIT_ALL, team_ids=()
            )
        with patch(CLOCK, return_value=NOW):
            service = build_workspace_member_service()
            first = service.issue_invitation(
                self.workspace.id, role=InvitedRole.EDIT_TEAMS, team_ids=[self.team.id, self.rival.id]
            )
            second = service.issue_invitation(self.workspace.id, role=InvitedRole.EDIT_ALL, team_ids=())
            service.issue_invitation(self.other.id, role=InvitedRole.EDIT_ALL, team_ids=())
        query = build_workspace_management_query(self.workspace.id)

        with CaptureQueriesContext(connection) as queries:
            rows = query.invitations(NOW)

        self.assertEqual([row.invitation_id for row in rows], [second.invitation_id, first.invitation_id])
        self.assertEqual(rows[1].team_names, ("A2", "A1"))
        self.assertEqual(rows[1].role, InvitedRole.EDIT_TEAMS)
        self.assertEqual(rows[0].team_names, ())
        self.assertEqual(len(queries), 2)
        # 期限を過ぎたら出ない
        self.assertEqual(query.invitations(NOW + INVITATION_TTL), [])


class PreviewQueryTest(WorkspaceCase):
    def setUp(self):
        with patch(CLOCK, return_value=NOW):
            self.issued = build_workspace_member_service().issue_invitation(
                self.workspace.id, role=InvitedRole.EDIT_TEAMS, team_ids=[self.team.id, self.rival.id]
            )

    def test_preview_shows_the_workspace_and_the_role(self):
        preview = build_invitation_preview_query().preview(self.issued.token, None, NOW)

        self.assertEqual((preview.workspace_key, preview.workspace_name), (self.workspace.key, "A"))
        self.assertEqual(preview.role, InvitedRole.EDIT_TEAMS)
        self.assertEqual(preview.team_names, ("A2", "A1"))
        self.assertEqual(preview.expires_at, NOW + INVITATION_TTL)
        self.assertFalse(preview.is_member)

    def test_preview_tells_whether_the_viewer_is_already_a_member(self):
        query = build_invitation_preview_query()
        # B の担当・世界の球団だけの担当（outsider）は A のメンバーではない
        expected = {self.owner: True, self.editor: True, self.manager: True, self.outsider: False}
        for user, is_member in expected.items():
            with self.subTest(user=user.username):
                self.assertIs(query.preview(self.issued.token, user.id, NOW).is_member, is_member)

    def test_unusable_invitations_are_all_the_same(self):
        query = build_invitation_preview_query()
        for token, now in (("unknown", NOW), ("", NOW), (self.issued.token, NOW + INVITATION_TTL)):
            with self.subTest(token=token, now=now), self.assertRaisesRegex(InvalidInvitation, "見つからないか"):
                query.preview(token, None, now)
        self.team.delete()
        self.rival.delete()
        with self.assertRaisesRegex(InvalidInvitation, "見つからないか"):
            query.preview(self.issued.token, None, NOW)
