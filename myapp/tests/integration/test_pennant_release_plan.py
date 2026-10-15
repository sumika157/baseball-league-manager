"""戦力外の指名を締める処理が使う（#168 段階B）: 指名どおりに外す・使い終わったら消す・計算中の変更で断る・永続化。

世界は `test_pennant_offseason_screens.py` と同じ（世界Aはシーズン終了）。引退は `decide_retirements` を
差し替えて決め、AI 球団（ライバル球団）の自動の戦力外は `decide_releases` を差し替えて観察する。
"""

from contextlib import ExitStack
from unittest import mock

from myapp.domain.exceptions import AlreadyClosed, InvalidWorld, PlayerNotFound, TeamNotFound
from myapp.domain.pennant.offseason import plan_offseason
from myapp.domain.pennant.release import ReleasePlan
from myapp.domain.value_objects import DepartureRoute
from myapp.infrastructure import orm_models
from myapp.infrastructure.repositories import DjangoReleasePlanRepository
from myapp.presentation.views import build_pennant_offseason_service, build_pennant_world_service

from ..helpers import real_scope, world_scope
from .test_pennant_advance import YEAR
from .test_pennant_offseason_screens import DECIDE_RELEASES, DECIDE_RETIREMENTS, ScreenCase

PLAN_OFFSEASON = "myapp.application.pennant_offseason.plan_offseason"


class ReleasePlanCase(ScreenCase):
    def team_id(self) -> int:
        team_id = orm_models.PennantWorld.objects.get(id=self.world_a).managed_team_id
        assert team_id is not None
        return team_id

    def repository(self, world_id=None) -> DjangoReleasePlanRepository:
        return DjangoReleasePlanRepository(world_scope(self.world_a if world_id is None else world_id))

    def save_plan(self, player_ids, *, year: int = YEAR) -> None:
        self.repository().save(ReleasePlan(team_id=self.team_id(), year=year, player_ids=frozenset(player_ids)))

    def left_via(self, player_id: int) -> str | None:
        stint = orm_models.PlayerStint.objects.get(player_id=player_id, team__league__world_id=self.world_a)
        return stint.left_via if stint.to_year == YEAR else None

    def close(self, *, retired=(), auto_released=None):
        """締める。AI の戦力外（自動）は `auto_released` の選手を外す（None なら差し替えない）。"""
        decide = None
        with ExitStack() as stack:
            stack.enter_context(mock.patch(DECIDE_RETIREMENTS, return_value=tuple(retired)))
            if auto_released is not None:
                chosen = set(auto_released)
                decide = mock.Mock(
                    side_effect=lambda club, remaining, **kwargs: tuple(
                        p.player_id for p in remaining if p.player_id in chosen
                    )
                )
                stack.enter_context(mock.patch(DECIDE_RELEASES, decide))
            result = build_pennant_offseason_service(world_scope(self.world_a)).close_season(expected_year=YEAR)
        return result, decide


class CloseWithManualReleasesTest(ReleasePlanCase):
    def test_only_the_chosen_players_are_released_and_the_plan_is_cleared(self):
        players = self.managed_players(4)
        self.save_plan(players[:2])

        result, _ = self.close()

        self.assertEqual(result.released_count, 2)
        self.assertEqual([self.left_via(p) for p in players], [DepartureRoute.RELEASED.value] * 2 + [None, None])
        self.assertIsNone(self.repository().find_by_team(self.team_id()), "使い終わった指名は消す")
        self.assertFalse(orm_models.PennantReleasePlan.objects.exists())

    def test_manual_zero_overrides_the_automatic_rule_for_the_managed_club_only(self):
        """手動で0人なら、自動の規則が外す選手も外さない。AI 球団は自動のまま（規則が呼ばれるのは AI 球団だけ）。"""
        own = self.managed_players(1)
        rival = list(
            orm_models.PlayerStint.objects.filter(team__league__world_id=self.world_a, to_year=None)
            .exclude(team_id=self.team_id())
            .values_list("player_id", flat=True)[:1]
        )
        self.save_plan(())

        result, decide = self.close(auto_released=[*own, *rival])

        self.assertEqual(result.released_count, 1)
        self.assertIsNone(self.left_via(own[0]))
        self.assertEqual(self.left_via(rival[0]), DepartureRoute.RELEASED.value)
        self.assertEqual({call.args[0].team_id for call in decide.call_args_list}, {_rival_team(self)})

    def test_a_chosen_player_who_retires_is_recorded_as_retired(self):
        players = self.managed_players(2)
        self.save_plan(players)

        result, _ = self.close(retired=players[:1])

        self.assertEqual((result.retired_count, result.released_count), (1, 1))
        self.assertEqual(self.left_via(players[0]), DepartureRoute.RETIRED.value)
        self.assertEqual(self.left_via(players[1]), DepartureRoute.RELEASED.value)

    def test_a_plan_for_another_year_is_not_used_but_cleared(self):
        players = self.managed_players(1)
        self.save_plan(players, year=YEAR - 1)

        result, _ = self.close(auto_released=[])

        self.assertEqual(result.released_count, 0)
        self.assertFalse(orm_models.PennantReleasePlan.objects.exists())

    def test_the_close_option_counts_the_manual_plan(self):
        service = build_pennant_offseason_service(world_scope(self.world_a))
        self.assertIsNone(service.close_option().release_count, "自動")
        self.save_plan(self.managed_players(3))
        self.assertEqual(service.close_option().release_count, 3)


