"""閲覧と書き込みの境界。ログイン・新規登録と、チーム担当者の権限。"""

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from myapp.infrastructure import orm_models

from ..helpers import (
    login_as_manager,
    play_game,
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
        self.player = self.service.register_player(self.team.id, "山田", 10, "内野手")

    def test_reading_needs_no_login(self):
        pages = [
            reverse("dashboard"),
            reverse("team_list"),
            reverse("player_list", args=[self.team.id]),
            reverse("player_detail", args=[self.team.id, self.player.id]),
            reverse("game_list"),
            reverse("standings"),
        ]
        for url in pages:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_writing_pages_redirect_to_login(self):
        for url in (
            reverse("game_create"),
            reverse("player_edit", args=[self.team.id, self.player.id]),
        ):
            with self.subTest(url=url):
                response = self.client.get(url)

                self.assertEqual(response.status_code, 302)
                self.assertIn("/accounts/login/", response["Location"])

    def test_registering_a_player_without_login_changes_nothing(self):
        before = orm_models.Player.objects.count()

        response = self.client.post(
            reverse("player_list", args=[self.team.id]),
            {"name": "侵入", "number": "99", "position": "内野手"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])
        self.assertEqual(orm_models.Player.objects.count(), before)

    def test_editing_a_player_without_login_changes_nothing(self):
        response = self.client.post(
            reverse("player_edit", args=[self.team.id, self.player.id]),
            {"name": "改ざん", "number": "10", "position": "内野手"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])
        self.assertEqual(orm_models.Player.objects.get(pk=self.player.id).name, "山田")

    def test_retiring_a_player_without_login_changes_nothing(self):
        self.client.post(
            reverse("player_edit", args=[self.team.id, self.player.id]),
            {"retire": "1"},
        )

        stint = orm_models.PlayerStint.objects.get(player_id=self.player.id)
        self.assertIsNone(stint.to_year)

    def test_write_controls_are_hidden_from_anonymous_visitors(self):
        """押せない導線は見せない（試合の画面と同じ扱いに揃える）。"""
        listing = self.client.get(reverse("player_list", args=[self.team.id]))
        detail = self.client.get(reverse("player_detail", args=[self.team.id, self.player.id]))

        self.assertNotContains(listing, "新入団選手の登録")
        self.assertNotContains(detail, "player_edit")
        self.assertNotContains(detail, reverse("player_edit", args=[self.team.id, self.player.id]))


class TeamManagerPermissionTest(BaseCase):
    """チーム担当者制（フェーズ5）。

    ログインしただけでは書き込めない。担当するチームが関わる範囲だけを
    編集でき、管理ユーザーは担当に関わらず全権を持つ。
    """

    def setUp(self):
        super().setUp()
        self.player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        self.game = play_game(self.team, self.rival)
        # 相手チームとも別リーグとも無関係な、どこも担当していないチーム
        self.outsider = orm_models.Team.objects.create(league=self.league, name="無関係チーム")

    def _player_edit_url(self):
        return reverse("player_edit", args=[self.team.id, self.player.id])

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

    def test_staff_can_edit_any_team(self):
        self.client.force_login(User.objects.create_user(username="staff", password="x", is_staff=True))
        self.assertEqual(self.client.get(self._player_edit_url()).status_code, 200)

    def test_anonymous_is_sent_to_login_not_403(self):
        """未ログインは拒否ではなくログインへ誘導する。まだ入る余地があるため。"""
        response = self.client.get(self._player_edit_url())
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

    def test_non_manager_cannot_register_a_player(self):
        self.client.force_login(User.objects.create_user(username="other", password="x"))

        response = self.client.post(
            reverse("player_list", args=[self.team.id]),
            {"name": "田中", "number": "11", "position": "外野手"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(orm_models.PlayerStint.objects.filter(number=11).exists())

    def test_player_list_hides_write_controls_from_non_managers(self):
        self.client.force_login(User.objects.create_user(username="other", password="x"))

        response = self.client.get(reverse("player_list", args=[self.team.id]))

        self.assertEqual(response.status_code, 200)  # 閲覧はできる
        self.assertNotContains(response, "新入団選手の登録")

    def test_player_list_shows_write_controls_to_managers(self):
        login_as_manager(self.client, self.team)

        response = self.client.get(reverse("player_list", args=[self.team.id]))

        self.assertContains(response, "新入団選手の登録")

    # --- 試合 ---

    def test_manager_of_either_side_can_edit_the_game(self):
        """試合は2チームにまたがる。どちらか一方の担当者なら編集できる。"""
        login_as_manager(self.client, self.rival)
        self.assertEqual(self.client.get(reverse("game_edit", args=[self.game.id])).status_code, 200)

    def test_manager_of_an_uninvolved_team_cannot_edit_the_game(self):
        login_as_manager(self.client, self.outsider)
        self.assertEqual(self.client.get(reverse("game_edit", args=[self.game.id])).status_code, 403)

    def test_game_detail_hides_the_edit_link_from_non_managers(self):
        login_as_manager(self.client, self.outsider)

        response = self.client.get(reverse("game_detail", args=[self.game.id]))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "記録を編集")

    def test_creating_a_game_between_teams_you_do_not_manage_is_refused(self):
        login_as_manager(self.client, self.outsider)

        response = self.client.post(
            reverse("game_create"),
            {
                "year": "2026",
                "played_on": "2026-04-02",
                "home_team": self.team.id,
                "away_team": self.rival.id,
                "home_score": "1",
                "away_score": "0",
            },
            follow=True,
        )

        self.assertContains(response, "どちらのチームも担当していない")
        self.assertEqual(orm_models.Game.objects.count(), 1)  # 既存の1件のみ

    def test_game_list_hides_the_create_link_when_you_manage_nothing(self):
        self.client.force_login(User.objects.create_user(username="other", password="x"))

        response = self.client.get(reverse("game_list"))

        self.assertNotContains(response, "試合を登録")
