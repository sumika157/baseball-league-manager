"""試合編集画面（React）のE2Eテスト。

打順を決めて打席を1つ記録し、保存するまでの導線を実ブラウザで確認する。
入力検証や保存の分岐は integration 側（test_game_scorebook_api.py）で検証済みなので、
ここでは JS が絡む部分（結果を選ぶと進塁が既定値で埋まること・保存からの画面遷移）に絞る。
"""

from pathlib import Path

from django.contrib.auth.models import User
from django.urls import reverse

from myapp.infrastructure import orm_models

from ..helpers import build_roster_service, make_league, make_stadium, play_game, real_scope
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
        league = make_league(name="Eリーグ")
        self.team = orm_models.Team.objects.create(league=league, name="ホームズ")
        self.rival = orm_models.Team.objects.create(league=league, name="ビジターズ")

        roster = build_roster_service(real_scope())
        self.batter = roster.register_player(self.rival.id, "山田", 3, "内野手")
        self.home_batter = roster.register_player(self.team.id, "鈴木", 5, "内野手")
        self.pitcher = roster.register_player(self.team.id, "佐藤", 18, "投手")

        self.game = play_game(self.team, self.rival, home_score=0, away_score=0)

        user = User.objects.create_user(username="manager", password="pass12345")
        self.team.managers.add(user)

    def _login(self):
        self.page.goto(self.live_server_url + "/accounts/login/")
        self.page.fill('input[name="username"]', "manager")
        self.page.fill('input[name="password"]', "pass12345")
        self.page.locator('input[name="password"]').press("Enter")
        self.page.wait_for_url(f"{self.live_server_url}/")

    def test_record_a_plate_appearance_and_save(self):
        self._login()
        self.page.goto(self.live_server_url + reverse("game_edit", args=[self.game.id]))

        # 打順を決める。1回表はビジターの攻撃なので、そちらの1番が最初の打者になる
        self.page.get_by_label("ビジターズ 1番の選手", exact=True).select_option(str(self.batter.id))
        self.page.get_by_label("ビジターズ 1番の守備位置", exact=True).select_option("指")
        self.page.get_by_label("ホームズ 1番の選手", exact=True).select_option(str(self.home_batter.id))
        self.page.get_by_label("ホームズ 1番の守備位置", exact=True).select_option("指")

        self.page.get_by_role("button", name="次の打席を記録する").click()

        # 結果を選ぶと進塁が既定値で埋まる。本塁打なら打者は本塁まで進む
        self.page.get_by_label("結果", exact=True).select_option("本塁打")
        self.page.get_by_label("投手", exact=True).select_option(str(self.pitcher.id))
        self.assertEqual(self.page.get_by_label("山田の進塁後", exact=True).input_value(), "4")

        self.page.get_by_role("button", name="この打席を記録する").click()

        # 得点も打席から導かれる（入力欄は無い）
        self.assertIn("ビジターズ 1 - 0 ホームズ", self.page.get_by_label("導出した得点").inner_text())

        self.page.get_by_role("button", name="保存する").click()
        self.page.wait_for_url(f"{self.live_server_url}{reverse('game_detail', args=[self.game.id])}")

        # 保存の中身はドメイン経由で永続化されている（成績はすべて打席から導出）
        saved = orm_models.Game.objects.get(id=self.game.id)
        self.assertEqual((saved.away_score, saved.home_score), (1, 0))
        self.assertEqual(orm_models.GamePlateAppearance.objects.filter(game_id=self.game.id).count(), 1)
        batting = orm_models.GameBattingLine.objects.get(game_id=self.game.id, player_id=self.batter.id)
        self.assertEqual((batting.home_runs, batting.at_bats, batting.runs_batted_in), (1, 1, 1))

    def _record_a_home_run(self):
        """打順を決めて本塁打を1つ記録する（保存はしない）。"""
        self._login()
        self.page.goto(self.live_server_url + reverse("game_edit", args=[self.game.id]))
        self.page.get_by_label("ビジターズ 1番の選手", exact=True).select_option(str(self.batter.id))
        self.page.get_by_label("ビジターズ 1番の守備位置", exact=True).select_option("指")
        self.page.get_by_label("ホームズ 1番の選手", exact=True).select_option(str(self.home_batter.id))
        self.page.get_by_label("ホームズ 1番の守備位置", exact=True).select_option("指")
        self.page.get_by_role("button", name="次の打席を記録する").click()
        self.page.get_by_label("結果", exact=True).select_option("本塁打")
        self.page.get_by_label("投手", exact=True).select_option(str(self.pitcher.id))
        self.page.get_by_role("button", name="この打席を記録する").click()

    def test_times_and_attendance_are_saved_and_shown_on_the_detail(self):
        self._record_a_home_run()

        self.page.get_by_label("開始時刻").fill("18:19")
        self.page.get_by_label("終了時刻").fill("21:09")
        self.page.get_by_label("観客数").fill("42071")
        self.page.get_by_role("button", name="保存する").click()
        self.page.wait_for_url(f"{self.live_server_url}{reverse('game_detail', args=[self.game.id])}")

        items = self.page.get_by_test_id("game-day-items").inner_text()
        self.assertEqual(items, "開始 18:19 ◇ 終了 21:09 ◇ 試合時間 2時間50分 ◇ 観客 42,071人")

    def test_an_attendance_over_the_capacity_warns_and_stays_on_the_page(self):
        stadium = make_stadium(name="小さな球場", capacity=1000)
        orm_models.Team.objects.filter(id=self.team.id).update(home_stadium=stadium)
        self._record_a_home_run()

        self.page.get_by_label("観客数").fill("5000")
        self.page.get_by_role("button", name="保存する").click()

        warnings = self.page.get_by_test_id("save-warnings")
        warnings.wait_for()
        self.assertIn("保存しました。", warnings.inner_text())
        self.assertIn("5,000人", warnings.inner_text())
        self.assertIn("収容人数（1,000人）", warnings.inner_text())
        self.assertIn(reverse("game_edit", args=[self.game.id]), self.page.url)
        # 保存は済んでいる
        self.assertEqual(orm_models.Game.objects.get(id=self.game.id).attendance, 5000)
