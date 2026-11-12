"""メンバーと招待の画面・招待の受諾・新規登録の `next`（W4 T4。#80）。

- メンバーの画面（`/w/<key>/members/`。持ち主だけ）: 持ち主・編集者・チーム担当の一覧。持ち主の行には役割の変更と除名を
  出さない。招待の発行（リンクは発行の直後の1回だけ）・取消、役割の変更（全体を編集 ⇔ 担当チーム）・除名。
  クエリ数はメンバー・招待の数によらない。
- 招待の受諾（`/invitations/<token>/`。ワークスペースの外）: 未ログインは名前と役割とログイン・新規登録の導線
  （`next` で戻る）。受諾したらダッシュボードへ。既にメンバーなら受諾のボタンを出さない。期限切れ・使用済み・無い
  トークンは同じ 404。
- 新規登録（`SignUpView`）: 安全な `next` だけをログインの URL に運ぶ（外部の URL は捨てる）。自動ではログインしない。

門の役割ごとの表は `test_workspace_permissions.py`、ほかのワークスペースの id は `test_workspace_isolation.py` の
`URL_TABLE` と `test_cross_workspace_ids.py`。
"""

import re
from datetime import timedelta
from urllib.parse import urlencode

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from myapp.domain.workspace import InvitedRole, Visibility, invitation_digest
from myapp.infrastructure import orm_models

from ..helpers import build_workspace_member_service, make_editor, make_league, make_workspace, ws_url

LINK = re.compile(r'value="http://testserver/invitations/([A-Za-z0-9_-]+)/"')
PASSWORD = "Sharing-Pass-123"


def _managers(team: orm_models.Team) -> set[int]:
    return set(team.managers.values_list("id", flat=True))


class MembersCase(TestCase):
    """非公開のワークスペースに、チーム2つ・編集者・チーム担当を置く。持ち主でログインしておく。"""

    def setUp(self):
        self.owner = User.objects.create_user(username="owner", password=PASSWORD)
        self.ws = make_workspace(self.owner, "共有のリーグ", visibility=Visibility.PRIVATE)
        league = make_league("リーグ", workspace=self.ws)
        self.home = orm_models.Team.objects.create(league=league, name="ホームズ")
        self.away = orm_models.Team.objects.create(league=league, name="ビジターズ")
        self.editor = make_editor(User.objects.create_user(username="editor", password=PASSWORD), self.ws)
        self.manager = User.objects.create_user(username="manager", password=PASSWORD)
        self.home.managers.add(self.manager)
        self.client.force_login(self.owner)

    def url(self, name, *args):
        return ws_url(name, *args, workspace=self.ws)

    def issue(self, role=InvitedRole.EDIT_TEAMS, teams=None) -> tuple[int, str]:
        team_ids = [self.home.id] if teams is None else teams
        issued = build_workspace_member_service().issue_invitation(self.ws.id, role=role, team_ids=team_ids)
        return issued.invitation_id, issued.token


