"""試合の編集画面（React）に渡す初期データの形を固定するテスト。

編集画面の材料を application の DTO に寄せたとき、画面の HTML と payload が
変わっていないことを確かめるための特性テスト。期待値は `golden/game_edit_payload.json`
（#49 の変更前の実装が返した payload。DB の id は選手名・チーム名の記号に置き換えてある）。

代打・守備固め・救援投手を含む試合を使う。出場時点を入力欄に出すのは守備固めだけで、
代打・投手は打席から導くので出さない、という判断（`entry_is_derived`）が崩れると落ちる。
"""

import json
from datetime import date
from pathlib import Path

from django.urls import reverse

from myapp.application.dto import LineupSlot
from myapp.domain.entities import PlateAppearance, RunnerAdvance
from myapp.domain.value_objects import AdvanceReason, Base, FieldingPosition, JerseyNumber, PlateAppearanceResult

from ..helpers import login_as_manager
from .test_fielding import FieldingTestBase

GOLDEN = Path(__file__).parent / "golden" / "game_edit_payload.json"
P = PlateAppearanceResult
R = AdvanceReason
FP = FieldingPosition


class GameEditPayloadTest(FieldingTestBase):
    """打席・代打・守備固め・救援投手を含む試合の payload。"""

    def setUp(self):
        super().setUp()
        self.pinch = self._player(self.team, "ホーム代打", 77, "内野手")
        self.defender = self._player(self.team, "ホーム守備固め", 78, "捕手")
        self.relief = self._player(self.team, "ホーム救援", 79, "投手")
        login_as_manager(self.client, self.team)

    def _record_substitutions(self) -> None:
        slots = [
            *self._lineup(),
            LineupSlot(self.team.id, self.pinch, 8, 1, FP.PINCH_HITTER),
            # 4回表の2人目の打者から捕手を守備固めに替える
            LineupSlot(self.team.id, self.defender, 8, 2, FP.CATCHER, 4, False, 2),
            LineupSlot(self.team.id, self.relief, 9, 1, FP.PITCHER),
        ]
        entries = self._plate_appearances()
        # 3回裏の8番は代打
        target = next(e for e in entries if e.is_bottom and e.inning == 3 and e.batting_order == 8)
        target.batter_id, target.slot_sequence = self.pinch, 1
        target.advances = [RunnerAdvance(self.pinch, Base.BATTER, Base.OUT, R.PUT_OUT)]
        # 4回表（ビジター）の三者三振から救援が投げる
        last = len(entries)
        extra: list[PlateAppearance] = [
            self._pa(last + i + 1, 4, False, order, P.STRIKEOUT_SWINGING) for i, order in enumerate((2, 3, 4))
        ]
        for entry in extra:
            entry.pitcher_id = self.relief
        self.recording.record_scorebook(
            self.game.id,
            year=2026,
            played_on=date(2026, 4, 1),
            home_team_id=self.team.id,
            away_team_id=self.rival.id,
            lineup=slots,
            plate_appearances=[*entries, *extra],
        )

    def _snapshot(self, payload: dict) -> dict:
        """DB の id を名前の記号に置き換え、毎回変わる値を落とす（語彙は定数なので対象外）。"""
        # チームと選手は別の連番なので、記号の対応表も分ける（同じ数値が両方にありうる）
        team_names = {self.team.id: "HOME_TEAM", self.rival.id: "AWAY_TEAM"}
        player_names = {player["id"]: player["name"] for team in payload["teams"] for player in team["players"]}
        label = player_names.__getitem__

        snapshot = json.loads(json.dumps(payload))
        snapshot.pop("vocabulary")
        snapshot.pop("csrf_token")
        game = snapshot["game"]
        snapshot["urls"] = {key: url.replace(f"/{game['id']}/", "/GAME/") for key, url in snapshot["urls"].items()}
        game["id"] = "GAME"
        game["home_team"], game["away_team"] = team_names[game["home_team"]], team_names[game["away_team"]]
        for team in snapshot["teams"]:
            team["team_id"] = team_names[team["team_id"]]
            for player in team["players"]:
                player["id"] = label(player["id"])
            for slot in team["lineup"]:
                slot["player_id"] = label(slot["player_id"])
        for entry in snapshot["plate_appearances"]:
            entry["batter_id"], entry["pitcher_id"] = label(entry["batter_id"]), label(entry["pitcher_id"])
            for advance in entry["advances"]:
                advance["runner_id"] = label(advance["runner_id"])
            for error in entry["errors"]:
                error["player_id"] = label(error["player_id"])
        return snapshot

    def _get_payload(self) -> dict:
        response = self.client.get(reverse("game_edit", args=[self.game.id]))
        self.assertEqual(response.status_code, 200)
        return response.context["payload"]

    def test_the_payload_matches_the_golden_file(self):
        self._record_substitutions()

        snapshot = self._snapshot(self._get_payload())

        self.assertEqual(snapshot, json.loads(GOLDEN.read_text(encoding="utf-8")))

    def test_only_the_defensive_replacement_gets_an_entry_point(self):
        """守備固めは入った半回と打者番号を返す。代打・投手は打席から導くので返さない。"""
        self._record_substitutions()
        payload = self._get_payload()
        rows = {slot["player_id"]: slot for team in payload["teams"] for slot in team["lineup"]}

        defender, pinch, relief = rows[self.defender], rows[self.pinch], rows[self.relief]
        self.assertEqual(
            (defender["entered_inning"], defender["entered_is_bottom"], defender["entered_batter"]), (4, False, 2)
        )
        for derived in (pinch, relief):
            self.assertEqual(
                (derived["entered_inning"], derived["entered_is_bottom"], derived["entered_batter"]), (None, False, 1)
            )
        self.assertEqual((pinch["fielding_position"], relief["fielding_position"]), ("打", "投"))

    def test_an_unrecorded_game_has_no_lineup_and_no_plate_appearances(self):
        payload = self._get_payload()

        self.assertEqual(payload["plate_appearances"], [])
        self.assertEqual([team["lineup"] for team in payload["teams"]], [[], []])
        self.assertEqual((payload["game"]["home_score"], payload["game"]["away_score"]), (0, 0))
        # 両チームとも在籍中の選手は背番号順に並ぶ。背番号は表記の文字列なので、文字列の順ではなく
        # ドメインの並び（00 → 0 → 1 → … → 10）で比べる（文字列の順だと「10」が「2」より前に来る）
        for team in payload["teams"]:
            numbers = [player["number"] for player in team["players"]]
            self.assertEqual(numbers, sorted(numbers, key=lambda n: JerseyNumber(n).sort_key))
        self.assertEqual([team["is_home"] for team in payload["teams"]], [True, False])
