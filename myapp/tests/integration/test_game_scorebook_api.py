"""スコアブック（打席の記録）の保存 API。

送るのは試合の基本情報・ラインアップ・打席だけ。得点・イニングスコア・登板順・
勝敗はサーバーが打席から導く。ここではその往復と、成立しない記録を弾くことを見る。
業務ルールそのものは DB を使わない `tests/domain/test_plate_appearances.py` にある。
"""

from myapp.infrastructure import orm_models
from myapp.infrastructure.repositories import DjangoGameRepository

from ..helpers import login_as_manager, play_game, post_game_scorebook
from .base import BaseCase

# 塁の値（ドメインの Base と同じ）。API は数値で受け取る
BATTER, FIRST, SECOND, THIRD, HOME, OUT = 0, 1, 2, 3, 4, -1


class ScorebookApiTest(BaseCase):
    """1回表にビジターが1点、1回裏はホームが3人で終わる試合を送る。"""

    def setUp(self):
        super().setUp()
        login_as_manager(self.client, self.team, self.rival)
        self.game = play_game(self.team, self.rival, home_score=0, away_score=0)
        self.home_pitcher = self._register(self.team, "ホーム先発", 11, "投手")
        self.away_pitcher = self._register(self.rival, "ビジター先発", 12, "投手")
        self.away_batters = [self._register(self.rival, f"ビジター{i}", 20 + i, "内野手") for i in range(1, 6)]
        self.home_batters = [self._register(self.team, f"ホーム{i}", 30 + i, "内野手") for i in range(1, 4)]

    def _register(self, team, name, number, position) -> int:
        return self.service.register_player(team.id, name, number, position).id

    # --- 送る中身 ---

    @staticmethod
    def _advance(runner_id, from_base, to_base, reason="打撃"):
        return {"runner_id": runner_id, "from_base": from_base, "to_base": to_base, "reason": reason}

    def _plate_appearances(self):
        away = self.away_batters
        home = self.home_batters
        return [
            self._pa(1, 1, away[0], "単打", [self._advance(away[0], BATTER, FIRST)]),
            self._pa(
                2,
                2,
                away[1],
                "二塁打",
                [self._advance(away[0], FIRST, THIRD), self._advance(away[1], BATTER, SECOND)],
            ),
            # 犠飛で1点。打者はアウトだが打数には数えない
            self._pa(
                3,
                3,
                away[2],
                "犠飛",
                [
                    self._advance(away[2], BATTER, OUT, "アウト"),
                    self._advance(away[0], THIRD, HOME, "タッチアップ"),
                ],
            ),
            self._pa(4, 4, away[3], "空振り三振", [self._advance(away[3], BATTER, OUT, "アウト")]),
            self._pa(5, 5, away[4], "ゴロアウト", [self._advance(away[4], BATTER, OUT, "アウト")]),
            self._pa(6, 1, home[0], "空振り三振", [self._advance(home[0], BATTER, OUT, "アウト")], bottom=True),
            self._pa(7, 2, home[1], "ゴロアウト", [self._advance(home[1], BATTER, OUT, "アウト")], bottom=True),
            self._pa(8, 3, home[2], "フライアウト", [self._advance(home[2], BATTER, OUT, "アウト")], bottom=True),
        ]

    def _pa(self, sequence, order, batter_id, result, advances, *, bottom=False):
        return {
            "sequence": sequence,
            "inning": 1,
            "is_bottom": bottom,
            "batter_id": batter_id,
            "pitcher_id": self.away_pitcher if bottom else self.home_pitcher,
            "batting_order": order,
            "slot_sequence": 0,
            "result": result,
            "fielded_by": "",
            "advances": advances,
            "errors": [],
        }

    def _lineup(self):
        rows = [
            {
                "team_id": self.rival.id,
                "player_id": player_id,
                "batting_order": order,
                "slot_sequence": 0,
                "fielding_position": "指",
            }
            for order, player_id in enumerate(self.away_batters, start=1)
        ]
        rows += [
            {
                "team_id": self.team.id,
                "player_id": player_id,
                "batting_order": order,
                "slot_sequence": 0,
                "fielding_position": "指",
            }
            for order, player_id in enumerate(self.home_batters, start=1)
        ]
        return rows

    def _payload(self, **overrides):
        payload = {
            "year": 2026,
            "played_on": "2026-04-01",
            "home_team": self.team.id,
            "away_team": self.rival.id,
            "lineup": self._lineup(),
            "plate_appearances": self._plate_appearances(),
        }
        payload.update(overrides)
        return payload

    # --- 往復 ---

    def test_a_scorebook_is_saved_and_the_score_is_derived(self):
        """得点は送らない。打席から導かれること。"""
        response = post_game_scorebook(self.client, self.game.id, self._payload())

        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["ok"])
        saved = DjangoGameRepository().find_by_id(self.game.id)
        self.assertEqual((saved.away_score, saved.home_score), (1, 0))
        self.assertEqual(saved.line_score.away, (1,))
        self.assertEqual(saved.line_score.home, (0,))
        self.assertEqual(len(saved.plate_appearances), 8)

    def test_batting_lines_are_derived_from_the_plate_appearances(self):
        post_game_scorebook(self.client, self.game.id, self._payload())

        rows = {row.player_id: row for row in orm_models.GameBattingLine.objects.filter(game_id=self.game.id)}

        leadoff = rows[self.away_batters[0]]
        self.assertEqual((leadoff.at_bats, leadoff.singles), (1, 1))
        # 犠飛は打数に数えず、打点だけが付く
        sacrifice = rows[self.away_batters[2]]
        self.assertEqual((sacrifice.at_bats, sacrifice.sacrifice_flies, sacrifice.runs_batted_in), (0, 1, 1))

    def test_pitching_lines_and_the_order_are_derived(self):
        """登板順と登板した回も送らない。打席から導かれること。"""
        post_game_scorebook(self.client, self.game.id, self._payload())

        rows = {row.player_id: row for row in orm_models.GamePitchingLine.objects.filter(game_id=self.game.id)}

        self.assertEqual(rows[self.home_pitcher].innings_pitched, 1.0)
        self.assertEqual(rows[self.home_pitcher].strikeouts, 1)
        # チームごとに1から振る（相手の先発が2番手にならないこと）
        self.assertEqual(rows[self.home_pitcher].appearance_order, 1)
        self.assertEqual(rows[self.away_pitcher].appearance_order, 1)

    # --- 弾くもの ---

    def test_a_batting_order_that_skips_is_rejected(self):
        """打順が飛んでいる記録は、スコアブックとして成立しない。"""
        entries = self._plate_appearances()
        entries[1]["batting_order"] = 4

        response = post_game_scorebook(self.client, self.game.id, self._payload(plate_appearances=entries))

        self.assertEqual(response.status_code, 400)
        self.assertIn("打順", response.json()["error"])

    def test_an_advance_that_contradicts_its_reason_is_rejected(self):
        entries = self._plate_appearances()
        entries[0]["advances"] = [self._advance(self.away_batters[0], BATTER, FIRST, "盗塁刺")]

        response = post_game_scorebook(self.client, self.game.id, self._payload(plate_appearances=entries))

        self.assertEqual(response.status_code, 400)

    def test_a_batter_missing_from_the_lineup_is_rejected(self):
        """打席に立ったのにラインアップに無い選手がいると、その成績が消える。"""
        response = post_game_scorebook(self.client, self.game.id, self._payload(lineup=self._lineup()[1:]))

        self.assertEqual(response.status_code, 400)
        self.assertIn("打撃成績", response.json()["error"])

    def test_a_missing_key_is_rejected(self):
        """キーの欠落を空リストと同じに扱わない（既存の記録が全消去されるため）。"""
        payload = self._payload()
        del payload["plate_appearances"]

        response = post_game_scorebook(self.client, self.game.id, payload)

        self.assertEqual(response.status_code, 400)

    def test_an_unknown_result_is_rejected(self):
        entries = self._plate_appearances()
        entries[0]["result"] = "サイクルヒット"

        response = post_game_scorebook(self.client, self.game.id, self._payload(plate_appearances=entries))

        self.assertEqual(response.status_code, 400)

    def test_someone_without_permission_cannot_save(self):
        self.client.logout()
        login_as_manager(self.client, username="stranger")

        response = post_game_scorebook(self.client, self.game.id, self._payload())

        self.assertEqual(response.status_code, 403)

    def test_anonymous_cannot_save(self):
        self.client.logout()

        response = post_game_scorebook(self.client, self.game.id, self._payload())

        self.assertEqual(response.status_code, 403)