class MembersPageTest(MembersCase):
    def test_the_page_lists_the_members_without_actions_on_the_owner(self):
        response = self.client.get(self.url("workspace_members"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [(member.username, member.role.label) for member, _ in response.context["members"]],
            [("owner", "持ち主"), ("editor", "編集者"), ("manager", "チーム担当")],
        )
        content = response.content.decode()
        self.assertIn("担当: ホームズ", content)
        self.assertNotIn(self.url("workspace_member_edit", self.owner.id), content, "持ち主の行に役割の変更")
        self.assertNotIn(self.url("workspace_member_remove", self.owner.id), content, "持ち主の行に除名")
        for user in (self.editor, self.manager):
            self.assertIn(self.url("workspace_member_edit", user.id), content)
            self.assertIn(self.url("workspace_member_remove", user.id), content)

    def test_the_role_forms_start_from_the_current_role(self):
        members = dict(self.client.get(self.url("workspace_members")).context["members"])
        forms = {member.username: form for member, form in members.items() if form is not None}

        self.assertEqual(forms["editor"].initial, {"role": InvitedRole.EDIT_ALL.value, "teams": []})
        self.assertEqual(forms["manager"].initial, {"role": InvitedRole.EDIT_TEAMS.value, "teams": [self.home.id]})
        self.assertEqual(
            [value for value, _ in forms["editor"].fields["role"].choices],
            [role.value for role in InvitedRole],
            "役割の選択肢の出典は InvitedRole",
        )
        self.assertEqual(
            [value for value, _ in forms["editor"].fields["teams"].choices],
            [self.away.id, self.home.id],
            "担当チームの選択肢はこのワークスペースのチーム（チームの表示順。同じ順なら名前順）",
        )

    def test_the_tab_and_the_header_link_are_for_the_owner(self):
        members_link = f'href="{self.url("workspace_members")}"'
        self.assertIn(members_link, self.client.get(self.url("workspace_settings")).content.decode(), "設定のタブ")
        for user, shown in ((self.owner, True), (self.editor, False), (self.manager, False)):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                content = self.client.get(self.url("dashboard")).content.decode()
                self.assertEqual(members_link in content, shown)

    def test_others_get_403_and_anonymous_goes_to_the_login(self):
        outsider = User.objects.create_user(username="outsider", password=PASSWORD)
        for user, status in ((self.editor, 403), (self.manager, 403), (outsider, 404)):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                self.assertEqual(self.client.get(self.url("workspace_members")).status_code, status)
        self.client.logout()
        url = self.url("workspace_members")
        self.assertRedirects(self.client.get(url), f"{reverse('login')}?next={url}", fetch_redirect_response=False)

    def test_the_queries_do_not_depend_on_the_number_of_members_and_invitations(self):
        url = self.url("workspace_members")
        self.client.get(url)  # セッションを温める
        with CaptureQueriesContext(connection) as one:
            self.client.get(url)
        for number in range(3):
            make_editor(User.objects.create_user(username=f"more-editor{number}"), self.ws)
            more = User.objects.create_user(username=f"more-manager{number}")
            self.home.managers.add(more)
            self.away.managers.add(more)
            self.issue()
            self.issue(InvitedRole.EDIT_ALL, [])
        with self.assertNumQueries(len(one.captured_queries)):
            self.assertEqual(self.client.get(url).status_code, 200)

    def test_only_get_is_allowed(self):
        self.assertEqual(self.client.post(self.url("workspace_members")).status_code, 405)
        for name in ("workspace_invitation_create",):
            self.assertEqual(self.client.get(self.url(name)).status_code, 405)


class InvitationIssueTest(MembersCase):
    def test_the_link_is_shown_once_right_after_issuing(self):
        response = self.client.post(
            self.url("workspace_invitation_create"),
            {"role": InvitedRole.EDIT_TEAMS.value, "teams": [self.home.id, self.away.id]},
        )

        self.assertRedirects(response, self.url("workspace_members"), fetch_redirect_response=False)
        page = self.client.get(response["Location"])
        (token,) = LINK.findall(page.content.decode())
        self.assertContains(
            page, "data-invitation-link", count=1, msg_prefix="リンクは写せる欄で出す（目印の綴りがずれていない）"
        )
        invitation = orm_models.WorkspaceInvitation.objects.get(workspace=self.ws)
        self.assertEqual(invitation.token_digest, invitation_digest(token), "見せたリンクが保存した招待のもの")
        self.assertEqual(set(invitation.teams.values_list("id", flat=True)), {self.home.id, self.away.id})
        self.assertEqual(len(page.context["invitations"]), 1)

        again = self.client.get(self.url("workspace_members"))
        self.assertEqual(LINK.findall(again.content.decode()), [], "リンクは発行の直後の1回だけ")
        self.assertNotIn(token, again.content.decode())

    def test_edit_all_ignores_checked_teams(self):
        self.client.post(
            self.url("workspace_invitation_create"), {"role": InvitedRole.EDIT_ALL.value, "teams": [self.home.id]}
        )

        invitation = orm_models.WorkspaceInvitation.objects.get(workspace=self.ws)
        self.assertEqual((invitation.role, invitation.teams.count()), (InvitedRole.EDIT_ALL.value, 0))

    def test_input_errors_redraw_the_page_and_issue_nothing(self):
        other = make_workspace(name="よそ")
        other_team = orm_models.Team.objects.create(league=make_league("よそのリーグ", workspace=other), name="よそ")
        bodies = {
            "担当チームが無い": {"role": InvitedRole.EDIT_TEAMS.value},
            "よそのチーム": {"role": InvitedRole.EDIT_TEAMS.value, "teams": [other_team.id]},
            "役割が無い": {"teams": [self.home.id]},
            "役割が読めない": {"role": "owner", "teams": [self.home.id]},
        }
        for label, body in bodies.items():
            with self.subTest(label):
                response = self.client.post(self.url("workspace_invitation_create"), body)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context["invitation_form"].errors)
                self.assertFalse(orm_models.WorkspaceInvitation.objects.exists())
        response = self.client.post(self.url("workspace_invitation_create"), bodies["担当チームが無い"])
        self.assertIn("担当チームを1つ以上選んでください", str(response.context["invitation_form"].errors))

    def test_revoking(self):
        invitation_id, _ = self.issue()
        url = self.url("workspace_invitation_revoke", invitation_id)

        self.assertRedirects(self.client.post(url), self.url("workspace_members"), fetch_redirect_response=False)
        self.assertFalse(orm_models.WorkspaceInvitation.objects.exists())
        self.assertEqual(self.client.post(url).status_code, 404, "取り消した招待は無い招待")
        self.assertEqual(self.client.get(url).status_code, 405)

    def test_expired_invitations_are_not_listed(self):
        invitation_id, _ = self.issue()
        orm_models.WorkspaceInvitation.objects.filter(id=invitation_id).update(expires_at=timezone.now())

        self.assertEqual(self.client.get(self.url("workspace_members")).context["invitations"], [])


