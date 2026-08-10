"""試合編集画面（React）のE2Eテスト。

イニングスコア・打撃・投球を入力して保存する主要導線を実ブラウザで確認する。
入力検証や保存の分岐は integration 側（test_game_edit_api.py）で検証済みなので、
ここでは JS が絡む部分（スコアの自動導出・保存からの画面遷移）に絞る。
"""

from pathlib import Path

from django.contrib.auth.models import User
from django.urls import reverse

from myapp.infrastructure import orm_models

from ..helpers import build_service, play_game
from .base import PlaywrightTestCase

# React のビルド成果物。無いと画面が「読み込んでいます」のまま失敗し原因が
# 分かりにくいため、テストの前提として先に検査する
DIST_ENTRY = Path(__file__).resolve().parents[3] / "myapp" / "static" / "myapp" / "dist" / "game_edit.js"


class GameEditReactTest(PlaywrightTestCase):
    @classmethod
    def setUpClass(cls):
        if not DIST_ENTRY.exists():
            raise AssertionError(
                "React のビルド成果物（myapp/static/myapp/dist/game_edit.js）がありません。"
                "`make frontend-build` を実行してから E2E テストを実行してください。"
            )
        super().setUpClass()

    def setUp(self):
        super().setUp()
        league = orm_models.League.objects.create(name="Eリーグ")
        self.team = orm_models.Team.objects.create(league=league, name="ホームズ")
        self.rival = orm_models.Team.objects.create(league=league, name="ビジターズ")

        service = build_service()
        self.batter = service.register_player(self.team.id, "山田", 3, "内野手")
        self.pitcher = service.register_player(self.team.id, "佐藤", 18, "投手")
        self.opposing_pitcher = service.register_player(self.rival.id, "田中", 21, "投手")

        self.game = play_game(self.team, self.rival, home_score=0, away_score=0)

        user = User.objects.create_user(username="manager", password="pass12345")
        self.team.managers.add(user)

    def _login(self):
        self.page.goto(self.live_server_url + "/accounts/login/")
        self.page.fill('input[name="username"]', "manager")
        self.page.fill('input[name="password"]', "pass12345")
        self.page.locator('input[name="password"]').press("Enter")
        self.page.wait_for_url(f"{self.live_server_url}/")

    def test_enter_and_save_a_full_box_score(self):
        self._login()
        self.page.goto(self.live_server_url + reverse("game_edit", args=[self.game.id]))

        # ビジターは9回すべて0点、ホームは初回に2点（リードして9回裏なし）。
        # get_by_label は部分一致が既定で「1回表」が「11回表」にも当たるため exact 指定
        for inning in range(1, 10):
            self.page.get_by_label(f"{inning}回表", exact=True).fill("0")
        self.page.get_by_label("1回裏", exact=True).fill("2")
        for inning in range(2, 9):
            self.page.get_by_label(f"{inning}回裏", exact=True).fill("0")

        # 得点はイニングスコアから導出され、手入力できなくなる
        home_score = self.page.get_by_label("ホーム得点", exact=True)
        self.assertEqual(home_score.input_value(), "2")
        self.assertTrue(home_score.get_attribute("readonly") is not None)
        self.assertEqual(self.page.get_by_label("ビジター得点", exact=True).input_value(), "0")

        # 打撃: 山田が4番・一塁で2安打2打点
        self.page.get_by_label("山田 打順", exact=True).fill("4")
        self.page.get_by_label("山田 守備位置", exact=True).select_option("一")
        self.page.get_by_label("山田 打数", exact=True).fill("4")
        self.page.get_by_label("山田 単打", exact=True).fill("2")
        self.page.get_by_label("山田 打点", exact=True).fill("2")

        # 投球: 佐藤が完投勝利、田中が完投で敗戦
        self.page.get_by_label("佐藤 登板", exact=True).fill("1")
        self.page.get_by_label("佐藤 投球回", exact=True).fill("9.0")
        self.page.get_by_label("佐藤 奪三振", exact=True).fill("8")
        self.page.get_by_label("田中 登板", exact=True).fill("1")
        self.page.get_by_label("田中 投球回", exact=True).fill("8.0")
        self.page.get_by_label("田中 自責点", exact=True).fill("2")

        self.page.get_by_role("button", name="保存する").click()
        self.page.wait_for_url(f"{self.live_server_url}{reverse('game_detail', args=[self.game.id])}")

        # 保存の中身はドメイン経由で永続化されている（勝敗はイニングスコアから導出）
        line = orm_models.GamePitchingLine.objects.get(game_id=self.game.id, player_id=self.pitcher.id)
        self.assertEqual(line.wins, 1)
        loser = orm_models.GamePitchingLine.objects.get(game_id=self.game.id, player_id=self.opposing_pitcher.id)
        self.assertEqual(loser.losses, 1)
        batting = orm_models.GameBattingLine.objects.get(game_id=self.game.id, player_id=self.batter.id)
        self.assertEqual(batting.singles, 2)
        self.assertEqual(batting.batting_order, 4)
