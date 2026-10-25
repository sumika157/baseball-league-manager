"""ワークスペースの業務ルール（DB 不要・Django 非依存）。"""

import random
import unittest

from myapp.domain.exceptions import InvalidWorkspace, MemberNotFound
from myapp.domain.workspace import (
    KEY_ALPHABET,
    KEY_LENGTH,
    MAX_NAME_LENGTH,
    InvitedRole,
    Visibility,
    Workspace,
    WorkspaceRole,
    generate_workspace_key,
    members_from_rows,
    validate_workspace_key,
)

KEY = "abcde12345"


class VisibilityTest(unittest.TestCase):
    def test_choices_are_public_and_private(self):
        self.assertEqual(Visibility.choices(), [("public", "公開"), ("private", "非公開")])

    def test_every_member_has_a_label(self):
        for member in Visibility:
            self.assertTrue(member.label)


class WorkspaceTest(unittest.TestCase):
    def test_name_is_stripped(self):
        workspace = Workspace(name=" 私のリーグ ", key=KEY, owner_id=1, visibility=Visibility.PUBLIC)

        self.assertEqual(workspace.name, "私のリーグ")
        self.assertEqual(workspace.visibility, Visibility.PUBLIC)
        self.assertIsNone(workspace.id)

    def test_empty_or_blank_name_is_rejected(self):
        for name in ("", "   ", None):
            with self.subTest(name=name), self.assertRaises(InvalidWorkspace):
                Workspace(name=name, key=KEY, owner_id=1, visibility=Visibility.PUBLIC)  # type: ignore[arg-type]

    def test_too_long_name_is_rejected(self):
        Workspace(name="あ" * MAX_NAME_LENGTH, key=KEY, owner_id=1, visibility=Visibility.PUBLIC)
        with self.assertRaises(InvalidWorkspace):
            Workspace(name="あ" * (MAX_NAME_LENGTH + 1), key=KEY, owner_id=1, visibility=Visibility.PUBLIC)

    def test_owner_must_be_a_positive_int(self):
        for owner_id in (0, -1, True, None):
            with self.subTest(owner_id=owner_id), self.assertRaises(InvalidWorkspace):
                Workspace(name="a", key=KEY, owner_id=owner_id, visibility=Visibility.PUBLIC)  # type: ignore[arg-type]

    def test_visibility_must_be_a_visibility(self):
        with self.assertRaises(InvalidWorkspace):
            Workspace(name="a", key=KEY, owner_id=1, visibility="public")  # type: ignore[arg-type]

    def test_bad_key_is_rejected(self):
        with self.assertRaises(InvalidWorkspace):
            Workspace(name="a", key="short", owner_id=1, visibility=Visibility.PUBLIC)


class WorkspaceKeyTest(unittest.TestCase):
    def test_system_random_is_accepted(self):
        self.assertEqual(len(generate_workspace_key(random.SystemRandom())), KEY_LENGTH)

    def test_generated_key_has_the_right_shape(self):
        key = generate_workspace_key(random.Random(1))

        self.assertEqual(len(key), KEY_LENGTH)
        self.assertTrue(all(c in KEY_ALPHABET for c in key))
        self.assertEqual(validate_workspace_key(key), key)

    def test_same_random_source_gives_the_same_key(self):
        self.assertEqual(generate_workspace_key(random.Random(7)), generate_workspace_key(random.Random(7)))

    def test_different_seeds_give_different_keys(self):
        self.assertNotEqual(generate_workspace_key(random.Random(1)), generate_workspace_key(random.Random(2)))

    def test_validation_rejects_wrong_length_and_characters(self):
        for key in ("", "abcde1234", "abcde123456", "ABCDE12345", "abcde-2345", "あいうえおかきくけこ"):
            with self.subTest(key=key), self.assertRaises(InvalidWorkspace):
                validate_workspace_key(key)


OWNER = 1
EDITOR = 2
MANAGER = 3
OUTSIDER = 4
TEAM_A = 10
TEAM_B = 20


def make(**members) -> Workspace:
    return Workspace(name="a", key=KEY, owner_id=OWNER, visibility=Visibility.PRIVATE, id=5, **members)


