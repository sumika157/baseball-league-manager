"""GM が1巡ずつ進めるドラフト会議のサービス（`PennantDraftService`。#297 D3 T2）と、締める処理・設定・戦力外の固定。

土台の世界（2リーグ×3球団・公式戦を終えた方式「なし」と NPB 方式）は `world_case.PostseasonWorldCase`。
会議は方式「なし」の世界で開く（ポストシーズンを経ずに開ける）。NPB 方式の世界はポストシーズンの前なので
開けない（#297 Q1）。
"""

from __future__ import annotations

from collections import defaultdict
from unittest import mock

from django.db import transaction

from myapp.application.dto import DraftMeetingView
from myapp.domain.exceptions import (
    AlreadyClosed,
    DraftChanged,
    DraftConflict,
    DraftInProgress,
    DraftOrderRuleLocked,
    DraftStarted,
    InvalidDraftAction,
    PostseasonFormatLocked,
    PostseasonNotFinished,
)
from myapp.domain.pennant.development import DevelopmentDecision
from myapp.domain.pennant.draft_order import DraftOrderRule
from myapp.domain.pennant.draft_record import DraftMeetingStatus, DraftRecord
from myapp.domain.pennant.offseason import prepare_meeting
from myapp.domain.pennant.postseason import PostseasonFormat
from myapp.domain.pennant.world import WorldScope
from myapp.infrastructure import orm_models
from myapp.infrastructure.repositories import DjangoDraftRepository, DjangoWorldRepository
from myapp.presentation.views import (
    build_pennant_draft_service,
    build_pennant_offseason_service,
    build_pennant_settings_service,
)

from .test_pennant_midseason import add_development
from .world_case import YEAR, PostseasonWorldCase


class DraftServiceCase(PostseasonWorldCase):
    @property
    def world_id(self) -> int:
        return self.none_world

    def service(self, world_id: int | None = None):
        return build_pennant_draft_service(world_id or self.world_id)

    def offseason(self):
        return build_pennant_offseason_service(self.world_id)

    def record(self) -> DraftRecord | None:
        return DjangoDraftRepository(WorldScope.pennant(self.world_id)).find(YEAR)

    def managed(self) -> int:
        team_id = DjangoWorldRepository().find_by_id(self.world_id).managed_team_id
        assert team_id is not None
        return team_id

    def view(self) -> DraftMeetingView:
        return self.service().view(expected_year=YEAR)

    def start(self) -> DraftMeetingView:
        self.service().start(expected_year=YEAR)
        return self.view()

    def auto_complete(self) -> None:
        view = self.view()
        self.service().auto_complete(expected_year=YEAR, expected_seq=view.last_seq)

    def next_year_ratings(self) -> bool:
        return orm_models.PennantPlayerRatings.objects.filter(
            year=YEAR + 1, player__stints__team__league__world_id=self.world_id
        ).exists()

    def rookies(self) -> dict[int, list[str]]:
        """締めた年のドラフト・新外国人で入った選手の名前（球団 id ごと。並びは名前の順）。"""
        by_team: dict[int, list[str]] = defaultdict(list)
        for team_id, name in orm_models.PlayerStint.objects.filter(
            team__league__world_id=self.world_id, from_year=YEAR + 1
        ).values_list("team_id", "player__name"):
            by_team[team_id].append(name)
        return {team_id: sorted(names) for team_id, names in by_team.items()}


