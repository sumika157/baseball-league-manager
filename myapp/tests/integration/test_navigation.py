"""ヘッダーの導線。"""

from django.contrib.auth.models import User
from django.test import TestCase

from ..helpers import default_workspace, ws_url


class HeaderNavigationTest(TestCase):
    """ヘッダーの導線が権限に応じて出し分けられること。"""

    # 「ペナント」の入口も同じ見た目（nav-admin-link）なので、管理画面へのリンクで見分ける
    ADMIN_LINK = 'href="/admin/" class="nav-admin-link"'

    def setUp(self):
        # 今の URL はワークスペースを1つ解決する（無ければ 404）
        default_workspace()
        # 管理画面に入れるのは superuser だけ（W4 T6）。staff の印だけでは入れず、リンクも出ない
        self.operator = User.objects.create_superuser(username="root", password="x")
        self.staff = User.objects.create_user(username="staff", password="x", is_staff=True)
        self.member = User.objects.create_user(username="member", password="x")

    def test_the_superuser_sees_admin_link(self):
        self.client.force_login(self.operator)
        response = self.client.get(ws_url("dashboard"))
        self.assertContains(response, self.ADMIN_LINK)

    def test_staff_who_is_not_a_superuser_does_not_see_admin_link(self):
        self.client.force_login(self.staff)
        self.assertNotContains(self.client.get(ws_url("dashboard")), self.ADMIN_LINK)

    def test_normal_user_does_not_see_admin_link(self):
        self.client.force_login(self.member)
        self.assertNotContains(self.client.get(ws_url("dashboard")), self.ADMIN_LINK)

    def test_anonymous_does_not_see_admin_link(self):
        response = self.client.get(ws_url("dashboard"))
        self.assertNotContains(response, self.ADMIN_LINK)
        self.assertContains(response, "ログイン")

    def test_admin_page_actually_rejects_normal_user_and_staff(self):
        for user in (self.member, self.staff):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.client.get("/admin/")
                self.assertEqual(response.status_code, 302)
                self.assertIn("/admin/login/", response["Location"])

    def test_admin_page_accepts_the_superuser(self):
        self.client.force_login(self.operator)
        self.assertEqual(self.client.get("/admin/").status_code, 200)
