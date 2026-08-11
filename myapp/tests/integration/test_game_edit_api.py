"""結合テスト: 試合編集画面の保存 API（presentation/api.py）と、画面（GET）の器。

画面の描画（GET）自体は既存の game_edit ビューのテストで扱われている範囲が
大半なので、ここでは保存 API の正常系・異常系と、React のマウント先・
初期データがテンプレートに描画されていることだけを確認する。
"""

import json

from django.urls import reverse

from myapp.infrastructure import orm_models

from ..helpers import api_inning_rows, login_as_manager, play_game, post_game_update
from .base import BaseCase


class GameUpdateApiTest(BaseCase):
    """POST /api/games/<id>/ の検証・権限・保存。"""

    def setUp(self):
        super().setUp()
        self.game = play_game(self.team, self.rival, home_score=1, away_score=0)
        self.url = reverse("api_game_update", args=[self.game.id])

    def _payload(self, **overrides):
        payload = {
            "year": 2026,
            "played_on": "2026-04-01",
            "home_team": self.team.id,
            "away_team": self.rival.id,
            "home_score": 1,
            "away_score": 0,
            "batting": [],
            "pitching": [],
            "innings": api_inning_rows(),
        }
        payload.update(overrides)
        return payload

    def test_anonymous_is_rejected(self):
        response = self.client.post(self.url, data=json.dumps(self._payload()), content_type="application/json")

        self.assertEqual(response.status_code, 403)
        self.assertFalse(response.json()["ok"])
        self.assertIn("ログイン", response.json()["error"])

    def test_manager_of_an_uninvolved_team_is_rejected(self):
        outsider_team = orm_models.Team.objects.create(league=self.league, name="無関係チーム")
        login_as_manager(self.client, outsider_team)

        response = post_game_update(self.client, self.game.id, self._payload())

        self.assertEqual(response.status_code, 403)
        self.assertIn("権限がありません", response.json()["error"])

    def test_missing_game_returns_404(self):
        login_as_manager(self.client, self.team)

        response = post_game_update(self.client, 9999, self._payload())

        self.assertEqual(response.status_code, 404)
        self.assertIn("見つかりません", response.json()["error"])

    def test_non_json_body_is_rejected(self):
        login_as_manager(self.client, self.team)

        response = self.client.post(self.url, data="not json", content_type="application/json")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "リクエストの形式が不正です。")

    def test_innings_with_non_dict_rows_is_rejected(self):
        login_as_manager(self.client, self.team)

        response = post_game_update(self.client, self.game.id, self._payload(innings=[1, 2, 3]))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "リクエストの形式が不正です。")

    def test_form_validation_error_is_returned_in_japanese(self):
        login_as_manager(self.client, self.team)
        payload = self._payload()
        del payload["year"]

        response = post_game_update(self.client, self.game.id, payload)

        self.assertEqual(response.status_code, 400)
        self.assertIn("シーズン", response.json()["error"])

    def test_successful_save_redirects_to_the_game_detail(self):
        login_as_manager(self.client, self.team)

        response = post_game_update(self.client, self.game.id, self._payload(home_score=3))

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["redirect_url"], reverse("game_detail", args=[self.game.id]))

    def test_domain_error_is_returned_in_japanese(self):
        login_as_manager(self.client, self.team)

        response = post_game_update(
            self.client,
            self.game.id,
            self._payload(home_score=9, innings=api_inning_rows(away=[0], home=[1])),
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["ok"])
        self.assertIn("イニングスコアの合計が得点と一致しません", response.json()["error"])

    def test_missing_batting_key_is_rejected_instead_of_clearing_records(self):
        """キーが1つ無いだけのリクエストで、既存の成績が消えてしまわないこと。"""
        login_as_manager(self.client, self.team)
        payload = self._payload()
        del payload["batting"]

        response = post_game_update(self.client, self.game.id, payload)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "リクエストの形式が不正です。")

    def test_out_of_range_inning_is_rejected(self):
        """inning の範囲チェックが効いていること（範囲外で巨大な配列を作らせない）。"""
        login_as_manager(self.client, self.team)
        payload = self._payload(innings=[{"inning": 999, "away": 1, "home": 0}])

        response = post_game_update(self.client, self.game.id, payload)

        self.assertEqual(response.status_code, 400)

    def test_line_score_is_ordered_by_inning_number_not_by_position(self):
        """行の並び順が入れ替わっても、回の取り違えが起きないこと。"""
        login_as_manager(self.client, self.team)
        # 3回目の行を先頭に置く。位置で組み立てると3回目の得点が1回目に入ってしまう
        innings = [
            {"inning": 3, "away": 5, "home": 0},
            {"inning": 1, "away": 0, "home": 0},
            {"inning": 2, "away": 0, "home": 0},
        ]

        payload = self._payload(home_score=0, away_score=5, innings=innings)
        response = post_game_update(self.client, self.game.id, payload)

        self.assertEqual(response.status_code, 200)
        line_score = self.service.get_game_edit_data(self.game.id)["game"].line_score
        self.assertEqual(line_score.away, (0, 0, 5))

    def test_home_only_row_beyond_away_is_dropped(self):
        """表の記録が無い回の裏だけが送られても、裏はその回まで切り落とされること。"""
        login_as_manager(self.client, self.team)
        # 2回表が未記録なのに2回裏だけ値がある不正な形
        innings = [
            {"inning": 1, "away": 1, "home": 1},
            {"inning": 2, "away": None, "home": 2},
        ]

        payload = self._payload(home_score=1, away_score=1, innings=innings)
        response = post_game_update(self.client, self.game.id, payload)

        self.assertEqual(response.status_code, 200)
        line_score = self.service.get_game_edit_data(self.game.id)["game"].line_score
        self.assertEqual(line_score.away, (1,))
        self.assertEqual(line_score.home, (1,))


class GameEditTemplateTest(BaseCase):
    """試合編集画面（GET）の器に React のマウント先・初期データがあること。"""

    def test_page_has_the_react_mount_point_and_initial_data(self):
        game = play_game(self.team, self.rival)
        login_as_manager(self.client, self.team)

        response = self.client.get(reverse("game_edit", args=[game.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="game-edit-root"')
        self.assertContains(response, 'id="game-edit-data"')
        self.assertContains(response, "myapp/dist/game_edit.js")
