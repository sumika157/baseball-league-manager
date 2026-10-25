"""閲覧と書き込みの境界。ログイン・新規登録と、チーム担当者の権限。"""

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from myapp.infrastructure import orm_models

from ..helpers import (
    login_as_manager,
    make_editor,
    make_workspace,
    play_game,
    post_game_scorebook_day_unrecorded,
    ws_url,
)
from .base import BaseCase


class AuthTest(TestCase):
    def test_login_redirect_url_resolves(self):
        from django.conf import settings

        self.assertTrue(reverse(settings.LOGIN_REDIRECT_URL))

    def test_signup_page_is_reachable(self):
        self.assertEqual(self.client.get("/accounts/signup/").status_code, 200)


class WriteRequiresLoginTest(BaseCase):
    """閲覧は誰でも、書き込みはログインした人だけ。

    以前は試合だけがログイン必須で、選手の登録・編集・退団は
    未ログインのまま実行できていた。画面ごとに要否が違うと、
    どこが公開範囲なのか読み取れなくなる。
    """

    def setUp(self):
        super().setUp()
        self.player = self.roster.register_player(self.team.id, "山田", 10, "内野手")

    def test_reading_needs_no_login(self):
        pages = [
            ws_url("dashboard"),
            ws_url("team_list"),
            ws_url("player_list", self.team.id),
            ws_url("player_detail", self.team.id, self.player.id),
            ws_url("game_list"),
            ws_url("standings"),
        ]
        for url in pages:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_writing_pages_redirect_to_login(self):
        for url in (
            ws_url("game_create"),
            ws_url("player_edit", self.team.id, self.player.id),
        ):
            with self.subTest(url=url):
                response = self.client.get(url)

                self.assertEqual(response.status_code, 302)
                self.assertIn("/accounts/login/", response["Location"])

    def test_registering_a_player_without_login_changes_nothing(self):
        before = orm_models.Player.objects.count()

        response = self.client.post(
            ws_url("player_list", self.team.id),
            {"name": "侵入", "number": "99", "position": "内野手"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])
        self.assertEqual(orm_models.Player.objects.count(), before)

    def test_editing_a_player_without_login_changes_nothing(self):
        response = self.client.post(
            ws_url("player_edit", self.team.id, self.player.id),
            {"name": "改ざん", "number": "10", "position": "内野手"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])
        self.assertEqual(orm_models.Player.objects.get(pk=self.player.id).name, "山田")

    def test_retiring_a_player_without_login_changes_nothing(self):
        self.client.post(
            ws_url("player_edit", self.team.id, self.player.id),
            {"retire": "1"},
        )

        stint = orm_models.PlayerStint.objects.get(player_id=self.player.id)
        self.assertIsNone(stint.to_year)

    def test_write_controls_are_hidden_from_anonymous_visitors(self):
        """押せない導線は見せない（試合の画面と同じ扱いに揃える）。"""
        listing = self.client.get(ws_url("player_list", self.team.id))
        detail = self.client.get(ws_url("player_detail", self.team.id, self.player.id))

        self.assertNotContains(listing, "新入団選手の登録")
        self.assertNotContains(detail, "player_edit")
        self.assertNotContains(detail, ws_url("player_edit", self.team.id, self.player.id))


