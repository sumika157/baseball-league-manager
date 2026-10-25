"""空のワークスペースから、ブラウザだけでリーグを組み立てて試合を登録する（W3 T3。#79 の完了条件）。

管理画面で作ったばかりの空のワークスペースの持ち主（staff ではない）が、トップ → ワークスペース → ヘッダーの
「登録」から入り、球場 → リーグ → チーム2つ → 選手の登録 → 試合の登録までを、画面の操作だけで行う。
個々の画面の分岐（フォームのエラー・消せない行・権限）は integration（`test_setup_screens.py` ほか）で見ている。
ここでは、フォームの送信・リダイレクト・タブとリンクの遷移がブラウザで繋がることだけを確かめる。
"""

import re

from django.contrib.auth.models import User

from myapp.infrastructure import orm_models

from ..helpers import make_workspace, ws_url
from .base import PlaywrightTestCase

WORKSPACE_NAME = "はじめてのワークスペース"


class SetupFlowTest(PlaywrightTestCase):
    def setUp(self):
        super().setUp()
        # 運営者が管理画面で作った空のワークスペース（テストの準備は ORM で作る）。持ち主は staff ではない
        owner = User.objects.create_user(username="owner", password="pass12345")
        self.workspace = make_workspace(owner=owner, name=WORKSPACE_NAME)

    def _login(self):
        self.page.goto(self.live_server_url + "/accounts/login/")
        self.page.fill('input[name="username"]', "owner")
        self.page.fill('input[name="password"]', "pass12345")
        self.page.locator('input[name="password"]').press("Enter")
        self.page.wait_for_url(f"{self.live_server_url}/")

    def _url(self, name: str, *args: object) -> str:
        return self.live_server_url + ws_url(name, *args, workspace=self.workspace)

    def _submit_and_wait(self, button: str, url_name: str, *args: object) -> None:
        self.page.click(f'button:has-text("{button}")')
        self.page.wait_for_url(self._url(url_name, *args))

    def test_the_owner_builds_a_league_and_registers_a_game(self):
        self._login()

        # トップの一覧からワークスペースへ。空のダッシュボードには登録の案内が出る
        self.page.click(f'a:has-text("{WORKSPACE_NAME}")')
        self.page.wait_for_url(self._url("dashboard"))
        self.assertIn("リーグ・球場・チームを登録する", self.page.content())

        # ヘッダーの「登録」から入る（最初はリーグのタブ）
        self.page.click('.app-nav-user a:has-text("登録")')
        self.page.wait_for_url(self._url("setup_leagues"))

        # 球場
        self.page.click('.nav-tabs a:has-text("球場")')
        self.page.wait_for_url(self._url("setup_stadiums"))
        self.page.fill('input[name="name"]', "はじめての球場")
        self.page.fill('input[name="capacity"]', "30000")
        self._submit_and_wait("作る", "setup_stadiums")
        self.assertIn("はじめての球場", self.page.content())

        # リーグ（外国人枠・支配下枠は既定のまま）
        self.page.click('.nav-tabs a:has-text("リーグ")')
        self.page.wait_for_url(self._url("setup_leagues"))
        self.page.fill('input[name="name"]', "はじめてのリーグ")
        self._submit_and_wait("作る", "setup_leagues")
        self.assertIn("はじめてのリーグ", self.page.content())

        # チーム2つ（1つは本拠地あり、1つは本拠地なし）
        self.page.click('.nav-tabs a:has-text("チーム")')
        self.page.wait_for_url(self._url("setup_teams"))
        for name, home in (("ホームズ", "はじめての球場"), ("ビジターズ", "なし")):
            self.page.fill('input[name="name"]', name)
            self.page.select_option('select[name="league"]', label="はじめてのリーグ")
            self.page.select_option('select[name="home_stadium"]', label=home)
            self._submit_and_wait("作る", "setup_teams")
        league = orm_models.League.objects.get(workspace=self.workspace)
        home_team = orm_models.Team.objects.get(league=league, name="ホームズ")
        away_team = orm_models.Team.objects.get(league=league, name="ビジターズ")
        self.assertEqual(home_team.home_stadium.name, "はじめての球場")
        self.assertIsNone(away_team.home_stadium_id)

        # 選手の登録（チームの一覧 → チームの画面の登録フォーム）
        self.page.goto(self._url("team_list"))
        self.page.click('a:has-text("ホームズ")')
        self.page.wait_for_url(self._url("player_list", home_team.id))
        self.page.fill('input[name="name"]', "はじめての選手")
        self.page.fill('input[name="number"]', "18")
        self.page.select_option('select[name="position"]', label="投手")
        # 登録すると、登録した守備位置の表のタブ（?pos=）で選手一覧へ戻る
        self.page.click('button:has-text("登録")')
        self.page.wait_for_url(re.compile(re.escape(self._url("player_list", home_team.id)) + r"\?pos="))
        self.assertIn("はじめての選手", self.page.content())

        # 試合の登録（試合一覧 → 試合を登録 → スコアブックへ）
        self.page.goto(self._url("game_list"))
        self.page.click('a:has-text("試合を登録")')
        self.page.wait_for_url(self._url("game_create"))
        self.page.fill('input[name="year"]', "2026")
        self.page.fill('input[name="played_on"]', "2026-04-01")
        self.page.select_option('select[name="home_team"]', value=str(home_team.id))
        self.page.select_option('select[name="away_team"]', value=str(away_team.id))
        self.page.click('button:has-text("登録してスコアブックへ")')
        self.page.wait_for_url(re.compile(r"/games/\d+/edit/"))

        game = orm_models.Game.objects.get(home_team=home_team)
        self.assertEqual((game.away_team_id, game.year), (away_team.id, 2026))
        self.assertTrue(orm_models.PlayerStint.objects.filter(team=home_team, player__name="はじめての選手").exists())
