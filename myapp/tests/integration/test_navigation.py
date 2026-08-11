"""ヘッダーの導線。"""

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse


class HeaderNavigationTest(TestCase):
    """ヘッダーの導線が権限に応じて出し分けられること。"""

    ADMIN_LINK = 'class="nav-admin-link"'

    def setUp(self):
        self.staff = User.objects.create_user(username="staff", password="x", is_staff=True)
        self.member = User.objects.create_user(username="member", password="x")

    def test_staff_sees_admin_link(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, self.ADMIN_LINK)

    def test_normal_user_does_not_see_admin_link(self):
        self.client.force_login(self.member)
        self.assertNotContains(self.client.get(reverse("dashboard")), self.ADMIN_LINK)

    def test_anonymous_does_not_see_admin_link(self):
        response = self.client.get(reverse("dashboard"))
        self.assertNotContains(response, self.ADMIN_LINK)
        self.assertContains(response, "ログイン")

    def test_admin_page_actually_rejects_normal_user(self):
        self.client.force_login(self.member)
        response = self.client.get("/admin/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])

    def test_admin_page_accepts_staff(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get("/admin/").status_code, 200)
