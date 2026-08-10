"""E2E配線の動作確認用スモークテスト。

Playwrightの導入確認が目的で、既存機能の網羅的なE2E化はここでは行わない。
"""

from .base import PlaywrightTestCase


class SmokeTest(PlaywrightTestCase):
    def test_dashboard_requires_login(self):
        self.page.goto(self.live_server_url + '/')
        self.assertRegex(self.page.url, r'/accounts/login/')

    def test_login_page_renders_form(self):
        self.page.goto(self.live_server_url + '/accounts/login/')
        self.assertEqual(self.page.locator('input[name="username"]').count(), 1)
        self.assertEqual(self.page.locator('input[name="password"]').count(), 1)
