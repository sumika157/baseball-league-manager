"""認証まわりの URL。パスワード再設定の画面は公開しない（メールを送らないため）。

`django.contrib.auth.urls` をまるごと読み込むと再設定の画面まで公開され、登録済みのアドレスを
入れると存在しないメールサーバーへ送ろうとして 500 になる。ログイン・ログアウト・パスワード変更だけを
明示的に並べているので、その外が 404 のままであること、並べたものが動くことを確かめる。
"""

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import NoReverseMatch, reverse


class PasswordResetNotExposedTest(TestCase):
    RESET_PATHS = (
        "/accounts/password_reset/",
        "/accounts/password_reset/done/",
        "/accounts/reset/MQ/set-password/",
        "/accounts/reset/done/",
    )

    def test_password_reset_urls_are_404(self):
        for path in self.RESET_PATHS:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_password_reset_post_is_404(self):
        response = self.client.post("/accounts/password_reset/", {"email": "a@example.com"})
        self.assertEqual(response.status_code, 404)

    def test_password_reset_url_names_do_not_exist(self):
        for name in ("password_reset", "password_reset_done", "password_reset_confirm", "password_reset_complete"):
            with self.subTest(name=name), self.assertRaises(NoReverseMatch):
                reverse(name)

    def test_login_page_has_no_reset_link(self):
        response = self.client.get(reverse("login"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "password_reset")
        self.assertNotContains(response, "パスワードを忘れ")


class AuthUrlsStillWorkTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="member", password="old-Pass-12345")

    def test_login(self):
        response = self.client.post(reverse("login"), {"username": "member", "password": "old-Pass-12345"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("_auth_user_id", self.client.session)

    def test_logout(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse("logout"))
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_password_change_and_done(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("password_change")).status_code, 200)
        response = self.client.post(
            reverse("password_change"),
            {
                "old_password": "old-Pass-12345",
                "new_password1": "new-Pass-67890x",
                "new_password2": "new-Pass-67890x",
            },
        )
        self.assertRedirects(response, reverse("password_change_done"))
        self.assertEqual(self.client.get(reverse("password_change_done")).status_code, 200)
