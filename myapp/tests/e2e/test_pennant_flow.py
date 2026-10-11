"""ペナントの通し導線のスモーク（P4b）。

世界を作る → GM ホームで1日進める → 結果のまとめ → 順位表とボックススコアを読む、を実ブラウザで1本だけ確かめる。
権限・上限・検証エラーなどの分岐は integration（`test_pennant_home.py`・`test_pennant_worlds.py`）で見ている。
ここでは、フォームの送信・リダイレクト・画面遷移がブラウザで繋がることと、
ペナントの画面のリンクが世界の外に出ないことだけを確かめる。
"""

import re

from django.contrib.auth.models import User

from myapp.domain.pennant.schedule import AdvanceTarget
from myapp.infrastructure import orm_models
from myapp.presentation.views import build_pennant_season_service

from ..helpers import build_service
from ..integration.test_pennant_advance import register_club
from .base import PlaywrightTestCase

# ペナントの画面のリンクが向いてよい、世界の外の行き先（ヘッダーの出口）
HEADER_PAGES = ("/", "/players/", "/pennant/")
HEADER_PREFIXES = ("/accounts/", "/admin/")


class PennantFlowTest(PlaywrightTestCase):
    def setUp(self):
        super().setUp()
        league = orm_models.League.objects.create(name="Pリーグ")
        stadium = orm_models.Stadium.objects.create(name="P球場", city="東京")
        self.team = orm_models.Team.objects.create(league=league, name="ホームズ", home_stadium=stadium)
        self.rival = orm_models.Team.objects.create(league=league, name="ライバルズ")
        service = build_service()
        register_club(service, self.team, "ホ")
        register_club(service, self.rival, "ラ")
        User.objects.create_user(username="gm", password="pass12345")

    def _login(self):
        self.page.goto(self.live_server_url + "/accounts/login/")
        self.page.fill('input[name="username"]', "gm")
        self.page.fill('input[name="password"]', "pass12345")
        self.page.locator('input[name="password"]').press("Enter")
        self.page.wait_for_url(f"{self.live_server_url}/")

    def _assert_links_stay_in_the_world(self, world_id: int):
        prefix = f"/pennant/{world_id}/"
        hrefs = self.page.eval_on_selector_all("a", "els => els.map(e => e.getAttribute('href'))")
        self.assertGreater(len(hrefs), 0)
        for href in hrefs:
            allowed = href.startswith((prefix, "?", "#", *HEADER_PREFIXES)) or href in HEADER_PAGES
            self.assertTrue(allowed, f"{self.page.url} のリンク {href} が世界の外へ出ています")

    def test_create_advance_and_read(self):
        self._login()

        # 世界を作る（元にするリーグと受け持つ球団を選ぶ）
        self.page.goto(self.live_server_url + "/pennant/")
        self.page.fill('input[name="name"]', "スモークの世界")
        self.page.check('input[name="leagues"]')
        self.page.select_option('select[name="managed_team"]', label="ホームズ")
        self.page.click('button:has-text("世界を作る")')
        self.page.wait_for_url(re.compile(r"/pennant/\d+/$"))
        world_id = int(re.search(r"/pennant/(\d+)/", self.page.url).group(1))
        self.assertIn("GM ホーム", self.page.content())
        self._assert_links_stay_in_the_world(world_id)

        # 1日進める。結果のまとめ付きでホームに戻る
        self.page.click('button:has-text("1日進める")')
        self.page.wait_for_url(re.compile(r"\?since="))
        self.assertIn("進めた結果", self.page.content())
        self._assert_links_stay_in_the_world(world_id)

        # 順位表を読む
        self.page.click('.segmented a:has-text("順位表")')
        self.page.wait_for_url(re.compile(r"/standings/$"))
        self.assertEqual(self.page.locator("tbody tr").count(), 2)
        self._assert_links_stay_in_the_world(world_id)

        # ボックススコアを読む（試合一覧 → 試合詳細）
        self.page.click('.segmented a:has-text("試合")')
        self.page.wait_for_url(re.compile(r"/games/$"))
        self.page.locator('a:has-text("詳細")').first.click()
        self.page.wait_for_url(re.compile(r"/games/\d+/$"))
        self.assertIn("シミュレーション", self.page.content())
        self._assert_links_stay_in_the_world(world_id)

        # 実データには試合が増えていない
        self.assertEqual(orm_models.Game.objects.filter(home_team__league__world__isnull=True).count(), 0)

    def test_close_the_season_read_the_result_and_start_the_next_year(self):
        self._login()
        self.page.goto(self.live_server_url + "/pennant/")
        self.page.fill('input[name="name"]', "オフの世界")
        self.page.check('input[name="leagues"]')
        self.page.select_option('select[name="managed_team"]', label="ホームズ")
        self.page.click('button:has-text("世界を作る")')
        self.page.wait_for_url(re.compile(r"/pennant/\d+/$"))
        world_id = int(re.search(r"/pennant/(\d+)/", self.page.url).group(1))

        # 1シーズンの進行はサービスで済ませる（画面で143試合ぶんを押さない）。締める操作から画面で確かめる
        build_pennant_season_service(world_id).advance(AdvanceTarget.SEASON_END)
        self.page.reload()
        self.assertEqual(self.page.locator('button:has-text("1日進める")').count(), 0, "終了後は進めない")

        # 締める。確認はボタン1つ（文言で「元に戻せません」を伝える）
        self.assertIn("元に戻せません", self.page.content())
        self.page.click('button:has-text("のシーズンを締める")')
        self.page.wait_for_url(re.compile(r"/offseason/\d+/$"))
        self.assertIn("シーズンを締めました", self.page.content())
        self.assertIn("オフ（", self.page.locator("h1").inner_text())
        self._assert_links_stay_in_the_world(world_id)

        # ホームに戻ると、前年の順位とオフの結果へのリンクが出て、翌年を1日進められる
        self.page.click('.segmented a:has-text("ホーム")')
        self.page.wait_for_url(re.compile(rf"/pennant/{world_id}/$"))
        self.assertIn("年の最終順位", self.page.content())
        self.assertIn("のオフの結果", self.page.content())
        self.page.click('button:has-text("1日進める")')
        self.page.wait_for_url(re.compile(r"\?since="))
        self.assertIn("進めた結果", self.page.content())
        self._assert_links_stay_in_the_world(world_id)
