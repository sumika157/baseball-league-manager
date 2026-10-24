"""CSRF の検査が実際に効いていること、失敗したときの応答の形。

Django のテスト用 `Client` は既定で CSRF の検査を飛ばすので、ほかのテストが通っても
検査が効いている証拠にならない。ここだけ `enforce_csrf_checks=True` で検査する。
"""

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from myapp.presentation.csrf import API_ERROR_MESSAGE

from ..helpers import ws_url


class ScorebookApiCsrfTest(TestCase):
    """スコアブックの保存 API（`api_game_scorebook`）。存在しない試合に空の JSON を送り、403 かどうかだけを見る。

    URL はワークスペースの下（`/w/<key>/api/...`）。API かどうかはパスの形ではなく URL 名で決める（W2 T4）。
    """

    def setUp(self):
        self.url = ws_url("api_game_scorebook", 999999)
        self.assertTrue(self.url.startswith("/w/"), self.url)
        self.client = Client(enforce_csrf_checks=True)
        # ログイン済みにして、未ログインの 403 と CSRF の 403 を取り違えないようにする
        self.client.force_login(User.objects.create_user("csrf-check", password="x"))

    def _csrf_token(self) -> str:
        """CSRF クッキーを受け取り、その値を返す（画面と同じく、クッキーの値をヘッダーに載せる）。"""
        self.client.get(reverse("login"))
        return self.client.cookies["csrftoken"].value

    def test_post_without_token_is_rejected_with_japanese_json(self):
        response = self.client.post(self.url, data="{}", content_type="application/json")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response.json(), {"ok": False, "error": API_ERROR_MESSAGE})

    def test_post_with_token_header_passes_csrf(self):
        token = self._csrf_token()

        response = self.client.post(
            self.url, data="{}", content_type="application/json", headers={"X-CSRFToken": token}
        )

        self.assertNotEqual(response.status_code, 403)

    def test_post_with_wrong_token_is_rejected(self):
        self._csrf_token()

        response = self.client.post(
            self.url, data="{}", content_type="application/json", headers={"X-CSRFToken": "x" * 64}
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], API_ERROR_MESSAGE)


class CsrfFailureOutsideApiTest(TestCase):
    def test_non_api_failure_renders_403_template(self):
        """保存 API 以外は、JSON ではなく 403.html（日本語の HTML）を返す。"""
        client = Client(enforce_csrf_checks=True)

        response = client.post(reverse("login"), {"username": "a", "password": "b"})

        self.assertEqual(response.status_code, 403)
        self.assertTemplateUsed(response, "403.html")
        self.assertContains(response, "この操作はできません", status_code=403)


class ErrorTemplatesTest(TestCase):
    def test_disallowed_host_renders_400_template(self):
        """許可していないホスト名は、Django 既定の素っ気ない 400 ではなく 400.html を返す。"""
        response = Client().get("/", headers={"host": "unknown.invalid"})

        self.assertEqual(response.status_code, 400)
        self.assertTemplateUsed(response, "400.html")
