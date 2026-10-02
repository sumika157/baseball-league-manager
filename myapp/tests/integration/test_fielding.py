"""守備成績（打席から導いて保存・表示する）の結合テスト。

導出の規則そのものは DB を使わない `tests/domain/test_fielding.py` にある。
ここで見るのは、保存と読み直し・保存前の照合・既存試合への後付け・画面に出ることだけ。
"""

import re
from datetime import date
from importlib import import_module
from io import StringIO

from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from myapp.application.dto import LineupSlot
from myapp.domain.entities import FieldingError, PlateAppearance, RunnerAdvance
from myapp.domain.exceptions import InvalidGame, InvalidPlateAppearance
from myapp.domain.value_objects import (
    AdvanceReason,
    Base,
    ErrorKind,
    FieldingLine,
    FieldingPosition,
    PlateAppearanceResult,
)
from myapp.infrastructure import orm_models
from myapp.infrastructure.repositories import DjangoGameRepository

from ..helpers import build_recording_service, login_as_manager, play_game
from .base import BaseCase

P = PlateAppearanceResult
R = AdvanceReason
FP = FieldingPosition

# 打順 → 守備位置。指名打者制で、投手は打順にいない
POSITIONS = [FP.SHORTSTOP, FP.SECOND_BASE, FP.FIRST_BASE, FP.THIRD_BASE, FP.LEFT_FIELD, FP.CENTER_FIELD]
POSITIONS += [FP.RIGHT_FIELD, FP.CATCHER, FP.DESIGNATED_HITTER]


def _move(runner_id, frm, to, reason) -> RunnerAdvance:
    return RunnerAdvance(runner_id=runner_id, from_base=frm, to_base=to, reason=reason)


class FieldingTestBase(BaseCase):
    """3回まで、ビジターが4人ずつ・ホームは三者三振で終える試合。

    ホームの守備に付くもの（表の間にホームが守る）:

    - 1回: 遊ゴロ 6-3、三振（捕）、中飛
    - 2回: 単打の後に 6-4-3 の併殺、三ゴロ 5-3
    - 3回: 二塁手の失策で出塁、その後は三者連続三振

    ビジターの守備は、ホームの三振9つがすべて捕手に付く。
    """

    def setUp(self):
        super().setUp()
        self.home_pitcher = self._player(self.team, "ホーム先発", 11, "投手")
        self.away_pitcher = self._player(self.rival, "ビジター先発", 12, "投手")
        self.home = [self._player(self.team, f"ホーム{i}", 30 + i, "内野手") for i in range(1, 10)]
        self.away = [self._player(self.rival, f"ビジター{i}", 50 + i, "内野手") for i in range(1, 10)]
        self.recording = build_recording_service()
        self.game = play_game(self.team, self.rival, home_score=0, away_score=0)

    def _player(self, team, name, number, position) -> int:
        return self.service.register_player(team.id, name, number, position).id

    def _lineup(self) -> list[LineupSlot]:
        return [
            LineupSlot(team.id, player_id, order, 0, POSITIONS[order - 1])
            for team, ids in ((self.team, self.home), (self.rival, self.away))
            for order, player_id in enumerate(ids, start=1)
        ]

    def _pa(self, sequence, inning, bottom, order, result, *, fielded_by=(), advances=None, errors=()):
        batter = (self.home if bottom else self.away)[order - 1]
        if advances is None:
            advances = [_move(batter, Base.BATTER, result.default_batter_base, result.default_batter_reason)]
        return PlateAppearance(
            sequence=sequence,
            inning=inning,
            is_bottom=bottom,
            batter_id=batter,
            pitcher_id=self.away_pitcher if bottom else self.home_pitcher,
            batting_order=order,
            result=result,
            fielded_by=tuple(fielded_by),
            advances=advances,
            errors=list(errors),
        )

    def _plate_appearances(self) -> list[PlateAppearance]:
        a = self.away
        entries = [
            # 1回表
            self._pa(1, 1, False, 1, P.GROUND_OUT, fielded_by=(FP.SHORTSTOP, FP.FIRST_BASE)),
            self._pa(2, 1, False, 2, P.STRIKEOUT_SWINGING),
            self._pa(3, 1, False, 3, P.FLY_OUT, fielded_by=(FP.CENTER_FIELD,)),
            # 2回表: 単打の後に 6-4-3、その後に 5-3
            self._pa(7, 2, False, 4, P.SINGLE),
            self._pa(
                8,
                2,
                False,
                5,
                P.GROUND_OUT,
                fielded_by=(FP.SHORTSTOP, FP.SECOND_BASE, FP.FIRST_BASE),
                advances=[
                    _move(a[4], Base.BATTER, Base.OUT, R.PUT_OUT),
                    _move(a[3], Base.FIRST, Base.OUT, R.FORCE_OUT),
                ],
            ),
            self._pa(9, 2, False, 6, P.GROUND_OUT, fielded_by=(FP.THIRD_BASE, FP.FIRST_BASE)),
            # 3回表: 二塁手の失策で出塁、その後は三者連続三振
            self._pa(
                13,
                3,
                False,
                7,
                P.REACHED_ON_ERROR,
                advances=[_move(a[6], Base.BATTER, Base.FIRST, R.ERROR)],
                errors=[FieldingError(self.home[1], FP.SECOND_BASE, ErrorKind.THROWING)],
            ),
            self._pa(14, 3, False, 8, P.STRIKEOUT_LOOKING),
            self._pa(15, 3, False, 9, P.STRIKEOUT_SWINGING),
            self._pa(16, 3, False, 1, P.STRIKEOUT_SWINGING),
        ]
        # 各回の裏は三者三振
        bottoms = [(4, 1, 1), (10, 2, 4), (17, 3, 7)]
        for first_sequence, inning, first_order in bottoms:
            entries.extend(
                self._pa(first_sequence + offset, inning, True, first_order + offset, P.STRIKEOUT_SWINGING)
                for offset in range(3)
            )
        return sorted(entries, key=lambda entry: entry.sequence)

    def record(self, game_id=None, *, year=2026, plate_appearances=None):
        return self.recording.record_scorebook(
            game_id or self.game.id,
            year=year,
            played_on=date(year, 4, 1),
            home_team_id=self.team.id,
            away_team_id=self.rival.id,
            lineup=self._lineup(),
            plate_appearances=plate_appearances or self._plate_appearances(),
        )

    def rows(self, game_id=None) -> dict[int, orm_models.GameFieldingLine]:
        return {
            row.player_id: row for row in orm_models.GameFieldingLine.objects.filter(game_id=game_id or self.game.id)
        }