class MemberRoleTest(MembersCase):
    def test_an_editor_becomes_a_team_manager_and_back(self):
        url = self.url("workspace_member_edit", self.editor.id)
        response = self.client.post(url, {"role": InvitedRole.EDIT_TEAMS.value, "teams": [self.home.id, self.away.id]})

        self.assertRedirects(response, self.url("workspace_members"), fetch_redirect_response=False)
        self.assertFalse(orm_models.WorkspaceEditor.objects.filter(workspace=self.ws, user=self.editor).exists())
        self.assertIn(self.editor.id, _managers(self.home) & _managers(self.away))

        self.client.post(url, {"role": InvitedRole.EDIT_ALL.value, "teams": [self.home.id]})
        self.assertTrue(orm_models.WorkspaceEditor.objects.filter(workspace=self.ws, user=self.editor).exists())
        self.assertNotIn(self.editor.id, _managers(self.home) | _managers(self.away))

    def test_reassigning_the_teams_of_a_manager(self):
        self.client.post(
            self.url("workspace_member_edit", self.manager.id),
            {"role": InvitedRole.EDIT_TEAMS.value, "teams": [self.away.id]},
        )

        self.assertNotIn(self.manager.id, _managers(self.home))
        self.assertIn(self.manager.id, _managers(self.away))

    def test_a_manager_without_teams_is_a_form_error(self):
        response = self.client.post(
            self.url("workspace_member_edit", self.manager.id), {"role": InvitedRole.EDIT_TEAMS.value}
        )

        self.assertEqual(response.status_code, 200)
        forms = {member.user_id: form for member, form in response.context["members"]}
        self.assertTrue(forms[self.manager.id].errors["teams"])
        self.assertContains(response, "manager さんの役割を変えられませんでした。")
        self.assertIn(self.manager.id, _managers(self.home))

    def test_the_owner_and_non_members_are_404(self):
        outsider = User.objects.create_user(username="outsider")
        for user in (self.owner, outsider):
            with self.subTest(user=user.username):
                for name in ("workspace_member_edit", "workspace_member_remove"):
                    response = self.client.post(self.url(name, user.id), {"role": InvitedRole.EDIT_ALL.value})
                    self.assertEqual(response.status_code, 404)
        self.assertFalse(orm_models.WorkspaceEditor.objects.filter(user__in=[self.owner, outsider]).exists())

    def test_removing_members(self):
        for user in (self.editor, self.manager):
            with self.subTest(user=user.username):
                response = self.client.post(self.url("workspace_member_remove", user.id))
                self.assertRedirects(response, self.url("workspace_members"), fetch_redirect_response=False)
        self.assertFalse(orm_models.WorkspaceEditor.objects.filter(workspace=self.ws).exists())
        self.assertEqual(_managers(self.home), set())
        self.assertEqual(self.client.post(self.url("workspace_member_remove", self.editor.id)).status_code, 404)

    def test_removing_keeps_the_assignments_in_other_workspaces(self):
        other = make_workspace(name="よそ")
        other_team = orm_models.Team.objects.create(league=make_league("よそのリーグ", workspace=other), name="よそ")
        other_team.managers.add(self.manager)

        self.client.post(self.url("workspace_member_remove", self.manager.id))

        self.assertEqual(_managers(other_team), {self.manager.id})


