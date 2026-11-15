"""ペナントの首脳陣（#210 M1: 監督と采配の傾向）。

決定（2026-11-15。#210 のコメント）と計画（#398）から:
- 世界の作成で全球団に架空の監督が就く（開幕年から）。実データには書けない・他の世界は読めない
- 進める処理は、その年の監督の傾向を試合に渡す。監督のいない球団（この機能より前の世界）は標準の傾向で、
  標準の傾向なら傾向を足す前と同じ試合になる
- 締める処理は、高齢の監督を締める年で退任させ、後任と監督のいない球団の監督を翌年から就ける。締めた年の首脳陣は
  書き換えない（`AlreadyClosed`）。2回締めても増えない
- 世界を消すと首脳陣も消える
- GM ホームの「監督」カード・球団史の「監督」列・オフの結果の「監督の交代」（書き込みの導線は無い）

世界は2リーグ×3球団（公式戦を1週間だけ進め、残りの対戦を消して「公式戦を終えた」年にする）。
"""

from __future__ import annotations

from datetime import date

from django.db import transaction
from django.test import TestCase

from myapp.domain.exceptions import AlreadyClosed, InvalidStaffTenure, InvalidWorld
from myapp.domain.pennant.schedule import AdvanceTarget
from myapp.domain.pennant.staff import Appointment, ManagerStyle, StaffMember, StaffRole
from myapp.infrastructure import orm_models
from myapp.infrastructure.repositories import DjangoFixtureRepository, DjangoStaffRepository
from myapp.presentation.views import (
    build_pennant_offseason_service,
    build_pennant_season_service,
    build_pennant_staff_view_service,
    build_pennant_world_service,
)

from ..helpers import make_league, real_scope, world_scope, ws_url
from .world_case import YEAR, _register_club

SEED = 13
AXES = ("starter_leash", "pinch_hitting", "base_stealing", "bunting")


def _newcomer(name: str = "新監督") -> StaffMember:
    return StaffMember(name=name, name_kana="シンカントク", birth_date=date(1975, 5, 5), style=ManagerStyle())


class StaffCase(TestCase):
    """2リーグ×3球団の世界。`teams` は世界の全球団（`teams[0]` が受け持つ球団）。"""

    world_id: int
    teams: list[int]
    league_ids: list[int]
    managed_source_id: int

    @classmethod
    def setUpTestData(cls):
        cls.league_ids = []
        for league_index in range(2):
            league = make_league(name=f"監督{league_index}リーグ")
            cls.league_ids.append(league.id)
            for index in range(3):
                team = orm_models.Team.objects.create(league=league, name=f"監督{league_index}-{index}球団")
                _register_club(team, f"監{league_index}{index}")
                if league_index == 0 and index == 0:
                    cls.managed_source_id = team.id
        cls.world_id = cls.create_world()
        cls.teams = list(
            orm_models.Team.objects.filter(league__world_id=cls.world_id).order_by("id").values_list("id", flat=True)
        )

    @classmethod
    def create_world(cls) -> int:
        return (
            build_pennant_world_service(real_scope())
            .create_world(
                name="監督の世界",
                owner_id=None,
                source_league_ids=cls.league_ids,
                start_year=YEAR,
                seed=SEED,
                managed_source_team_id=cls.managed_source_id,
            )
            .world.id
        )

    # --- 道具 ---

    def tenures(self, world_id=None) -> list[tuple[int, int, int | None]]:
        """(球団, 初年, 最終年)。"""
        world_id = self.world_id if world_id is None else world_id
        return list(
            orm_models.PennantStaffTenure.objects.filter(team__league__world_id=world_id)
            .order_by("team_id", "first_year")
            .values_list("team_id", "first_year", "last_year")
        )

    def members(self, world_id=None):
        world_id = self.world_id if world_id is None else world_id
        return orm_models.PennantStaffMember.objects.filter(tenures__team__league__world_id=world_id).distinct()

    def finish_regular_season(self) -> None:
        build_pennant_season_service(world_scope(self.world_id)).advance(AdvanceTarget.WEEK)
        fixtures = DjangoFixtureRepository(world_scope(self.world_id))
        fixtures.remove(fixtures.find_all())

    def close(self) -> None:
        build_pennant_offseason_service(world_scope(self.world_id)).close_season()

    def plate_appearances(self):
        return list(
            orm_models.GamePlateAppearance.objects.filter(game__home_team__league__world_id=self.world_id)
            .order_by("game__played_on", "game__home_team_id", "sequence")
            .values_list("game__played_on", "sequence", "batter_id", "pitcher_id", "result")
        )

    def set_styles(self, level: int) -> None:
        self.members().update(**dict.fromkeys(AXES, level))

    def make_manager_old(self, team_id: int, *, birth: date) -> None:
        member_id = orm_models.PennantStaffTenure.objects.get(team_id=team_id).member_id
        orm_models.PennantStaffMember.objects.filter(id=member_id).update(birth_date=birth)

    def repository(self, scope=None) -> DjangoStaffRepository:
        return DjangoStaffRepository(scope or world_scope(self.world_id))


