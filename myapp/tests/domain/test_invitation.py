"""招待（`Invitation`・`InvitedRole`）と受諾（`Workspace.accept`）の業務ルール（DB 不要・Django 非依存）。

招待は期限内に1回だけ使える（受諾したら呼ぶ側が消す）。`EDIT_TEAMS` は担当チームを1つ以上持つ。
"""

import hashlib
import unittest
from datetime import UTC, datetime, timedelta

from myapp.domain.exceptions import InvalidInvitation, InvalidWorkspace
from myapp.domain.workspace import (
    INVITATION_TTL,
    INVITATION_UNAVAILABLE,
    Invitation,
    InvitedRole,
    Visibility,
    Workspace,
    WorkspaceRole,
    invitation_digest,
)

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
WORKSPACE_ID = 5
OWNER = 1
EDITOR = 2
NEWCOMER = 9
TEAM = 10


def issue(role=InvitedRole.EDIT_ALL, team_ids=(), *, workspace_id=WORKSPACE_ID, now=NOW) -> Invitation:
    return Invitation.issue(workspace_id=workspace_id, token="token-1", role=role, team_ids=team_ids, now=now)


def workspace() -> Workspace:
    return Workspace(
        name="a",
        key="abcde12345",
        owner_id=OWNER,
        visibility=Visibility.PRIVATE,
        id=WORKSPACE_ID,
        editor_ids=frozenset({EDITOR}),
    )


class InvitedRoleTest(unittest.TestCase):
    def test_choices_are_edit_all_and_edit_teams(self):
        self.assertEqual(InvitedRole.choices(), [("edit_all", "全体を編集"), ("edit_teams", "担当チームを編集")])

    def test_each_role_grants_a_workspace_role(self):
        self.assertIs(InvitedRole.EDIT_ALL.granted_role, WorkspaceRole.EDITOR)
        self.assertIs(InvitedRole.EDIT_TEAMS.granted_role, WorkspaceRole.TEAM_MANAGER)


class DigestTest(unittest.TestCase):
    def test_the_digest_is_the_sha256_of_the_token(self):
        self.assertEqual(invitation_digest("abc"), hashlib.sha256(b"abc").hexdigest())
        self.assertNotEqual(invitation_digest("abc"), invitation_digest("abd"))

    def test_an_empty_token_is_an_unavailable_invitation(self):
        for token in ("", None):
            with self.subTest(token=token), self.assertRaisesRegex(InvalidInvitation, INVITATION_UNAVAILABLE):
                invitation_digest(token)  # type: ignore[arg-type]


class IssueTest(unittest.TestCase):
    def test_issue_keeps_only_the_digest_and_expires_in_seven_days(self):
        invitation = issue(InvitedRole.EDIT_TEAMS, [TEAM])
        self.assertEqual(invitation.token_digest, invitation_digest("token-1"))
        self.assertNotIn("token-1", repr(invitation))
        self.assertEqual(invitation.expires_at, NOW + timedelta(days=7))
        self.assertEqual(INVITATION_TTL, timedelta(days=7))
        self.assertEqual(invitation.team_ids, frozenset({TEAM}))
        self.assertIsNone(invitation.id)

    def test_edit_teams_needs_a_team(self):
        with self.assertRaises(InvalidInvitation):
            issue(InvitedRole.EDIT_TEAMS, [])

    def test_edit_all_takes_no_team(self):
        with self.assertRaises(InvalidInvitation):
            issue(InvitedRole.EDIT_ALL, [TEAM])

    def test_bad_values_are_rejected(self):
        good = issue()
        for changes in (
            {"workspace_id": 0},
            {"token_digest": "x" * 64},
            {"token_digest": "a" * 63},
            {"token_digest": "A" * 64},
            {"role": "edit_all"},
            {"team_ids": [TEAM]},
            {"expires_at": "2026-11-01"},
        ):
            values = {**good.__dict__, **changes}
            with self.subTest(changes=changes), self.assertRaises(InvalidInvitation):
                Invitation(**values)

    def test_teams_must_be_within_the_given_teams(self):
        invitation = issue(InvitedRole.EDIT_TEAMS, [TEAM])
        invitation.ensure_teams_within({TEAM, TEAM + 1})
        issue().ensure_teams_within(())
        with self.assertRaises(InvalidInvitation):
            invitation.ensure_teams_within({TEAM + 1})

    def test_the_deadline_itself_is_expired(self):
        invitation = issue()
        self.assertFalse(invitation.is_expired(NOW + INVITATION_TTL - timedelta(seconds=1)))
        self.assertTrue(invitation.is_expired(NOW + INVITATION_TTL))


class AcceptTest(unittest.TestCase):
    def test_accepting_edit_all_makes_an_editor(self):
        target = workspace()
        target.accept(issue(), NEWCOMER, now=NOW)
        self.assertIs(target.role_of(NEWCOMER), WorkspaceRole.EDITOR)

    def test_accepting_edit_teams_makes_a_manager_of_the_teams(self):
        target = workspace()
        target.accept(issue(InvitedRole.EDIT_TEAMS, [TEAM]), NEWCOMER, now=NOW)
        self.assertEqual(target.team_assignments, {NEWCOMER: frozenset({TEAM})})

    def test_an_expired_invitation_cannot_be_used(self):
        target = workspace()
        with self.assertRaisesRegex(InvalidInvitation, INVITATION_UNAVAILABLE):
            target.accept(issue(), NEWCOMER, now=NOW + INVITATION_TTL)
        self.assertFalse(target.is_member(NEWCOMER))

    def test_an_invitation_of_another_workspace_cannot_be_used(self):
        target = workspace()
        with self.assertRaisesRegex(InvalidInvitation, INVITATION_UNAVAILABLE):
            target.accept(issue(workspace_id=WORKSPACE_ID + 1), NEWCOMER, now=NOW)
        self.assertFalse(target.is_member(NEWCOMER))

    def test_members_cannot_accept(self):
        """持ち主・既にメンバーの人は受諾できない（役割は変わらない。役割を変えるのは持ち主）。"""
        target = workspace()
        for user_id in (OWNER, EDITOR):
            with self.subTest(user_id=user_id), self.assertRaises(InvalidInvitation):
                target.accept(issue(InvitedRole.EDIT_TEAMS, [TEAM]), user_id, now=NOW)
        self.assertEqual((target.editor_ids, target.team_assignments), (frozenset({EDITOR}), {}))

    def test_a_bad_user_cannot_accept(self):
        with self.assertRaises(InvalidInvitation):
            workspace().accept(issue(), 0, now=NOW)

    def test_the_owner_role_is_not_granted_by_an_invitation(self):
        """招待で入るのは編集者かチーム担当だけ（持ち主は渡せない）。"""
        self.assertEqual(
            {role.granted_role for role in InvitedRole}, {WorkspaceRole.EDITOR, WorkspaceRole.TEAM_MANAGER}
        )
        with self.assertRaises(InvalidWorkspace):
            workspace().make_editor(OWNER)