class InvitationAcceptTest(MembersCase):
    def setUp(self):
        super().setUp()
        self.invitation_id, self.token = self.issue()
        self.accept_url = reverse("invitation_accept", args=(self.token,))
        self.guest = User.objects.create_user(username="guest", password=PASSWORD)

    def test_anonymous_sees_the_workspace_and_the_ways_to_log_in(self):
        self.client.logout()
        response = self.client.get(self.accept_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "共有のリーグ")
        self.assertContains(response, "チーム担当")
        self.assertContains(response, "ホームズ")
        back = urlencode({"next": self.accept_url})
        self.assertContains(response, f'href="{reverse("login")}?{back}"')
        self.assertContains(response, f'href="{reverse("signup")}?{back}"')
        self.assertNotContains(response, "招待を受け取る</button>")

    def test_anonymous_posts_go_to_the_login(self):
        self.client.logout()
        response = self.client.post(self.accept_url)

        self.assertRedirects(
            response, f"{reverse('login')}?{urlencode({'next': self.accept_url})}", fetch_redirect_response=False
        )
        self.assertTrue(orm_models.WorkspaceInvitation.objects.filter(id=self.invitation_id).exists())

    def test_logging_in_comes_back_and_accepting_opens_the_dashboard(self):
        self.client.logout()
        login = self.client.post(
            f"{reverse('login')}?{urlencode({'next': self.accept_url})}",
            {"username": "guest", "password": PASSWORD, "next": self.accept_url},
        )
        self.assertRedirects(login, self.accept_url, fetch_redirect_response=False)
        self.assertContains(self.client.get(self.accept_url), "招待を受け取る</button>")

        response = self.client.post(self.accept_url)

        self.assertRedirects(response, self.url("dashboard"), fetch_redirect_response=False)
        self.assertContains(
            self.client.get(response["Location"]), "ワークスペース「共有のリーグ」にチーム担当として参加しました。"
        )
        self.assertEqual(_managers(self.home), {self.manager.id, self.guest.id})
        self.assertFalse(orm_models.WorkspaceInvitation.objects.filter(id=self.invitation_id).exists())

    def test_the_second_acceptance_is_404(self):
        self.client.force_login(self.guest)
        self.client.post(self.accept_url)
        other = User.objects.create_user(username="latecomer")
        self.client.force_login(other)

        self.assertEqual(self.client.get(self.accept_url).status_code, 404)
        self.assertEqual(self.client.post(self.accept_url).status_code, 404)
        self.assertNotIn(other.id, _managers(self.home))

    def test_members_get_no_accept_button(self):
        for user in (self.owner, self.editor, self.manager):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                page = self.client.get(self.accept_url)
                self.assertContains(page, "すでにこのワークスペースのメンバーです。")
                self.assertNotContains(page, "招待を受け取る</button>")
                self.assertContains(page, f'href="{self.url("dashboard")}"')
                response = self.client.post(self.accept_url)
                self.assertRedirects(response, self.url("dashboard"), fetch_redirect_response=False)
        self.assertTrue(orm_models.WorkspaceInvitation.objects.filter(id=self.invitation_id).exists(), "使わずに残る")
        self.assertTrue(orm_models.WorkspaceEditor.objects.filter(workspace=self.ws, user=self.editor).exists())

    def test_expired_used_and_unknown_tokens_are_the_same_404(self):
        self.client.force_login(self.guest)
        expired_id, expired = self.issue()
        orm_models.WorkspaceInvitation.objects.filter(id=expired_id).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        for label, token in (("期限切れ", expired), ("無い", "no-such-token")):
            with self.subTest(label):
                url = reverse("invitation_accept", args=(token,))
                self.assertEqual(self.client.get(url).status_code, 404)
                self.assertEqual(self.client.post(url).status_code, 404)
        self.assertNotIn(self.guest.id, _managers(self.home))

    def test_an_edit_all_invitation_makes_an_editor(self):
        _, token = self.issue(InvitedRole.EDIT_ALL, [])
        self.client.force_login(self.guest)

        self.client.post(reverse("invitation_accept", args=(token,)))

        self.assertTrue(orm_models.WorkspaceEditor.objects.filter(workspace=self.ws, user=self.guest).exists())


