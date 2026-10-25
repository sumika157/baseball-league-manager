"""ワークスペースの作成と設定の画面・ヘッダーの切り替え（W4 T3。#80）。

- 作成（`/` のフォーム → `/workspaces/new/`）: 公開範囲の既定は非公開。作ったら新しいワークスペースのダッシュボード
  （空の案内）へ。上限に達したらフォームの代わりに案内。上限を超える POST は 200 でフォームのエラー（何も作らない）。
- 設定（`/w/<key>/settings/`）: 持ち主だけが名前と公開範囲を変えられる。ほかの人は GET も POST も 403。
- ヘッダー: ログインしていれば名前のところが切り替えのドロップダウン（自分のワークスペースと `/`）。
  「ワークスペースの設定」は持ち主だけ。一覧はワークスペースの数によらず1本で読み、未ログインでは読まない。
"""

from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import connection
from django.test import RequestFactory, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from myapp.domain.workspace import Visibility
from myapp.infrastructure import orm_models
from myapp.presentation import context_processors, views

from ..helpers import build_workspace_access_query, make_editor, make_league, make_workspace, ws_url

SWITCH_HEADER = "ワークスペースを切り替える"


class WorkspaceCreateScreenTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="creator", password="x")
        self.client.force_login(self.user)

    def owned(self):
        return orm_models.Workspace.objects.filter(owner=self.user)

    def test_the_form_is_on_the_top_and_private_by_default(self):
        response = self.client.get(reverse("home"))

        self.assertContains(response, f'action="{reverse("workspace_create")}"')
        private = Visibility.PRIVATE
        self.assertContains(response, f'<option value="{private.value}" selected>{private.label}</option>', html=True)
        self.assertEqual(
            [value for value, _ in response.context["create_form"].fields["visibility"].choices],
            [option.value for option in Visibility],
            "選択肢の出典は Visibility",
        )

    def test_creating_leads_to_the_new_empty_dashboard(self):
        response = self.client.post(
            reverse("workspace_create"), {"name": "  わたしのリーグ  ", "visibility": Visibility.PRIVATE.value}
        )

        created = self.owned().get()
        self.assertEqual((created.name, created.visibility), ("わたしのリーグ", Visibility.PRIVATE.value))
        self.assertRedirects(response, ws_url("dashboard", workspace=created), fetch_redirect_response=False)
        dashboard = self.client.get(response["Location"])
        self.assertContains(dashboard, "ワークスペース「わたしのリーグ」を作りました。")
        self.assertContains(dashboard, "まだチームが登録されていません。")

    def test_a_public_workspace_can_be_made(self):
        self.client.post(reverse("workspace_create"), {"name": "公開のリーグ", "visibility": Visibility.PUBLIC.value})

        self.assertEqual(self.owned().get().visibility, Visibility.PUBLIC.value)

    def test_input_errors_redraw_the_top_with_the_errors(self):
        for body in ({"name": "   ", "visibility": "private"}, {"name": "名前", "visibility": "secret"}, {}):
            with self.subTest(body=body):
                response = self.client.post(reverse("workspace_create"), body)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context["create_form"].errors)
                self.assertFalse(self.owned().exists())

    def test_the_limit_replaces_the_form_and_refuses_a_fourth(self):
        for number in range(1, 4):
            make_workspace(self.user, f"{number}つ目")

        top = self.client.get(reverse("home"))
        self.assertIsNone(top.context["create_form"])
        self.assertNotContains(top, f'action="{reverse("workspace_create")}"')
        self.assertContains(top, "ワークスペースは1人3つまで作れます。")

        response = self.client.post(reverse("workspace_create"), {"name": "4つ目", "visibility": "private"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ワークスペースは1人3つまで作れます。")
        self.assertEqual(self.owned().count(), 3)

    @override_settings(WORKSPACE_MAX_OWNED=1)
    def test_the_limit_comes_from_the_settings(self):
        make_workspace(self.user, "1つ目")

        self.assertIsNone(self.client.get(reverse("home")).context["create_form"])
        self.assertContains(self.client.get(reverse("home")), "ワークスペースは1人1つまで作れます。")

    def test_the_form_counts_only_owned_workspaces(self):
        """編集者として入っているワークスペースは、持っている数に数えない。"""
        for number in range(1, 4):
            make_editor(self.user, make_workspace(name=f"よそ{number}"))

        self.assertIsNotNone(self.client.get(reverse("home")).context["create_form"])

    def test_anonymous_posts_go_to_the_login_and_get_is_not_allowed(self):
        self.client.logout()
        response = self.client.post(reverse("workspace_create"), {"name": "侵入", "visibility": "public"})

        self.assertRedirects(response, f"{reverse('login')}?next={reverse('home')}", fetch_redirect_response=False)
        self.assertFalse(orm_models.Workspace.objects.filter(name="侵入").exists())
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("workspace_create")).status_code, 405)

    def test_the_top_reads_the_same_number_of_queries_whatever_the_number_of_workspaces(self):
        make_workspace(self.user, "1つ目")
        self.client.get(reverse("home"))  # セッションを温める
        with self.assertNumQueries(4) as one:  # セッション・利用者・一覧・持っている数
            self.client.get(reverse("home"))
        make_workspace(self.user, "2つ目")
        make_editor(self.user, make_workspace(name="よそ"))
        with self.assertNumQueries(len(one.captured_queries)):
            self.client.get(reverse("home"))