class ReleasePlanChangedWhileClosingTest(ReleasePlanCase):
    def test_a_plan_saved_during_the_calculation_stops_the_close(self):
        """外で計算している間に GM が指名を変えたら、古い計算を書かずに断る（`_state()` に指名が入る。#100）。"""
        service = build_pennant_offseason_service(world_scope(self.world_a))
        original = service._prepare

        def prepare_then_save(world, year):
            prepared = original(world, year)
            self.save_plan(self.managed_players(1))
            return prepared

        service._prepare = prepare_then_save  # type: ignore[method-assign]
        with mock.patch(DECIDE_RETIREMENTS, return_value=()), self.assertRaises(AlreadyClosed):
            service.close_season(expected_year=YEAR)

        self.assertFalse(orm_models.PennantPlayerRatings.objects.filter(year=YEAR + 1).exists(), "何も書かない")
        self.assertIsNotNone(self.repository().find_by_team(self.team_id()), "GM の指名は残る")


class ReleasePlanRepositoryTest(ReleasePlanCase):
    def test_round_trip_and_auto_deletes_the_rows(self):
        players = self.managed_players(2)
        self.save_plan(players)
        self.assertEqual(
            self.repository().find_by_team(self.team_id()), ReleasePlan(self.team_id(), YEAR, frozenset(players))
        )

        self.save_plan(())
        self.assertEqual(self.repository().find_by_team(self.team_id()).player_ids, frozenset())

        self.repository().save(ReleasePlan(self.team_id(), YEAR, None))
        self.assertIsNone(self.repository().find_by_team(self.team_id()))
        self.assertFalse(orm_models.PennantReleasePlanEntry.objects.exists())

    def test_the_real_data_and_other_worlds_cannot_be_written(self):
        with self.assertRaises(InvalidWorld):
            DjangoReleasePlanRepository(real_scope()).save(ReleasePlan(self.team_id(), YEAR, frozenset()))
        with self.assertRaises(TeamNotFound):
            self.repository(self.world_b).save(ReleasePlan(self.team_id(), YEAR, frozenset()))
        with self.assertRaises(TeamNotFound):
            self.repository(self.world_b).find_by_team(self.team_id())

    def test_players_outside_the_world_cannot_be_chosen(self):
        outsider = orm_models.PlayerStint.objects.filter(team__league__world_id=self.world_b).first().player_id
        with self.assertRaises(PlayerNotFound):
            self.save_plan([outsider])

    def test_deleting_the_world_deletes_the_plan(self):
        self.save_plan(self.managed_players(1))

        build_pennant_world_service(real_scope()).delete_world(self.world_a)

        self.assertFalse(orm_models.PennantReleasePlan.objects.exists())
        self.assertFalse(orm_models.PennantReleasePlanEntry.objects.exists())


def _rival_team(case: ReleasePlanCase) -> int:
    return (
        orm_models.Team.objects.filter(league__world_id=case.world_a)
        .exclude(id=case.team_id())
        .values_list("id", flat=True)
        .get()
    )


class TrackRecordMaterialsTest(ReleasePlanCase):
    def test_the_close_passes_the_track_record_derived_from_the_world(self):
        """実績の材料は世界の在籍と試合から導いて渡す（保存しない）。分岐した選手は在籍1年目で、生え抜きは不明。"""
        captured = {}

        def spy(clubs, **kwargs):
            captured["clubs"] = clubs
            return plan_offseason(clubs, **kwargs)

        with mock.patch(PLAN_OFFSEASON, side_effect=spy):
            self.close(auto_released=[])

        players = [player for club in captured["clubs"] for player in club.players]
        self.assertTrue(players)
        self.assertEqual({player.track_record.seasons for player in players}, {1}, "開幕年に分岐して、その年に締める")
        self.assertFalse(any(player.track_record.homegrown for player in players), "分岐した在籍は入団の経路が不明")
        hitter = max(players, key=lambda player: player.track_record.hits)
        lines = orm_models.GameBattingLine.objects.filter(player_id=hitter.player_id)
        hits = sum(line.singles + line.doubles + line.triples + line.home_runs for line in lines)
        self.assertEqual(hitter.track_record.hits, hits)
        self.assertGreater(hits, 0)