class SignUpNextTest(TestCase):
    """新規登録の `next`: 安全な URL だけをログインの URL に運ぶ。登録の後は自動でログインしない。"""

    def body(self, username, **extra):
        return {"username": username, "password1": PASSWORD, "password2": PASSWORD, **extra}

    def test_a_safe_next_is_carried_to_the_login(self):
        target = "/invitations/abc/"
        page = self.client.get(f"{reverse('signup')}?{urlencode({'next': target})}")
        self.assertContains(page, f'<input type="hidden" name="next" value="{target}">', html=True)
        self.assertContains(page, f'href="{reverse("login")}?next=/invitations/abc/"')

        response = self.client.post(reverse("signup"), self.body("newcomer", next=target))

        self.assertRedirects(
            response, f"{reverse('login')}?{urlencode({'next': target})}", fetch_redirect_response=False
        )
        self.assertNotIn("_auth_user_id", self.client.session, "登録の後は自動でログインしない")
        self.assertTrue(User.objects.filter(username="newcomer").exists())

    def test_the_login_page_carries_next_to_the_signup(self):
        target = "/invitations/abc/"
        page = self.client.get(f"{reverse('login')}?{urlencode({'next': target})}")

        self.assertContains(page, f'href="{reverse("signup")}?next=/invitations/abc/"')

    def test_external_next_is_dropped(self):
        for number, target in enumerate(("https://evil.example/", "//evil.example/path", "javascript:alert(1)")):
            with self.subTest(target=target):
                page = self.client.get(f"{reverse('signup')}?{urlencode({'next': target})}")
                self.assertNotContains(page, "evil.example")
                self.assertNotContains(page, 'name="next"')
                username = f"user{number}"
                response = self.client.post(reverse("signup"), self.body(username, next=target))
                self.assertRedirects(response, reverse("login"), fetch_redirect_response=False)

    def test_no_next_goes_to_the_login(self):
        response = self.client.post(reverse("signup"), self.body("plain"))

        self.assertRedirects(response, reverse("login"), fetch_redirect_response=False)