def _counts(row) -> tuple[int, int, int, int]:
    return (row.putouts, row.assists, row.errors, row.double_plays_turned)


class FieldingPersistenceTest(FieldingTestBase):
    def test_every_fielder_has_a_row_derived_from_the_plate_appearances(self):
        self.record()
        rows = self.rows()
        home = self.home

        # (刺殺, 補殺, 失策, 併殺参加)
        self.assertEqual(_counts(rows[home[0]]), (0, 2, 0, 1), "遊撃: 6-3 と 6-4-3 の補殺")
        self.assertEqual(_counts(rows[home[1]]), (1, 1, 1, 1), "二塁: 併殺の中継と失策")
        self.assertEqual(_counts(rows[home[2]]), (3, 0, 0, 1), "一塁: 3つの刺殺")
        self.assertEqual(_counts(rows[home[3]]), (0, 1, 0, 0), "三塁: 5-3 の補殺")
        self.assertEqual(_counts(rows[home[5]]), (1, 0, 0, 0), "中堅: 中飛")
        self.assertEqual(_counts(rows[home[7]]), (4, 0, 0, 0), "捕手: 三振4つ")

    def test_a_fielder_without_chances_still_has_a_row(self):
        self.record()
        rows = self.rows()

        self.assertEqual(_counts(rows[self.home[4]]), (0, 0, 0, 0))
        self.assertEqual(_counts(rows[self.home_pitcher]), (0, 0, 0, 0))

    def test_the_designated_hitter_has_no_row(self):
        self.record()

        self.assertNotIn(self.home[8], self.rows())

    def test_the_visiting_team_is_derived_too(self):
        self.record()
        rows = self.rows()

        # ホームの三振9つは、すべてビジターの捕手の刺殺
        self.assertEqual(_counts(rows[self.away[7]]), (9, 0, 0, 0))
        self.assertIn(self.away_pitcher, rows)

    def test_the_aggregate_round_trips(self):
        self.record()
        game = DjangoGameRepository().find_by_id(self.game.id)

        by_player = {entry.player_id: entry.line for entry in game.fielding}
        self.assertEqual(by_player[self.home[0]], FieldingLine(assists=2, double_plays_turned=1))
        self.assertEqual(by_player[self.home[1]], FieldingLine(putouts=1, assists=1, errors=1, double_plays_turned=1))
        self.assertTrue(all(entry.id is not None for entry in game.fielding))

    def test_saving_again_does_not_duplicate_rows(self):
        self.record()
        repo = DjangoGameRepository()
        repo.save(repo.find_by_id(self.game.id))
        repo.save(repo.find_by_id(self.game.id))

        self.assertEqual(orm_models.GameFieldingLine.objects.filter(game_id=self.game.id).count(), len(self.rows()))

    def test_a_game_read_without_plate_appearances_keeps_its_fielding_when_saved(self):
        """一覧のために省いて読んだ集約を保存しても、守備成績が消えない（打席と同じ罠）。"""
        self.record()
        before = _snapshot(self.rows())
        repo = DjangoGameRepository()
        game = next(g for g in repo.find_all() if g.id == self.game.id)
        self.assertEqual(game.fielding, [])
        self.assertFalse(game.plate_appearances_loaded)

        repo.save(game)

        self.assertEqual(_snapshot(self.rows()), before)

    def test_rerecording_derives_the_rows_again(self):
        """記録を直すと守備成績も導き直される。"""
        self.record()
        self.assertIn(self.home[2], self.rows())
        # 一塁への送球を全部やめる（6-3 の経路を外す）
        entries = self._plate_appearances()
        entries[0].fielded_by = ()
        self.record(plate_appearances=entries)

        rows = self.rows()
        self.assertEqual(_counts(rows[self.home[0]]), (0, 1, 0, 1), "6-3 の補殺が消える")
        self.assertEqual(_counts(rows[self.home[2]]), (2, 0, 0, 1), "一塁の刺殺が1つ減る")

    def test_reads_of_many_games_do_not_load_fielding(self):
        """まとめて読む経路に守備成績を付けない（選手ページ・一覧の読み込みを広げない）。"""
        self.record()
        repo = DjangoGameRepository()
        with CaptureQueriesContext(connection) as queries:
            repo.find_by_team(self.team.id)
            repo.find_all()
        self.assertFalse([q for q in queries.captured_queries if "gamefieldingline" in q["sql"].lower()])