class SettingsTest(unittest.TestCase):
    def test_rename_strips_and_checks_the_name(self):
        workspace = make()
        workspace.rename(" 新しい名前 ")
        self.assertEqual(workspace.name, "新しい名前")
        for name in ("", "  ", "あ" * (MAX_NAME_LENGTH + 1)):
            with self.subTest(name=name), self.assertRaises(InvalidWorkspace):
                workspace.rename(name)
        self.assertEqual(workspace.name, "新しい名前", "失敗した改名で名前が変わった")

    def test_change_visibility(self):
        workspace = make()
        workspace.change_visibility(Visibility.PUBLIC)
        self.assertIs(workspace.visibility, Visibility.PUBLIC)
        with self.assertRaises(InvalidWorkspace):
            workspace.change_visibility("private")  # type: ignore[arg-type]
        self.assertIs(workspace.visibility, Visibility.PUBLIC)


class MemberInvariantTest(unittest.TestCase):
    """1人の役割は1つだけ。持ち主は編集者にも担当にも入らない（設計書 3.3）。"""

    def test_members_are_held_and_roles_are_derived(self):
        workspace = make(editor_ids=frozenset({EDITOR}), team_assignments={MANAGER: frozenset({TEAM_A})})
        self.assertEqual(
            [workspace.role_of(user) for user in (OWNER, EDITOR, MANAGER, OUTSIDER, None)],
            [
                WorkspaceRole.OWNER,
                WorkspaceRole.EDITOR,
                WorkspaceRole.TEAM_MANAGER,
                WorkspaceRole.NONE,
                WorkspaceRole.NONE,
            ],
        )
        self.assertTrue(workspace.is_member(MANAGER))
        self.assertFalse(workspace.is_member(OUTSIDER))

    def test_the_owner_is_neither_an_editor_nor_a_manager(self):
        for members in ({"editor_ids": frozenset({OWNER})}, {"team_assignments": {OWNER: frozenset({TEAM_A})}}):
            with self.subTest(members=members), self.assertRaises(InvalidWorkspace):
                make(**members)

    def test_an_editor_is_not_also_a_manager(self):
        with self.assertRaises(InvalidWorkspace):
            make(editor_ids=frozenset({EDITOR}), team_assignments={EDITOR: frozenset({TEAM_A})})

    def test_a_manager_has_at_least_one_team(self):
        with self.assertRaises(InvalidWorkspace):
            make(team_assignments={MANAGER: frozenset()})

    def test_ids_must_be_positive_ints(self):
        for members in (
            {"editor_ids": frozenset({0})},
            {"editor_ids": frozenset({True})},
            {"team_assignments": {0: frozenset({TEAM_A})}},
            {"team_assignments": {MANAGER: frozenset({-1})}},
        ):
            with self.subTest(members=members), self.assertRaises(InvalidWorkspace):
                make(**members)

    def test_the_members_are_copied(self):
        """渡した集まりを後から書き換えても、集約は変わらない。"""
        assignments = {MANAGER: frozenset({TEAM_A})}
        workspace = make(team_assignments=assignments)
        assignments[OUTSIDER] = frozenset({TEAM_B})
        self.assertEqual(workspace.team_assignments, {MANAGER: frozenset({TEAM_A})})


