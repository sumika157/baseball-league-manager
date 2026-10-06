"""戦力分析の起用マップ（野球場の背景に守備位置の箱を置く）のレイアウトの E2E テスト。

箱の重なりと背景の描画、狭い画面での横はみ出しは CSS の効き方の問題で
integration では確かめられないため、実ブラウザで測る（#144）。
"""

from django.urls import reverse

from myapp.infrastructure import orm_models

from ..helpers import build_service
from .base import PlaywrightTestCase


class UsageMapLayoutTest(PlaywrightTestCase):
    def setUp(self):
        super().setUp()
        league = orm_models.League.objects.create(name="Eリーグ")
        self.team = orm_models.Team.objects.create(league=league, name="ホームズ")
        service = build_service()
        for number in range(1, 8):
            player = service.register_player(self.team.id, f"サルバドール・グティエレス{number}", number, "外野手")
            orm_models.Player.objects.filter(id=player.id).update(throws="右", bats="左")

    def _open(self, width: int, color: str = "hand") -> None:
        self.page.set_viewport_size({"width": width, "height": 900})
        url = reverse("team_analysis", args=[self.team.id])
        self.page.goto(self.live_server_url + f"{url}?tab=usage&color={color}")

    def test_boxes_do_not_overlap_on_a_wide_screen(self):
        self._open(1280)
        boxes = self.page.locator(".usage-box")
        rects = [boxes.nth(i).bounding_box() for i in range(boxes.count())]
        self.assertEqual(len(rects), 10)
        for i, a in enumerate(rects):
            for b in rects[i + 1 :]:
                assert a is not None and b is not None
                apart = (
                    a["x"] + a["width"] <= b["x"] + 0.5
                    or b["x"] + b["width"] <= a["x"] + 0.5
                    or a["y"] + a["height"] <= b["y"] + 0.5
                    or b["y"] + b["height"] <= a["y"] + 0.5
                )
                self.assertTrue(apart, f"箱が重なっている: {a} と {b}")

    def test_field_background_is_drawn_on_a_wide_screen_only(self):
        self._open(1280)
        wide = self.page.evaluate("getComputedStyle(document.querySelector('.usage-map')).backgroundImage")
        self.assertNotEqual(wide, "none")
        self._open(390)
        narrow = self.page.evaluate("getComputedStyle(document.querySelector('.usage-map')).backgroundImage")
        self.assertEqual(narrow, "none")

    def test_page_does_not_overflow_horizontally_on_a_narrow_screen(self):
        for color in ("hand", "natural"):
            with self.subTest(color=color):
                self._open(390, color)
                widths = self.page.evaluate(
                    "() => ({page: document.documentElement.scrollWidth, viewport: window.innerWidth})"
                )
                self.assertLessEqual(widths["page"], widths["viewport"])
