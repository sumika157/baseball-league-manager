"""スコアブック（打席の記録）の保存 API。

送るのは試合の基本情報・ラインアップ・打席だけ。得点・イニングスコア・登板順・
勝敗はサーバーが打席から導く。ここではその往復と、成立しない記録を弾くことを見る。
業務ルールそのものは DB を使わない `tests/domain/test_plate_appearances.py` にある。
"""

from myapp.domain.pennant.world import WorldScope
from myapp.domain.value_objects import BattingLine, InningsPitched, PitchingLine
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
        self.game = play_game(self.team, self.rival, home_score=0, away_score=0, recorded=False)
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
        saved = DjangoGameRepository(WorldScope.real()).find_by_id(self.game.id)
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

    def test_a_run_on_the_play_that_makes_the_third_out_by_the_batter_is_rejected(self):
        """2アウトのゴロで三塁走者が還った記録は、得点にならない（規則 5.08）。"""
        away = self.away_batters
        entries = self._plate_appearances()
        entries[2] = self._pa(3, 3, away[2], "空振り三振", [self._advance(away[2], BATTER, OUT, "アウト")])
        entries[3] = self._pa(4, 4, away[3], "空振り三振", [self._advance(away[3], BATTER, OUT, "アウト")])
        entries[4] = self._pa(
            5,
            5,
            away[4],
            "ゴロアウト",
            [self._advance(away[4], BATTER, OUT, "アウト"), self._advance(away[0], THIRD, HOME)],
        )

        response = post_game_scorebook(self.client, self.game.id, self._payload(plate_appearances=entries))

        self.assertEqual(response.status_code, 400)
        self.assertIn("5.08", response.json()["error"])
        self.assertEqual(len(DjangoGameRepository().find_by_id(self.game.id).plate_appearances), 0)

    def test_someone_without_permission_cannot_save(self):
        self.client.logout()
        login_as_manager(self.client, username="stranger")

        response = post_game_scorebook(self.client, self.game.id, self._payload())

        self.assertEqual(response.status_code, 403)

    def test_anonymous_cannot_save(self):
        self.client.logout()

        response = post_game_scorebook(self.client, self.game.id, self._payload())

        self.assertEqual(response.status_code, 403)

    def _payload_with_substitute(self, **entry):
        substitute = self._register(self.rival, "ビジター代打", 99, "内野手")
        row = {
            "team_id": self.rival.id,
            "player_id": substitute,
            "batting_order": 2,
            "slot_sequence": 1,
            "fielding_position": "指",
            **entry,
        }
        return self._payload(lineup=[*self._lineup(), row])

    def test_a_substitute_row_without_the_entry_keys_is_rejected(self):
        response = post_game_scorebook(self.client, self.game.id, self._payload_with_substitute())

        self.assertEqual(response.status_code, 400)
        self.assertIn("キーがありません", response.json()["error"])

    def test_a_single_missing_key_is_rejected_too(self):
        payload = self._payload_with_substitute(entered_inning=None, entered_is_bottom=False)

        response = post_game_scorebook(self.client, self.game.id, payload)

        self.assertEqual(response.status_code, 400)
        self.assertIn("entered_batter", response.json()["error"])

    def test_explicit_nulls_are_accepted(self):
        payload = self._payload_with_substitute(entered_inning=None, entered_is_bottom=False, entered_batter=None)

        response = post_game_scorebook(self.client, self.game.id, payload)

        self.assertEqual(response.status_code, 200, response.content)

    def test_starters_do_not_need_the_keys(self):
        response = post_game_scorebook(self.client, self.game.id, self._payload())

        self.assertEqual(response.status_code, 200, response.content)

    # --- 記録のある試合を空の打席で上書きしない ---

    def test_a_game_with_plate_appearances_is_not_wiped_by_an_empty_scorebook(self):
        """不具合のあるクライアントが空の配列を送っても、記録が黙って全消去されない。"""
        post_game_scorebook(self.client, self.game.id, self._payload())

        response = post_game_scorebook(self.client, self.game.id, self._payload(lineup=[], plate_appearances=[]))

        self.assertEqual(response.status_code, 400)
        self.assertIn("打席がすべて取り除かれています", response.json()["error"])
        saved = DjangoGameRepository(WorldScope.real()).find_by_id(self.game.id)
        self.assertEqual(len(saved.plate_appearances), 8)
        self.assertEqual((saved.away_score, saved.home_score), (1, 0))
        self.assertTrue(orm_models.GameBattingLine.objects.filter(game_id=self.game.id).exists())

    def test_a_legacy_game_is_not_overwritten_by_an_empty_scorebook(self):
        """打席を記録する前の試合（明細だけがある）を空の打席で保存すると、成績が消える。"""
        """打席を記録する前の試合（明細だけがある）を空の打席で保存すると、成績が消える。"""
        batter = self.away_batters[0]
        legacy = play_game(
            self.team,
            self.rival,
            home_score=2,
            away_score=3,
            day=2,
            batting={batter: BattingLine(at_bats=4, singles=2)},
            pitching={self.home_pitcher: PitchingLine(innings=InningsPitched.from_notation("9.0"), earned_runs=3)},
        )

        response = post_game_scorebook(
            self.client, legacy.id, self._payload(lineup=[], plate_appearances=[], played_on="2026-04-02")
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("古い形式", response.json()["error"])
        saved = DjangoGameRepository(WorldScope.real()).find_by_id(legacy.id)
        self.assertEqual((saved.home_score, saved.away_score), (2, 3))
        self.assertEqual(saved.batting[0].line.at_bats, 4)
        self.assertEqual(len(saved.pitching), 1)

    def test_a_game_with_only_a_score_is_unrecorded_and_can_be_saved_empty(self):
        """得点だけの試合は未記録（集計に数えない）なので、空で保存するのを弾かない。

        保護と集計で「記録済み」の判定が食い違わないよう、どちらも `Game.is_recorded` を使う。
        """
        score_only = play_game(self.team, self.rival, home_score=4, away_score=2, day=3, recorded=False)
        self.assertFalse(DjangoGameRepository(WorldScope.real()).find_by_id(score_only.id).is_recorded)

        response = post_game_scorebook(
            self.client, score_only.id, self._payload(lineup=[], plate_appearances=[], played_on="2026-04-03")
        )

        self.assertEqual(response.status_code, 200, response.content)

    def test_a_new_game_without_lines_can_be_saved_empty(self):
        """打席も明細も無い試合を空で保存するのは従来どおり通る。"""
        response = post_game_scorebook(self.client, self.game.id, self._payload(lineup=[], plate_appearances=[]))

        self.assertEqual(response.status_code, 200, response.content)