class TeamManagerPermissionTest(BaseCase):
    """チーム担当者制（フェーズ5）。

    ログインしただけでは書き込めない。担当するチームが関わる範囲だけを編集できる。
    ワークスペースの持ち主・編集者は担当に関わらず全体を編集できる。管理ユーザー（is_staff）・superuser でも、
    ワークスペースの部外者ならサイトでは何もできない（W2 T5 で特権を廃止。#78）。
    """

    def setUp(self):
        super().setUp()
        self.player = self.roster.register_player(self.team.id, "山田", 10, "内野手")
        self.game = play_game(self.team, self.rival)
        # 相手チームとも別リーグとも無関係な、どこも担当していないチーム
        self.outsider = orm_models.Team.objects.create(league=self.league, name="無関係チーム")

    def _player_edit_url(self):
        return ws_url("player_edit", self.team.id, self.player.id)

    # --- 選手 ---

    def test_manager_can_open_player_edit(self):
        login_as_manager(self.client, self.team)
        self.assertEqual(self.client.get(self._player_edit_url()).status_code, 200)

    def test_logged_in_non_manager_is_rejected(self):
        """ログインしていても担当外なら編集できない。"""
        self.client.force_login(User.objects.create_user(username="other", password="x"))
        self.assertEqual(self.client.get(self._player_edit_url()).status_code, 403)

    def test_manager_of_another_team_is_rejected(self):
        login_as_manager(self.client, self.outsider)
        self.assertEqual(self.client.get(self._player_edit_url()).status_code, 403)

    def test_staff_outside_the_workspace_cannot_edit(self):
        """特権の廃止の再発防止: is_staff・superuser でも、ワークスペースの部外者なら編集できない（W2 T5）。"""
        staff = User.objects.create_user(username="staff", password="x", is_staff=True)
        root = User.objects.create_superuser(username="root", password="x")
        for user in (staff, root):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                self.assertEqual(self.client.get(self._player_edit_url()).status_code, 403)
                self.assertEqual(self.client.get(ws_url("game_edit", self.game.id)).status_code, 403)
                self.assertNotContains(self.client.get(ws_url("player_list", self.team.id)), "新入団選手の登録")

    def test_an_editor_of_the_workspace_can_edit_any_team(self):
        """全体の編集はワークスペースの編集者（と持ち主）の権限。staff かどうかは関係ない。"""
        self.client.force_login(make_editor(User.objects.create_user(username="editor", password="x")))
        self.assertEqual(self.client.get(self._player_edit_url()).status_code, 200)
        self.assertEqual(self.client.get(ws_url("game_edit", self.game.id)).status_code, 200)

    def test_anonymous_is_sent_to_login_not_403(self):
        """未ログインは拒否ではなくログインへ誘導する。まだ入る余地があるため。"""
        response = self.client.get(self._player_edit_url())
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

    def test_non_manager_cannot_register_a_player(self):
        self.client.force_login(User.objects.create_user(username="other", password="x"))

        response = self.client.post(
            ws_url("player_list", self.team.id),
            {"name": "田中", "number": "11", "position": "外野手"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(orm_models.PlayerStint.objects.filter(number=11).exists())

    def test_player_list_hides_write_controls_from_non_managers(self):
        self.client.force_login(User.objects.create_user(username="other", password="x"))

        response = self.client.get(ws_url("player_list", self.team.id))

        self.assertEqual(response.status_code, 200)  # 閲覧はできる
        self.assertNotContains(response, "新入団選手の登録")

    def test_player_list_shows_write_controls_to_managers(self):
        login_as_manager(self.client, self.team)

        response = self.client.get(ws_url("player_list", self.team.id))

        self.assertContains(response, "新入団選手の登録")

    # --- 試合 ---

    def test_manager_of_either_side_can_edit_the_game(self):
        """試合は2チームにまたがる。どちらか一方の担当者なら編集できる。"""
        login_as_manager(self.client, self.rival)
        self.assertEqual(self.client.get(ws_url("game_edit", self.game.id)).status_code, 200)

    def test_manager_of_an_uninvolved_team_cannot_edit_the_game(self):
        login_as_manager(self.client, self.outsider)
        self.assertEqual(self.client.get(ws_url("game_edit", self.game.id)).status_code, 403)

    def test_game_detail_hides_the_edit_link_from_non_managers(self):
        login_as_manager(self.client, self.outsider)

        response = self.client.get(ws_url("game_detail", self.game.id))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "記録を編集")

    def test_creating_a_game_between_teams_you_do_not_manage_is_refused(self):
        login_as_manager(self.client, self.outsider)

        response = self.client.post(
            ws_url("game_create"),
            {
                "year": "2026",
                "played_on": "2026-04-02",
                "home_team": self.team.id,
                "away_team": self.rival.id,
            },
            follow=True,
        )

        self.assertContains(response, "どちらのチームも担当していない")
        self.assertEqual(orm_models.Game.objects.count(), 1)  # 既存の1件のみ

    def test_game_list_hides_the_create_link_when_you_manage_nothing(self):
        self.client.force_login(User.objects.create_user(username="other", password="x"))

        response = self.client.get(ws_url("game_list"))

        self.assertNotContains(response, "試合を登録")

    def test_the_scorebook_cannot_move_a_game_to_teams_you_do_not_manage(self):
        """スコアブックの保存は、書き換えた後の対戦カードでも判定する（担当していない2チームの試合に替えさせない）。

        保存前の試合の2チームだけで判定していたため、片方の担当者が対戦カードを担当外の2チームに替えられた（W2 T5 で
        直した。再発防止）。
        """
        game = play_game(self.team, self.rival, recorded=False)
        login_as_manager(self.client, self.team)
        before = orm_models.Game.objects.filter(id=game.id).values().get()

        response = post_game_scorebook_day_unrecorded(
            self.client,
            game.id,
            {
                "year": 2026,
                "played_on": "2026-06-01",
                "home_team": self.rival.id,
                "away_team": self.outsider.id,
                "lineup": [],
                "plate_appearances": [],
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(orm_models.Game.objects.filter(id=game.id).values().get(), before)

    def test_the_scorebook_keeps_a_game_with_one_of_your_teams(self):
        """空振りの防止: 担当のチームが残る対戦カードなら保存できる。"""
        game = play_game(self.team, self.rival, recorded=False)
        login_as_manager(self.client, self.team)

        response = post_game_scorebook_day_unrecorded(
            self.client,
            game.id,
            {
                "year": 2026,
                "played_on": "2026-06-01",
                "home_team": self.team.id,
                "away_team": self.outsider.id,
                "lineup": [],
                "plate_appearances": [],
            },
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        self.assertEqual(orm_models.Game.objects.get(id=game.id).away_team_id, self.outsider.id)


class EmptyWorkspaceGameCreateTest(BaseCase):
    """チームの無いワークスペースでは、持ち主でも試合の登録の導線を出さず、画面は一覧へ戻す（対戦カードを選べない）。"""

    def test_the_owner_of_a_workspace_without_teams_is_sent_back_to_the_list(self):
        workspace = make_workspace(name="空のワークスペース")
        self.client.force_login(workspace.owner)

        listing = self.client.get(ws_url("game_list", workspace=workspace))
        response = self.client.get(ws_url("game_create", workspace=workspace))

        self.assertNotContains(listing, ws_url("game_create", workspace=workspace))
        self.assertRedirects(response, ws_url("game_list", workspace=workspace), fetch_redirect_response=False)

    def test_an_outsider_of_a_workspace_without_teams_gets_403(self):
        workspace = make_workspace(name="空のワークスペース")
        self.client.force_login(User.objects.create_user(username="outsider", password="x"))

        self.assertEqual(self.client.get(ws_url("game_create", workspace=workspace)).status_code, 403)