class MemberOperationsTest(unittest.TestCase):
    def test_make_editor_moves_a_manager_to_the_editors(self):
        workspace = make(team_assignments={MANAGER: frozenset({TEAM_A})})
        workspace.make_editor(MANAGER)
        self.assertEqual(workspace.editor_ids, frozenset({MANAGER}))
        self.assertEqual(workspace.team_assignments, {})

    def test_assign_teams_replaces_the_teams_and_drops_the_editor_role(self):
        workspace = make(editor_ids=frozenset({EDITOR}), team_assignments={MANAGER: frozenset({TEAM_A})})
        workspace.assign_teams(MANAGER, [TEAM_B])
        workspace.assign_teams(EDITOR, [TEAM_A, TEAM_B])
        self.assertEqual(workspace.editor_ids, frozenset())
        self.assertEqual(
            workspace.team_assignments, {MANAGER: frozenset({TEAM_B}), EDITOR: frozenset({TEAM_A, TEAM_B})}
        )

    def test_assign_teams_needs_a_team(self):
        workspace = make(team_assignments={MANAGER: frozenset({TEAM_A})})
        with self.assertRaises(InvalidWorkspace):
            workspace.assign_teams(MANAGER, [])
        self.assertEqual(workspace.team_assignments, {MANAGER: frozenset({TEAM_A})})

    def test_the_owner_cannot_be_given_a_role(self):
        workspace = make()
        for grant in (lambda: workspace.make_editor(OWNER), lambda: workspace.assign_teams(OWNER, [TEAM_A])):
            with self.subTest(grant=grant), self.assertRaises(InvalidWorkspace):
                grant()
        self.assertEqual((workspace.editor_ids, workspace.team_assignments), (frozenset(), {}))

    def test_change_role_switches_between_editing_all_and_teams(self):
        workspace = make(editor_ids=frozenset({EDITOR}), team_assignments={MANAGER: frozenset({TEAM_A})})
        workspace.change_role(EDITOR, InvitedRole.EDIT_TEAMS, [TEAM_B])
        workspace.change_role(MANAGER, InvitedRole.EDIT_ALL)
        self.assertEqual(workspace.editor_ids, frozenset({MANAGER}))
        self.assertEqual(workspace.team_assignments, {EDITOR: frozenset({TEAM_B})})

    def test_change_role_reassigns_the_teams(self):
        workspace = make(team_assignments={MANAGER: frozenset({TEAM_A})})
        workspace.change_role(MANAGER, InvitedRole.EDIT_TEAMS, [TEAM_B])
        self.assertEqual(workspace.team_assignments, {MANAGER: frozenset({TEAM_B})})

    def test_change_role_rejects_teams_for_editing_all(self):
        workspace = make(team_assignments={MANAGER: frozenset({TEAM_A})})
        with self.assertRaises(InvalidWorkspace):
            workspace.change_role(MANAGER, InvitedRole.EDIT_ALL, [TEAM_B])
        self.assertEqual(workspace.team_assignments, {MANAGER: frozenset({TEAM_A})})

    def test_change_role_needs_a_member(self):
        """メンバーでない人を役割の変更で入れられない（入れるのは招待の受諾だけ）。持ち主の役割は変えられない。"""
        workspace = make()
        with self.assertRaises(MemberNotFound):
            workspace.change_role(OUTSIDER, InvitedRole.EDIT_ALL)
        with self.assertRaises(InvalidWorkspace):
            workspace.change_role(OWNER, InvitedRole.EDIT_ALL)
        self.assertEqual((workspace.editor_ids, workspace.team_assignments), (frozenset(), {}))

    def test_remove_member_drops_the_editor_and_every_assignment(self):
        workspace = make(editor_ids=frozenset({EDITOR}), team_assignments={MANAGER: frozenset({TEAM_A, TEAM_B})})
        workspace.remove_member(EDITOR)
        workspace.remove_member(MANAGER)
        self.assertEqual((workspace.editor_ids, workspace.team_assignments), (frozenset(), {}))

    def test_remove_member_rejects_the_owner_and_outsiders(self):
        workspace = make()
        with self.assertRaises(InvalidWorkspace):
            workspace.remove_member(OWNER)
        with self.assertRaises(MemberNotFound):
            workspace.remove_member(OUTSIDER)


class AssignmentsWithinTest(unittest.TestCase):
    """担当チームはこのワークスペースの実データのチーム（リポジトリが範囲のチームの id を渡す）。"""

    def test_assignments_within_the_teams_pass(self):
        make(team_assignments={MANAGER: frozenset({TEAM_A})}).ensure_assignments_within({TEAM_A, TEAM_B})
        make().ensure_assignments_within(())

    def test_an_assignment_outside_the_teams_is_rejected(self):
        workspace = make(team_assignments={MANAGER: frozenset({TEAM_A, TEAM_B})})
        with self.assertRaises(InvalidWorkspace):
            workspace.ensure_assignments_within({TEAM_A})


class MembersFromRowsTest(unittest.TestCase):
    """管理画面で付けた重なった行は、読むときに落とさず上の役割だけを残す。"""

    def test_rows_become_members(self):
        editors, assignments = members_from_rows(
            OWNER, [EDITOR], [(TEAM_A, MANAGER), (TEAM_B, MANAGER), (TEAM_A, OUTSIDER)]
        )
        self.assertEqual(editors, frozenset({EDITOR}))
        self.assertEqual(assignments, {MANAGER: frozenset({TEAM_A, TEAM_B}), OUTSIDER: frozenset({TEAM_A})})

    def test_overlapping_rows_keep_only_the_higher_role(self):
        editors, assignments = members_from_rows(OWNER, [OWNER, EDITOR], [(TEAM_A, OWNER), (TEAM_A, EDITOR)])
        self.assertEqual(editors, frozenset({EDITOR}))
        self.assertEqual(assignments, {})
        # 組み立てた集約は不変条件を満たす（読むときに落ちない）
        make(editor_ids=editors, team_assignments=assignments)