class OpeningTest(DraftServiceCase):
    def test_the_meeting_can_be_started_after_the_postseason(self):
        view = self.view()

        self.assertIs(view.status, DraftMeetingStatus.NOT_STARTED)
        self.assertTrue(view.can_start)
        self.assertEqual(view.year, YEAR)
        self.assertEqual(view.managed_team_id, self.managed())

    def test_the_meeting_cannot_be_opened_before_the_postseason_ends(self):
        """会議を開けるのはポストシーズンの後（締められる条件と同じ。#297 Q1）。"""
        service = self.service(self.npb_world)
        view = service.view()
        self.assertFalse(view.can_start)
        self.assertIn("ポストシーズン", view.blocker)

        with self.assertRaises(PostseasonNotFinished):
            service.start(expected_year=YEAR)
        self.assertFalse(orm_models.PennantDraft.objects.exists())

    def test_start_saves_the_slots_and_the_ai_moves_up_to_the_gms_turn(self):
        view = self.start()
        record = self.record()

        assert record is not None
        self.assertIs(view.status, DraftMeetingStatus.IN_PROGRESS)
        self.assertTrue(view.is_my_turn)
        self.assertEqual(view.last_seq, record.last_seq)
        self.assertTrue(any(row.selectable for row in view.prospects))
        self.assertFalse(any(row.taken_by for row in view.prospects), "1位の入札は抽選まで確定しない")
        self.assertTrue(view.round_rows)
        # 人数の幅は、締める処理と同じ入力から同じ関数で計算したもの
        world = DjangoWorldRepository().find_by_id(self.world_id)
        inputs = self.offseason()._inputs.read(world, YEAR)
        prepared = prepare_meeting(
            inputs.clubs,
            year=YEAR,
            world_seed=world.seed,
            start_year=world.start_year,
            used_names=inputs.used_names,
            draft=inputs.draft,
            manual_releases=inputs.manual_releases,
        )
        self.assertEqual(record.slots, prepared.setup.slots)
        self.assertEqual(record.signature, prepared.setup.pool.signature)

    def test_a_meeting_starts_once(self):
        self.start()
        with self.assertRaises(DraftStarted):
            self.service().start(expected_year=YEAR)

    def test_an_old_year_is_refused(self):
        with self.assertRaises(AlreadyClosed):
            self.service().start(expected_year=YEAR + 1)
        self.assertIsNone(self.record())

    def test_a_change_during_the_calculation_stops_the_start(self):
        """外で計算している間に戦力外の指名が変わったら、古い計算の人数の幅を写さずに断る（状態の目印）。"""
        service = self.service()
        read = service._inputs.read

        def read_then_release(world, year):
            inputs = read(world, year)
            self.offseason().start_manual_releases(expected_year=YEAR)
            return inputs

        with (
            mock.patch.object(service._inputs, "read", side_effect=read_then_release),
            self.assertRaises(AlreadyClosed),
        ):
            service.start(expected_year=YEAR)
        self.assertIsNone(self.record(), "何も書かない")


class NominateTest(DraftServiceCase):
    def test_the_gm_nominates_and_the_ai_moves_to_the_gms_next_turn(self):
        view = self.start()
        chosen = next(row for row in view.prospects if row.selectable)

        step = self.service().nominate(chosen.key, expected_year=YEAR, expected_seq=view.last_seq)

        record = self.record()
        assert record is not None
        self.assertEqual(step.last_seq, record.last_seq)
        self.assertGreater(step.last_seq, view.last_seq)
        after = self.view()
        self.assertTrue(after.turn is None or after.turn.is_mine, "次の GM の番まで AI が進める")
        # 1位の入札は抽選まで解けている（GM の分は当たりか、外れて次の回に再入札）
        self.assertTrue(any(move.prospect_key == chosen.key for move in step.moves))
        self.assertTrue(after.own_moves or after.turn is not None)

    def test_an_old_screen_is_a_conflict_and_writes_nothing(self):
        view = self.start()
        chosen = next(row for row in view.prospects if row.selectable)
        self.service().nominate(chosen.key, expected_year=YEAR, expected_seq=view.last_seq)
        last = self.view().last_seq

        with self.assertRaises(DraftConflict):  # 二重送信・別のタブ
            self.service().nominate(chosen.key, expected_year=YEAR, expected_seq=view.last_seq)
        self.assertEqual(self.view().last_seq, last)

    def test_actions_the_meeting_does_not_accept_write_nothing(self):
        view = self.start()
        with self.assertRaises(InvalidDraftAction):  # 1人も指名しないうちは選択を終えられない
            self.service().pass_turn(expected_year=YEAR, expected_seq=view.last_seq)
        with self.assertRaises(InvalidDraftAction):  # 指名できる候補がいるのでお任せにできない
            self.service().delegate_turn(expected_year=YEAR, expected_seq=view.last_seq)
        with self.assertRaises(InvalidDraftAction):  # 候補にいない
            self.service().nominate(10_000, expected_year=YEAR, expected_seq=view.last_seq)
        self.assertFalse(view.can_pass)
        self.assertFalse(view.can_delegate)
        self.assertEqual(self.view().last_seq, view.last_seq)

    def test_a_taken_prospect_cannot_be_nominated(self):
        view = self.start()
        chosen = next(row for row in view.prospects if row.selectable)
        self.service().nominate(chosen.key, expected_year=YEAR, expected_seq=view.last_seq)
        after = self.view()
        taken = next(row for row in after.prospects if row.taken_by)
        self.assertFalse(taken.selectable)
        if after.turn is not None:
            with self.assertRaises(InvalidDraftAction):
                self.service().nominate(taken.key, expected_year=YEAR, expected_seq=after.last_seq)

    def test_without_a_meeting_writes_are_a_conflict(self):
        with self.assertRaises(DraftConflict):
            self.service().nominate(0, expected_year=YEAR, expected_seq=0)

    def test_auto_complete_finishes_the_meeting(self):
        self.start()
        self.auto_complete()

        view = self.view()
        self.assertIs(view.status, DraftMeetingStatus.FINISHED)
        self.assertIsNone(view.turn)
        self.assertEqual(view.round_rows, ())
        self.assertTrue(view.own_moves)
        with self.assertRaises(InvalidDraftAction):
            self.service().auto_complete(expected_year=YEAR, expected_seq=view.last_seq)