def _snapshot(rows) -> dict:
    return {player_id: _counts(row) for player_id, row in rows.items()}


class FieldingConsistencyTest(FieldingTestBase):
    """明細と打席が食い違う集約は、保存前に弾かれる。"""

    def test_a_rewritten_fielding_line_is_rejected_and_nothing_is_saved(self):
        self.record()
        before = _snapshot(self.rows())
        repo = DjangoGameRepository()
        game = repo.find_by_id(self.game.id)
        game.record_fielding(self.home[0], FieldingLine(putouts=99))

        with self.assertRaisesRegex(InvalidPlateAppearance, "守備成績が打席の記録と一致しません"):
            repo.save(game)

        self.assertEqual(_snapshot(self.rows()), before)

    def test_changing_the_plate_appearances_without_the_fielding_is_rejected(self):
        """打席だけを書き換えた集約は、古い守備成績のまま保存できない。"""
        self.record()
        repo = DjangoGameRepository()
        game = repo.find_by_id(self.game.id)
        game.plate_appearances[1].fielded_by = (FP.CATCHER, FP.FIRST_BASE)

        with self.assertRaises(InvalidPlateAppearance):
            repo.save(game)


class FieldingRebuildTest(FieldingTestBase):
    """既存の試合には、管理コマンドが打席から守備成績を作る（migrate の後に明示的に流す）。"""

    def _run(self):
        out, err = StringIO(), StringIO()
        call_command("rebuild_fielding_lines", stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def test_rows_are_created_from_the_plate_appearances(self):
        self.record()
        before = _snapshot(self.rows())
        orm_models.GameFieldingLine.objects.all().delete()

        self._run()

        self.assertEqual(_snapshot(self.rows()), before)

    def test_running_it_twice_gives_the_same_rows(self):
        self.record()
        before = _snapshot(self.rows())

        self._run()
        self._run()

        self.assertEqual(_snapshot(self.rows()), before)

    def test_a_game_without_plate_appearances_gets_no_rows(self):
        self._run()

        self.assertEqual(orm_models.GameFieldingLine.objects.filter(game_id=self.game.id).count(), 0)

    def test_a_game_that_cannot_be_built_is_skipped_and_reported(self):
        """記録が成立していない試合で止まらず、飛ばした件数と試合 id を出す。"""
        self.record()
        broken = play_game(self.team, self.rival, home_score=0, away_score=0)
        self.record(broken.id, year=2027)
        before = _snapshot(self.rows())
        orm_models.GamePlateAppearance.objects.filter(game_id=broken.id, sequence=1).update(result="存在しない結果")

        out, err = self._run()

        self.assertIn("1試合", out)
        self.assertIn("1件", err)
        self.assertIn(str(broken.id), err)
        # 組み立てられた試合は導き直され、飛ばした試合の行には触れない
        self.assertEqual(_snapshot(self.rows()), before)
        self.assertTrue(orm_models.GameFieldingLine.objects.filter(game_id=broken.id).exists())


def _historical_apps():
    """マイグレーション 0034 の時点のモデル群。0035 はこれだけを相手にする。"""
    executor = MigrationExecutor(connection)
    return executor.loader.project_state([("myapp", "0034_gamefieldingline_and_batting_entry")]).apps


class BackfillOfLineupEntryTest(FieldingTestBase):
    """データマイグレーション 0035: 既存のラインアップにチームと出場した打席を入れる。"""

    def setUp(self):
        super().setUp()
        self.backfill = import_module("myapp.migrations.0035_backfill_batting_team_and_entry").backfill
        self.pinch = self._player(self.team, "ホーム代打", 77, "内野手")

    def _record_with_pinch_hitter(self):
        lineup = [*self._lineup(), LineupSlot(self.team.id, self.pinch, 8, 1, FP.PINCH_HITTER)]
        entries = self._plate_appearances()
        # 3回裏の8番を代打に差し替える
        target = next(e for e in entries if e.is_bottom and e.inning == 3 and e.batting_order == 8)
        target.batter_id, target.slot_sequence = self.pinch, 1
        target.advances = [RunnerAdvance(self.pinch, Base.BATTER, Base.OUT, R.PUT_OUT)]
        self.recording.record_scorebook(
            self.game.id,
            year=2026,
            played_on=date(2026, 4, 1),
            home_team_id=self.team.id,
            away_team_id=self.rival.id,
            lineup=lineup,
            plate_appearances=entries,
        )
        return target

    def _batting(self):
        return orm_models.GameBattingLine.objects.filter(game_id=self.game.id)

    def _state(self):
        return {row.player_id: (row.team_id, row.entered_sequence) for row in self._batting()}

    def test_the_team_and_the_pinch_hitters_entry_are_restored_from_the_historical_models(self):
        target = self._record_with_pinch_hitter()
        before = self._state()
        # 取り違えたチームと消えた出場時刻から、元に戻ること
        self._batting().update(team_id=self.rival.id, entered_sequence=None)

        self.backfill(_historical_apps(), None)

        self.assertEqual(self._state(), before)
        self.assertEqual(before[self.pinch], (self.team.id, target.sequence))

    def test_a_pitcher_who_entered_gets_the_first_turn_he_pitched(self):
        relief = self._player(self.team, "ホーム救援", 79, "投手")
        lineup = [*self._lineup(), LineupSlot(self.team.id, relief, 9, 1, FP.PITCHER)]
        entries = self._plate_appearances()
        # 4回表（ビジター）の三者三振から救援が投げる
        entries += [self._pa(len(entries) + i + 1, 4, False, o, P.STRIKEOUT_SWINGING) for i, o in enumerate((2, 3, 4))]
        for entry in entries[-3:]:
            entry.pitcher_id = relief
        self.recording.record_scorebook(
            self.game.id,
            year=2026,
            played_on=date(2026, 4, 1),
            home_team_id=self.team.id,
            away_team_id=self.rival.id,
            lineup=lineup,
            plate_appearances=entries,
        )
        before = self._state()
        self._batting().update(entered_sequence=None)

        self.backfill(_historical_apps(), None)

        self.assertEqual(self._state(), before)
        self.assertEqual(before[relief][1], min(e.sequence for e in entries if e.pitcher_id == relief))

    def test_a_side_that_contradicts_the_stints_stops_the_migration(self):
        """打席に現れた側と在籍の記録が食い違うなら、どちらかに寄せず止まる。"""
        self._record_with_pinch_hitter()
        orm_models.PlayerStint.objects.filter(player_id=self.away[0]).delete()
        orm_models.PlayerStint.objects.create(player_id=self.away[0], team=self.team, number=5, from_year=2000)

        with self.assertRaisesRegex(RuntimeError, "食い違う") as caught:
            self.backfill(_historical_apps(), None)

        self.assertIn(f"試合{self.game.id}/選手{self.away[0]}", str(caught.exception))

    def test_a_player_who_cannot_be_placed_stops_the_migration(self):
        """打席に現れず、在籍の記録も無い選手は、推測で埋めずに止まる。"""
        ghost = self._player(self.team, "ホーム幽霊", 80, "内野手")
        lineup = [*self._lineup(), LineupSlot(self.team.id, ghost, 9, 1, FP.DESIGNATED_HITTER)]
        self.recording.record_scorebook(
            self.game.id,
            year=2026,
            played_on=date(2026, 4, 1),
            home_team_id=self.team.id,
            away_team_id=self.rival.id,
            lineup=lineup,
            plate_appearances=self._plate_appearances(),
        )
        orm_models.PlayerStint.objects.filter(player_id=ghost).delete()

        with self.assertRaisesRegex(RuntimeError, "決められない") as caught:
            self.backfill(_historical_apps(), None)

        self.assertIn(f"試合{self.game.id}/選手{ghost}", str(caught.exception))


class SubstitutionTest(FieldingTestBase):
    """途中出場: 代打・代走・投手は打席から、守備固めは入力された半回と打者番号から決める。"""

    def setUp(self):
        super().setUp()
        self.pinch = self._player(self.team, "ホーム代打", 77, "内野手")
        self.defender = self._player(self.team, "ホーム守備固め", 78, "捕手")
        self.relief = self._player(self.team, "ホーム救援", 79, "投手")

    def _slots(self, *, entered_inning=4, entered_is_bottom=False, entered_batter=1, pinch_inning=None, relief=False):
        slots = [
            *self._lineup(),
            LineupSlot(self.team.id, self.pinch, 8, 1, FP.PINCH_HITTER, pinch_inning),
            LineupSlot(
                self.team.id, self.defender, 8, 2, FP.CATCHER, entered_inning, entered_is_bottom, entered_batter
            ),
        ]
        if relief:
            slots.append(LineupSlot(self.team.id, self.relief, 9, 1, FP.PITCHER))
        return slots

    def _entries(self, *, relief=False):
        entries = self._plate_appearances()
        # 3回裏の8番は代打。4回表（ビジター）から捕手を守備固めに替える
        target = next(e for e in entries if e.is_bottom and e.inning == 3 and e.batting_order == 8)
        target.batter_id, target.slot_sequence = self.pinch, 1
        target.advances = [RunnerAdvance(self.pinch, Base.BATTER, Base.OUT, R.PUT_OUT)]
        last = len(entries)
        entries += [self._pa(last + i + 1, 4, False, order, P.STRIKEOUT_SWINGING) for i, order in enumerate((2, 3, 4))]
        if relief:
            for entry in entries[-3:]:
                entry.pitcher_id = self.relief
        return entries

    def _record(self, slots, *, relief=False):
        return self.recording.record_scorebook(
            self.game.id,
            year=2026,
            played_on=date(2026, 4, 1),
            home_team_id=self.team.id,
            away_team_id=self.rival.id,
            lineup=slots,
            plate_appearances=self._entries(relief=relief),
        )

    def _entered(self):
        rows = orm_models.GameBattingLine.objects.filter(game_id=self.game.id, slot_sequence__gte=1)
        return {row.player_id: row.entered_sequence for row in rows}

    def test_the_pinch_hitter_is_derived_and_the_defender_comes_from_the_half(self):
        self._record(self._slots())

        entries = self._entries()
        pinch_turn = next(e.sequence for e in entries if e.batter_id == self.pinch)
        fourth_top = min(e.sequence for e in entries if e.inning == 4 and not e.is_bottom)
        self.assertEqual(self._entered(), {self.pinch: pinch_turn, self.defender: fourth_top})

    def test_entry_in_the_middle_of_a_half_follows_the_batter_number(self):
        """半回の途中の交代: 2人目の打者から入るので、4回表の最初の三振は後任の刺殺にならない。"""
        self._record(self._slots(entered_batter=2))

        entries = self._entries()
        fourth = sorted(e.sequence for e in entries if e.inning == 4 and not e.is_bottom)
        self.assertEqual(self._entered()[self.defender], fourth[1])
        rows = self.rows()
        self.assertEqual(rows[self.defender].putouts, 2)
        # 捕手は3回裏の代打で退いている。入るまでの最初の三振は、誰の刺殺にもならない
        self.assertEqual(rows[self.home[7]].putouts, 4)

    def test_a_batter_number_beyond_the_half_is_rejected_in_japanese(self):
        with self.assertRaisesRegex(InvalidGame, "4回表の打者は3人です"):
            self._record(self._slots(entered_batter=9))

    def test_the_replacement_catcher_is_credited_from_the_half_he_entered(self):
        self._record(self._slots())

        rows = self.rows()
        self.assertEqual(rows[self.defender].putouts, 3, "4回表の三振3つ")
        self.assertEqual(rows[self.home[7]].putouts, 4, "交代前の捕手は4回表の三振を受けない")
        self.assertNotIn(self.pinch, rows)

    def test_a_half_without_plate_appearances_is_rejected_in_japanese(self):
        with self.assertRaisesRegex(InvalidGame, "9回表には打席の記録がありません"):
            self._record(self._slots(entered_inning=9))

    def test_a_defensive_replacement_without_an_entry_is_rejected_with_guidance(self):
        """守備に就く途中出場で出場時刻が無い新しい記録は弾く（不明を作らない）。"""
        slots = self._slots()
        slots[-1] = LineupSlot(self.team.id, self.defender, 8, 2, FP.CATCHER)

        with self.assertRaisesRegex(InvalidGame, "ホーム守備固めの出場した回を入力してください.*代打の打席"):
            self._record(slots)

    def test_input_for_a_pinch_hitter_is_ignored_and_derived_from_the_plate_appearances(self):
        """代打専用の行は入力を無視する（保存のたびに値が変わらない）。"""
        self._record(self._slots(pinch_inning=9))

        entries = self._entries()
        self.assertEqual(self._entered()[self.pinch], next(e.sequence for e in entries if e.batter_id == self.pinch))

    def test_a_relief_pitcher_in_the_lineup_enters_at_the_first_turn_he_pitched(self):
        self._record(self._slots(relief=True), relief=True)

        entries = self._entries(relief=True)
        self.assertEqual(self._entered()[self.relief], min(e.sequence for e in entries if e.pitcher_id == self.relief))

    def test_the_edit_payload_returns_the_input_only_for_rows_that_need_it(self):
        self._record(self._slots(entered_batter=2))
        login_as_manager(self.client, self.team)
        payload = self.client.get(reverse("game_edit", args=[self.game.id])).context["payload"]
        rows = {slot["player_id"]: slot for team in payload["teams"] for slot in team["lineup"]}

        defender = rows[self.defender]
        self.assertEqual(
            (defender["entered_inning"], defender["entered_is_bottom"], defender["entered_batter"]), (4, False, 2)
        )
        self.assertIsNone(rows[self.pinch]["entered_inning"])
        self.assertEqual(set(payload["vocabulary"]["entry_derived_positions"]), {"投", "打", "走"})


class PlayerPageFieldingTest(FieldingTestBase):
    def setUp(self):
        super().setUp()
        self.record()
        second = play_game(self.team, self.rival, year=2027, home_score=0, away_score=0)
        self.record(second.id, year=2027)

    def _url(self, player_id):
        return reverse("player_detail", args=[self.team.id, player_id])

    def _fielding_table(self, player_id) -> tuple[int, list[int], str]:
        html = self.client.get(self._url(player_id)).content.decode()
        tables = [t for t in re.findall(r"<table.*?</table>", html, flags=re.DOTALL) if "守備機会" in t]
        self.assertEqual(len(tables), 1, "守備成績の表が1つだけあること")
        head, body = tables[0].split("</thead>")
        rows = re.findall(r"<tr.*?</tr>", body, flags=re.DOTALL)
        return (
            len(re.findall(r"<th[ >]", head)),
            [len(re.findall(r"<td[ >]", row)) for row in rows],
            re.sub(r"\s+", " ", body),
        )

    def test_profile_carries_career_and_yearly_fielding(self):
        profile = self.service.get_player_profile(self.team.id, self.home[1])

        fielding = profile.fielding
        self.assertIsNotNone(fielding)
        self.assertEqual([row.label for row in fielding.years], ["2026年", "2027年"])
        year = fielding.years[0]
        self.assertEqual((year.games, year.putouts, year.assists, year.errors), (1, 1, 1, 1))
        self.assertEqual((year.total_chances, year.double_plays_turned), (3, 1))
        self.assertAlmostEqual(year.fielding_percentage, 2 / 3)
        # 通算は2試合ぶんを足した実数から守備率を計算し直す
        self.assertEqual(fielding.career.label, "通算")
        self.assertEqual((fielding.career.games, fielding.career.total_chances), (2, 6))
        self.assertAlmostEqual(fielding.career.fielding_percentage, 4 / 6)

    def test_card_keeps_header_and_rows_aligned(self):
        columns, row_columns, _ = self._fielding_table(self.home[0])

        # 年度別2行と通算の3行。見出しと各行の列数が一致する
        self.assertEqual(len(row_columns), 3)
        self.assertEqual(set(row_columns), {columns})

    def test_card_shows_the_values(self):
        _, _, body = self._fielding_table(self.home[0])

        self.assertIn("2026年", body)
        self.assertIn("通算", body)
        # 遊撃手: 2試合で補殺4・併殺2・守備率 1.000
        self.assertIn("1.000", body)

    def test_a_player_who_never_fielded_has_no_card(self):
        body = self.client.get(self._url(self.home[8])).content.decode()

        self.assertNotIn("守備機会", body)

    def test_the_pitchers_page_also_shows_it(self):
        columns, row_columns, _ = self._fielding_table(self.home_pitcher)

        self.assertEqual(set(row_columns), {columns})


class GameDetailFieldingTest(FieldingTestBase):
    def setUp(self):
        super().setUp()
        self.record()

    def test_each_team_box_lists_its_fielders(self):
        detail = self.service.get_game_detail(self.game.id)

        home = {row.player_name: row for row in detail.home_box.fielding}
        short = home["ホーム1"]
        self.assertEqual((short.position_label, short.putouts, short.assists, short.errors), ("遊", 0, 2, 0))
        self.assertEqual(home["ホーム2"].errors, 1)
        # 打順にいない投手も載る。位置は「投」
        self.assertEqual(home["ホーム先発"].position_label, "投")
        # 指名打者は守備に就かないので載らない
        self.assertNotIn("ホーム9", home)
        away = {row.player_name: row for row in detail.away_box.fielding}
        self.assertEqual(away["ビジター8"].putouts, 9)

    def test_fielders_are_in_batting_order_then_pitchers(self):
        detail = self.service.get_game_detail(self.game.id)

        names = [row.player_name for row in detail.home_box.fielding]
        self.assertEqual(names[:3], ["ホーム1", "ホーム2", "ホーム3"])
        self.assertEqual(names[-1], "ホーム先発")

    def test_the_page_renders_a_fielding_card_for_both_teams(self):
        body = self.client.get(reverse("game_detail", args=[self.game.id])).content.decode()

        self.assertIn(f"{self.team.name} 守備", body)
        self.assertIn(f"{self.rival.name} 守備", body)

    def test_fielding_table_keeps_header_and_rows_aligned(self):
        body = self.client.get(reverse("game_detail", args=[self.game.id])).content.decode()

        tables = [t for t in re.findall(r"<table.*?</table>", body, flags=re.DOTALL) if "<th>選手名</th>" in t]
        fielding = [t for t in tables if "刺殺" in t]
        self.assertEqual(len(fielding), 2)
        for table in fielding:
            head, rows = table.split("</thead>")
            columns = len(re.findall(r"<th[ >]", head))
            for row in re.findall(r"<tr.*?</tr>", rows, flags=re.DOTALL):
                self.assertEqual(len(re.findall(r"<td[ >]", row)), columns)

    def test_a_game_without_plate_appearances_has_no_card(self):
        plain = play_game(self.team, self.rival, home_score=1, away_score=0)

        body = self.client.get(reverse("game_detail", args=[plain.id])).content.decode()

        self.assertNotIn("守備</div>", body)