class WorldCreationTest(StaffCase):
    def test_every_club_gets_a_manager_from_the_opening_year(self):
        self.assertEqual(self.tenures(), [(team_id, YEAR, None) for team_id in self.teams])
        members = list(self.members())
        self.assertEqual(len({member.name for member in members}), len(self.teams))
        for member in members:
            age = (
                YEAR
                - member.birth_date.year
                - (0 if (member.birth_date.month, member.birth_date.day) <= (4, 1) else 1)
            )
            self.assertTrue(45 <= age <= 65, age)

    def test_the_same_seed_gives_the_same_managers(self):
        other = self.create_world()
        names = sorted(self.members().values_list("name", flat=True))
        self.assertEqual(sorted(self.members(other).values_list("name", flat=True)), names)

    def test_deleting_the_world_deletes_its_staff(self):
        other = self.create_world()
        build_pennant_world_service(real_scope()).delete_world(other)

        self.assertEqual(self.tenures(other), [])
        self.assertEqual(self.members().count(), len(self.teams), "他の世界の首脳陣は残る")
        self.assertEqual(orm_models.PennantStaffMember.objects.count(), len(self.teams))


class AdvanceTest(StaffCase):
    def test_standard_tactics_play_the_same_games_as_a_world_without_managers(self):
        """標準の傾向（全軸0）の監督なら、監督のいない世界（この機能より前）と1打席も違わない。

        監督の傾向がずれていれば試合が変わる（進める処理が傾向を渡している）。
        """
        savepoint = transaction.savepoint()
        orm_models.PennantStaffTenure.objects.filter(team__league__world_id=self.world_id).delete()
        self.finish_regular_season()
        without_managers = self.plate_appearances()
        transaction.savepoint_rollback(savepoint)

        savepoint = transaction.savepoint()
        self.set_styles(0)
        self.finish_regular_season()
        standard = self.plate_appearances()
        transaction.savepoint_rollback(savepoint)

        self.set_styles(2)
        self.finish_regular_season()
        aggressive = self.plate_appearances()

        self.assertTrue(without_managers)
        self.assertEqual(standard, without_managers)
        self.assertNotEqual(aggressive, without_managers)


class CloseTest(StaffCase):
    def test_an_old_manager_retires_and_a_successor_starts_next_year(self):
        # 翌年（YEAR+1）の4月1日に70歳
        self.make_manager_old(self.teams[0], birth=date(YEAR + 1 - 70, 1, 1))
        self.finish_regular_season()
        self.close()

        own = [row for row in self.tenures() if row[0] == self.teams[0]]
        self.assertEqual(own, [(self.teams[0], YEAR, YEAR), (self.teams[0], YEAR + 1, None)])
        changes = build_pennant_staff_view_service(world_scope(self.world_id)).manager_changes(YEAR)
        self.assertEqual([change.team_id for change in changes], [self.teams[0]])
        self.assertEqual(changes[0].predecessor_age, 69)

    def test_closing_twice_does_not_add_managers(self):
        self.make_manager_old(self.teams[0], birth=date(YEAR + 1 - 75, 1, 1))
        self.finish_regular_season()
        self.close()
        after_first = self.tenures()

        with self.assertRaises(AlreadyClosed):
            self.close()
        self.assertEqual(self.tenures(), after_first)

    def test_a_world_from_before_this_feature_gets_managers_from_next_year(self):
        """監督のいない世界（この機能より前）は、締めるときに翌年から監督が就く（締める年には遡らない）。"""
        orm_models.PennantStaffTenure.objects.filter(team__league__world_id=self.world_id).delete()
        orm_models.PennantStaffMember.objects.filter(tenures__isnull=True).delete()
        self.finish_regular_season()
        self.close()

        self.assertEqual(self.tenures(), [(team_id, YEAR + 1, None) for team_id in self.teams])
        history = build_pennant_staff_view_service(world_scope(self.world_id)).manager_history(
            self.teams[0], [YEAR, YEAR + 1]
        )
        self.assertEqual(history[0].name, "")
        self.assertTrue(history[1].name)

    def test_a_closed_year_cannot_be_rewritten(self):
        self.finish_regular_season()
        self.close()
        serving = self.repository().find_serving(YEAR)

        with self.assertRaises(AlreadyClosed):
            self.repository().apply(retirements=[serving[0].tenure.close(YEAR)], appointments=[])
        with self.assertRaises(AlreadyClosed):
            self.repository().apply(
                retirements=[],
                appointments=[Appointment(_newcomer(), self.teams[0], StaffRole.MANAGER, YEAR)],
            )


