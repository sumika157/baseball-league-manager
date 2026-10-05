"""サイト共通のヘッダーのレイアウトの E2E テスト。

戦力分析への導線を足したとき、その右に余白が無く「ログイン」と文字が詰まって並んだ（#122）。
CSS の効き方の問題で integration では確かめられないため、実ブラウザで測る。
"""

from django.urls import reverse

from .base import PlaywrightTestCase

# 要素どうしの最低限の間隔（px）。ヘッダーの他の要素の間は 14〜16px 空いている
MIN_GAP = 8


class HeaderLayoutTest(PlaywrightTestCase):
    def _gap_after_analysis_link(self, width: int) -> float:
        self.page.set_viewport_size({"width": width, "height": 800})
        self.page.goto(self.live_server_url + reverse("dashboard"))
        link = self.page.locator(".nav-analysis-link").bounding_box()
        user = self.page.locator(".app-nav-user").bounding_box()
        assert link is not None and user is not None
        return user["x"] - (link["x"] + link["width"])

    def test_analysis_link_does_not_touch_the_login_area(self):
        """広い画面でも、検索欄が隠れる狭い画面でも、戦力分析の右に間隔があること。"""
        for width in (1280, 390):
            with self.subTest(width=width):
                self.assertGreaterEqual(self._gap_after_analysis_link(width), MIN_GAP)