class CloseWithTheMeetingTest(DraftServiceCase):
    def test_an_unfinished_meeting_stops_the_close(self):
        self.start()
        with self.assertRaises(DraftInProgress):
            self.offseason().close_season(expected_year=YEAR)
        self.assertFalse(self.next_year_ratings(), "何も書かない")
        self.assertIsNotNone(self.record())

    def test_the_gms_pick_becomes_a_rookie_and_the_meeting_is_removed(self):
        """入札の無い規則（完全ウェーバー）で、GM が選んだ候補が自軍の新人になる（抽選で外れない）。"""
        build_pennant_settings_service(self.world_id).change_draft_order_rule(DraftOrderRule.WAIVER)
        view = self.start()
        chosen = [row for row in view.prospects if row.selectable][-1]
        step = self.service().nominate(chosen.key, expected_year=YEAR, expected_seq=view.last_seq)
        self.service().auto_complete(expected_year=YEAR, expected_seq=step.last_seq)

        self.offseason().close_season(expected_year=YEAR)

        self.assertIn(chosen.name, self.rookies()[self.managed()])
        self.assertIsNone(self.record(), "締めたら会議の記録は消える")
        self.assertFalse(orm_models.PennantDraftAction.objects.exists())

    def test_the_development_choices_reach_the_meeting_and_the_close_in_the_same_way(self):
        """育成の指名（#170 P2）は会議を始めるときと締めるときの同じ入力（`OffseasonInputsReader`）から読むので、
        指名した昇格を含めて人数の幅が食い違わない（`DraftChanged` にならない）。"""
        prospect = add_development(self.world_id, "育成指名", "199", draft_year=YEAR - 1, year=YEAR)
        self.offseason().set_development({prospect: DevelopmentDecision.PROMOTE}, expected_year=YEAR)
        self.start()
        self.auto_complete()

        result = self.offseason().close_season(expected_year=YEAR)

        self.assertGreaterEqual(result.promoted_count, 1)
        self.assertEqual(orm_models.PlayerStint.objects.get(player_id=prospect).promoted_year, YEAR + 1)

    def test_leaving_the_whole_meeting_to_the_ai_drafts_the_same_rookies_as_no_meeting(self):
        savepoint = transaction.savepoint()
        try:
            self.offseason().close_season(expected_year=YEAR)
            without_meeting = self.rookies()
        finally:
            transaction.savepoint_rollback(savepoint)

        self.start()
        self.auto_complete()
        self.offseason().close_season(expected_year=YEAR)

        self.assertEqual(self.rookies(), without_meeting)

    def test_a_meeting_started_while_closing_stops_the_close(self):
        """締める計算の間に会議を始めたら、会議を使わずに計算した新人を書かずに断る（会議の目印）。"""
        service = self.offseason()
        prepare = service._prepare

        def prepare_then_start(world, year):
            prepared = prepare(world, year)
            self.service().start(expected_year=YEAR)
            return prepared

        with mock.patch.object(service, "_prepare", side_effect=prepare_then_start), self.assertRaises(AlreadyClosed):
            service.close_season(expected_year=YEAR)
        self.assertFalse(self.next_year_ratings(), "何も書かない")
        self.assertIsNotNone(self.record(), "会議は残る")