class RepositoryTest(StaffCase):
    def test_the_real_data_cannot_hold_staff(self):
        real_team = orm_models.Team.objects.filter(league_id=self.league_ids[0]).values_list("id", flat=True).first()
        with self.assertRaises(InvalidWorld):
            self.repository(real_scope()).apply(
                retirements=[], appointments=[Appointment(_newcomer(), real_team, StaffRole.MANAGER, YEAR + 1)]
            )

    def test_clubs_of_other_worlds_and_a_second_manager_are_rejected_without_writing(self):
        real_team = orm_models.Team.objects.filter(league_id=self.league_ids[0]).values_list("id", flat=True).first()
        with self.assertRaises(InvalidStaffTenure):
            self.repository().apply(
                retirements=[], appointments=[Appointment(_newcomer(), real_team, StaffRole.MANAGER, YEAR + 1)]
            )
        with self.assertRaises(InvalidStaffTenure):
            self.repository().apply(
                retirements=[], appointments=[Appointment(_newcomer(), self.teams[0], StaffRole.MANAGER, YEAR + 1)]
            )
        self.assertEqual(self.tenures(), [(team_id, YEAR, None) for team_id in self.teams])
        self.assertEqual(orm_models.PennantStaffMember.objects.count(), len(self.teams))

    def test_closing_and_appointing_together(self):
        (current, *_) = self.repository().find_serving(YEAR + 1)
        self.repository().apply(
            retirements=[current.tenure.close(YEAR + 1)],
            appointments=[Appointment(_newcomer(), current.tenure.team_id, StaffRole.MANAGER, YEAR + 2)],
        )
        serving = {item.tenure.team_id: item.member.name for item in self.repository().find_serving(YEAR + 2)}
        self.assertEqual(serving[current.tenure.team_id], "新監督")
        self.assertEqual(len(serving), len(self.teams))

    def test_reads_are_limited_to_the_world(self):
        self.create_world()
        self.assertEqual(sorted(item.tenure.team_id for item in self.repository().find_serving(YEAR)), self.teams)


class ScreenTest(StaffCase):
    def test_the_home_shows_the_manager_card(self):
        orm_models.PennantStaffMember.objects.filter(tenures__team_id=self.teams[0]).update(
            base_stealing=2, bunting=-1
        )
        response = self.client.get(ws_url("pennant_world", self.world_id))
        card = response.context["manager"]
        self.assertEqual((card.year, card.tenure_year), (YEAR, 1))
        self.assertEqual([cell.title for cell in card.tactics], ["継投", "代打", "盗塁", "犠打"])
        content = response.content.decode()
        self.assertIn(f"監督（{YEAR}年）", content)
        self.assertIn("盗塁が多い", content)
        self.assertIn("犠打はやや少ない", content)

    def test_a_world_without_managers_shows_no_card(self):
        orm_models.PennantStaffTenure.objects.filter(team__league__world_id=self.world_id).delete()
        response = self.client.get(ws_url("pennant_world", self.world_id))
        self.assertIsNone(response.context["manager"])
        self.assertNotIn('id="manager"', response.content.decode())

    def test_the_club_history_and_the_offseason_show_managers(self):
        self.make_manager_old(self.teams[0], birth=date(YEAR + 1 - 72, 1, 1))
        old_name = orm_models.PennantStaffMember.objects.get(tenures__team_id=self.teams[0]).name
        self.finish_regular_season()
        self.close()

        history = self.client.get(ws_url("pennant_team_history", self.world_id, self.teams[0]))
        self.assertEqual(history.status_code, 200)
        ((_, manager),) = history.context["season_rows"]
        self.assertEqual(manager.name, old_name)
        self.assertIn("<th>監督</th>", history.content.decode())

        offseason = self.client.get(ws_url("pennant_offseason", self.world_id, YEAR))
        content = offseason.content.decode()
        self.assertIn("監督の交代（1球団）", content)
        self.assertIn(f"{old_name}（71歳・退任）", content)