class WorkspaceSettingsScreenTest(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner", password="x")
        self.ws = make_workspace(self.owner, "元の名前", visibility=Visibility.PUBLIC)
        self.url = ws_url("workspace_settings", workspace=self.ws)

    def test_the_owner_changes_the_name_and_the_visibility(self):
        self.client.force_login(self.owner)
        page = self.client.get(self.url)
        self.assertEqual(page.context["form"].initial, {"name": "元の名前", "visibility": Visibility.PUBLIC.value})

        response = self.client.post(self.url, {"name": "新しい名前", "visibility": Visibility.PRIVATE.value})

        self.assertRedirects(response, self.url)
        self.ws.refresh_from_db()
        self.assertEqual((self.ws.name, self.ws.visibility), ("新しい名前", Visibility.PRIVATE.value))
        self.assertContains(self.client.get(self.url), "新しい名前")

    def test_a_blank_name_is_a_form_error(self):
        self.client.force_login(self.owner)
        response = self.client.post(self.url, {"name": " ", "visibility": "public"})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors["name"])
        self.ws.refresh_from_db()
        self.assertEqual(self.ws.name, "元の名前")

    def test_only_the_owner_may_open_or_post(self):
        editor = make_editor(User.objects.create_user(username="editor", password="x"), self.ws)
        manager = User.objects.create_user(username="manager", password="x")
        team = orm_models.Team.objects.create(league=make_league("リーグ", workspace=self.ws), name="球団")
        team.managers.add(manager)
        outsider = User.objects.create_superuser("root", password="x")
        for user in (editor, manager, outsider):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                self.assertEqual(self.client.get(self.url).status_code, 403)
                response = self.client.post(self.url, {"name": "乗っ取り", "visibility": "public"})
                self.assertEqual(response.status_code, 403)
        self.ws.refresh_from_db()
        self.assertEqual(self.ws.name, "元の名前")

    def test_anonymous_goes_to_the_login(self):
        response = self.client.get(self.url)

        self.assertRedirects(response, f"{reverse('login')}?next={self.url}", fetch_redirect_response=False)

    def test_other_methods_are_not_allowed(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.put(self.url).status_code, 405)


class HeaderSwitchTest(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner", password="x")
        self.ws = make_workspace(self.owner, "あ　今のワークスペース")
        self.other = make_workspace(self.owner, "い　もう1つ")
        self.editor = make_editor(User.objects.create_user(username="editor", password="x"), self.ws)
        self.outsider_ws = make_workspace(name="う　入っていない")

    def test_members_can_switch_to_their_workspaces_and_the_top(self):
        for user, expected in ((self.owner, [self.ws, self.other]), (self.editor, [self.ws])):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.client.get(ws_url("dashboard", workspace=self.ws))
                content = response.content.decode()
                self.assertIn(SWITCH_HEADER, content)
                for workspace in expected:
                    self.assertIn(f'href="{ws_url("dashboard", workspace=workspace)}"', content)
                self.assertNotIn(self.outsider_ws.name, content, "入っていないワークスペースは出さない")
                self.assertIn(f'href="{reverse("home")}"', content)

    def test_only_the_owner_sees_the_settings_link(self):
        settings_link = f'href="{ws_url("workspace_settings", workspace=self.ws)}"'
        for user, shown in ((self.owner, True), (self.editor, False)):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.client.get(ws_url("dashboard", workspace=self.ws))
                self.assertEqual(settings_link in response.content.decode(), shown)
        self.client.logout()
        self.assertNotIn(settings_link, self.client.get(ws_url("dashboard", workspace=self.ws)).content.decode())

    def test_anonymous_sees_the_name_without_the_switch(self):
        response = self.client.get(ws_url("dashboard", workspace=self.ws))

        self.assertNotContains(response, SWITCH_HEADER)
        self.assertContains(response, f'href="{ws_url("dashboard", workspace=self.ws)}" class="app-workspace-name"')

    def test_the_switch_reads_once_whatever_the_number_of_workspaces(self):
        """ログインした人のヘッダーの一覧は1回だけ読み、クエリ数はワークスペースの数によらない。未ログインは読まない。"""
        url = ws_url("dashboard", workspace=self.ws)
        for user, reads in ((None, 0), (self.owner, 1)):
            with self.subTest(user=str(user)):
                if user is not None:
                    self.client.force_login(user)
                self.client.get(url)  # セッションを温める
                with (
                    patch.object(views, "build_workspace_list_query", wraps=views.build_workspace_list_query) as spy,
                    CaptureQueriesContext(connection) as one,
                ):
                    self.client.get(url)
                self.assertEqual(spy.call_count, reads)
                for number in range(3):
                    make_workspace(self.owner, f"増えた{number}{user is None}")
                with self.assertNumQueries(len(one.captured_queries)):
                    self.client.get(url)

    def test_the_switch_is_read_only_when_drawn(self):
        """コンテキストプロセッサは一覧を読まずに渡し、描いたとき（回したとき）に1回だけ読む（遅延オブジェクト）。"""
        request = RequestFactory().get("/")
        request.user = self.owner
        request.workspace = build_workspace_access_query().resolve(self.ws.key, self.owner.id)
        with self.assertNumQueries(0):
            context = context_processors.workspace(request)
        self.assertTrue(context["can_manage_workspace"])
        with self.assertNumQueries(1):
            names = [row.name for row in context["my_workspaces"]]
            names_again = [row.name for row in context["my_workspaces"]]
        self.assertEqual(names, [self.ws.name, self.other.name])
        self.assertEqual(names_again, names)
