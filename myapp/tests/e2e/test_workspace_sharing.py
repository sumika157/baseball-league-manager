"""2人目を招待して共同で編集する（W4 T4。#80 の完了条件）。

新規登録 → ワークスペースの作成（非公開。見本のリーグは既定のまま入れ、ダッシュボードに順位表が出る。W5）
→ 担当させるチームと選手はテストの補助で足す → メンバーの画面で
招待のリンクを発行 → 別の人が招待のリンクから新規登録して受諾 → 担当チームの選手を編集 → 未ログインでは見えない →
持ち主が公開にすると見える、までを実ブラウザで通す。個々の分岐（門・フォームのエラー・期限切れ・既にメンバー）は
integration（`test_workspace_members.py` ほか）で見ている。ここでは、フォームの送信・リダイレクト・`next` による戻りが
ブラウザで繋がることだけを確かめる。ブラウザのページはそれぞれ別のコンテキスト（クッキー）なので、持ち主・招待された人・
未ログインの人を別のページで扱う。
"""

import re

from myapp.domain.sample.league import SAMPLE_GAME_COUNT, SAMPLE_TEAM_COUNT
from myapp.domain.sample.names import SAMPLE_CLUBS, SAMPLE_LEAGUE_NAME
from myapp.infrastructure import orm_models

from ..helpers import TEST_YEAR, build_roster_service, make_league, real_scope, ws_url
from .base import PlaywrightTestCase

WORKSPACE_NAME = "仲間のリーグ"
PASSWORD = "Sharing-Pass-123"


class WorkspaceSharingTest(PlaywrightTestCase):
    def _sign_up_and_log_in(self, page, username: str, *, back_to: str) -> None:
        """新規登録してから、運ばれた `next` つきのログインの画面でログインし、`back_to` に戻る。"""
        page.fill('input[name="username"]', username)
        page.fill('input[name="password1"]', PASSWORD)
        page.fill('input[name="password2"]', PASSWORD)
        page.click('button:has-text("登録する")')
        page.wait_for_url(re.compile(r"/accounts/login/"))
        page.fill('input[name="username"]', username)
        page.fill('input[name="password"]', PASSWORD)
        page.locator('input[name="password"]').press("Enter")
        page.wait_for_url(back_to)

    def _url(self, name: str, *args: object) -> str:
        return self.live_server_url + ws_url(name, *args, workspace=self.workspace)

    def test_a_second_member_joins_by_invitation_and_edits_their_team(self):
        owner = self.page

        # 1. 持ち主: 新規登録 → ログイン → / のフォームで非公開のワークスペースを作る
        owner.goto(self.live_server_url + "/accounts/signup/")
        self._sign_up_and_log_in(owner, "owner", back_to=f"{self.live_server_url}/")
        owner.fill('input[name="name"]', WORKSPACE_NAME)
        owner.click('button:has-text("作る")')
        owner.wait_for_url(re.compile(r"/w/[a-z0-9]{10}/$"))
        self.workspace = orm_models.Workspace.objects.get(name=WORKSPACE_NAME)
        self.assertEqual(self.workspace.visibility, "private", "作成の既定は非公開")
        # 見本のリーグは既定でオン（W5）。作った直後のダッシュボードに見本のリーグの順位表が出る
        dashboard = owner.content()
        self.assertIn(f"見本のリーグ（{SAMPLE_TEAM_COUNT}チーム・{SAMPLE_GAME_COUNT}試合）を入れました", dashboard)
        self.assertIn(SAMPLE_LEAGUE_NAME, dashboard)
        self.assertIn(SAMPLE_CLUBS[0].team, dashboard)

        # 2. 中身はテストの補助で入れる（登録画面の流れは test_setup_flow.py）
        league = make_league("仲間リーグ", workspace=self.workspace)
        home = orm_models.Team.objects.create(league=league, name="ホームズ")
        orm_models.Team.objects.create(league=league, name="ビジターズ")
        player = build_roster_service(real_scope(self.workspace)).register_player(
            home.id, "共有太郎", "18", "投手", from_year=TEST_YEAR
        )

        # 3. ヘッダーのメニューからメンバーの画面へ。ホームズの担当として招待のリンクを発行する
        owner.goto(self._url("dashboard"))
        owner.click(".app-workspace-name")
        owner.click('.dropdown-menu a:has-text("メンバーと招待")')
        owner.wait_for_url(self._url("workspace_members"))
        invite_action = ws_url("workspace_invitation_create", workspace=self.workspace)
        invite_form = owner.locator(f'form[action="{invite_action}"]')
        invite_form.locator('select[name="role"]').select_option(label="担当チームを編集")
        invite_form.locator(f'input[name="teams"][value="{home.id}"]').check()
        invite_form.locator('button:has-text("招待のリンクを発行する")').click()
        owner.wait_for_url(self._url("workspace_members"))
        link = owner.locator("input[data-invitation-link]").input_value()
        self.assertRegex(link, r"/invitations/[A-Za-z0-9_-]+/$")

        # 4. 招待された人: リンクを開く（未ログイン） → 新規登録して受け取る → ログインして招待に戻る → 受け取る
        guest = self.browser.new_page()
        try:
            guest.goto(link)
            self.assertIn(WORKSPACE_NAME, guest.content())
            self.assertIn("チーム担当", guest.content())
            guest.click('a:has-text("新規登録して受け取る")')
            guest.wait_for_url(re.compile(r"/accounts/signup/"))
            self._sign_up_and_log_in(guest, "guest", back_to=link)
            guest.click('button:has-text("招待を受け取る")')
            guest.wait_for_url(self._url("dashboard"))
            self.assertIn("として参加しました", guest.content())

            # 5. 担当チームの選手を編集する
            guest.goto(self._url("player_edit", home.id, player.id))
            guest.get_by_label("よみがな").fill("キョウユウタロウ")
            guest.click('button:has-text("更新する")')
            guest.wait_for_url(f"**{ws_url('player_list', home.id, workspace=self.workspace)}**")
        finally:
            guest.close()
        self.assertEqual(orm_models.Player.objects.get(id=player.id).name_kana, "キョウユウタロウ")
        self.assertTrue(home.managers.filter(username="guest").exists())

        # 6. 未ログインの人には見えない（非公開。ログインへ）
        stranger = self.browser.new_page()
        try:
            stranger.goto(self._url("dashboard"))
            stranger.wait_for_url(re.compile(r"/accounts/login/"))
            self.assertNotIn(WORKSPACE_NAME, stranger.content())

            # 7. 持ち主が公開にすると、未ログインでも見える
            owner.goto(self._url("workspace_settings"))
            owner.select_option('select[name="visibility"]', label="公開")
            owner.click('button:has-text("保存する")')
            owner.wait_for_url(self._url("workspace_settings"))

            stranger.goto(self._url("dashboard"))
            stranger.wait_for_url(self._url("dashboard"))
            self.assertIn(WORKSPACE_NAME, stranger.content())
        finally:
            stranger.close()