class LocksTest(DraftServiceCase):
    def test_releases_are_fixed_after_the_start(self):
        offseason = self.offseason()
        self.assertTrue(offseason.release_view().can_change)
        self.start()

        view = offseason.release_view()
        self.assertFalse(view.can_change)
        self.assertIn("ドラフト会議", view.blocker)
        for change in (
            lambda: offseason.start_manual_releases(expected_year=YEAR),
            lambda: offseason.set_releases([], expected_year=YEAR),
            lambda: offseason.reset_releases(expected_year=YEAR),
        ):
            with self.assertRaises(DraftStarted):
                change()
        self.assertFalse(orm_models.PennantReleasePlan.objects.exists(), "何も書かない")

    def test_development_choices_are_fixed_after_the_start(self):
        """育成の指名（昇格・満了の自由契約。#170 P2）も会議の人数の幅を変えるので、始めた後は変えられない。"""
        offseason = self.offseason()
        before = offseason.development_view()
        self.assertTrue(before.can_change)
        decisions = {row.player.player_id: row.decision for row in before.rows}
        self.start()

        view = offseason.development_view()
        self.assertFalse(view.can_change)
        self.assertIn("ドラフト会議", view.blocker)
        for change in (
            lambda: offseason.start_manual_development(expected_year=YEAR),
            lambda: offseason.set_development(decisions, expected_year=YEAR),
            lambda: offseason.reset_development(expected_year=YEAR),
        ):
            with self.assertRaises(DraftStarted):
                change()
        self.assertFalse(orm_models.PennantDevelopmentPlan.objects.exists(), "何も書かない")

    def test_the_postseason_format_is_fixed_while_the_meeting_lasts(self):
        """会議はポストシーズンの後に開く（#297 Q1）。会議を始めた後に方式「なし」から変えると、ポストシーズンの前に
        始めた会議になるので変えられない。"""
        settings = build_pennant_settings_service(self.world_id)
        self.assertTrue(settings.view().can_change)
        self.start()

        self.assertIn("ドラフト会議", settings.view().blocker)
        with self.assertRaises(PostseasonFormatLocked):
            settings.change_postseason_format(PostseasonFormat.NPB)
        self.assertIs(DjangoWorldRepository().find_by_id(self.world_id).postseason_format, PostseasonFormat.NONE)

    def test_the_rule_is_fixed_while_the_meeting_lasts(self):
        settings = build_pennant_settings_service(self.world_id)
        self.start()

        self.assertIn("ドラフト会議", settings.view().draft_order_blocker)
        with self.assertRaises(DraftOrderRuleLocked):
            settings.change_draft_order_rule(DraftOrderRule.WAIVER)

        self.auto_complete()
        self.offseason().close_season(expected_year=YEAR)
        self.assertEqual(settings.view().draft_order_blocker, "", "締めたら会議の記録は消えるので、また変えられる")


class ChangedMeetingTest(DraftServiceCase):
    def test_a_meeting_that_cannot_be_replayed_can_only_be_reset(self):
        """コードが変わって候補が変わった会議（`DraftChanged`）は、やり直すことしかできない（#297 Q5）。"""
        view = self.start()
        orm_models.PennantDraft.objects.filter(world_id=self.world_id).update(signature="0" * 32)

        changed = self.view()
        self.assertTrue(changed.changed)
        self.assertEqual(changed.prospects, ())
        with self.assertRaises(DraftChanged):
            self.service().nominate(0, expected_year=YEAR, expected_seq=view.last_seq)
        with self.assertRaises(DraftChanged):
            self.offseason().close_season(expected_year=YEAR)

        self.service().reset(expected_year=YEAR, expected_seq=changed.last_seq)

        self.assertIsNone(self.record())
        self.assertTrue(self.view().can_start)

    def test_a_healthy_meeting_cannot_be_reset(self):
        view = self.start()
        with self.assertRaises(InvalidDraftAction):
            self.service().reset(expected_year=YEAR, expected_seq=view.last_seq)
        with self.assertRaises(DraftConflict):
            self.service().reset(expected_year=YEAR, expected_seq=view.last_seq + 1)
        self.assertIsNotNone(self.record())
