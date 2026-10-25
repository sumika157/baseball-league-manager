"""シーズンを締めた後の支配下の人数と、新人の入団経路（#167）。育成ドラフトの新人（#198 D2）。

世界は `test_pennant_offseason.py` と同じ（球団2つ・各23人、シーズン終了まで進めた世界A）。引退は乱数で決まるので、
人数を突き合わせるテストは `decide_retirements` と `decide_releases` を差し替えて誰も引退・戦力外にしない
（戦力外の規則は `test_pennant_release.py`）。
"""

from unittest import mock

from myapp.domain.pennant.draft import development_draftee_count, development_target, draftee_count
from myapp.domain.pennant.schedule import AdvanceTarget
from myapp.domain.pennant.world import WorldScope
from myapp.domain.value_objects import AcquisitionRoute, ContractStatus
from myapp.infrastructure import orm_models
from myapp.infrastructure.repositories import DjangoTeamRepository
from myapp.presentation.views import build_pennant_season_service

from .test_pennant_advance import YEAR
from .test_pennant_offseason import DECIDE_RELEASES, DECIDE_RETIREMENTS, ClosedSeasonCase, active_stints


class RegisteredRosterAfterCloseTest(ClosedSeasonCase):
    def registered_counts(self) -> dict[int, int]:
        """球団ごとの支配下の人数（集約の判定 `Team.registered_player_count`）。"""
        teams = DjangoTeamRepository(WorldScope.pennant(self.world_a)).find_all_with_roster()
        return {team.id: team.registered_player_count for team in teams if team.id is not None}

    def league(self) -> orm_models.League:
        return orm_models.League.objects.get(world_id=self.world_a)

    def rookies_of(self, team_id: int):
        """支配下で入った新人（本指名と外国人）。"""
        return active_stints(self.world_a).filter(
            team_id=team_id, from_year=YEAR + 1, signed_as=ContractStatus.REGISTERED.value
        )

    def development_rookies_of(self, team_id: int):
        """育成ドラフトで入った新人。"""
        return active_stints(self.world_a).filter(
            team_id=team_id, from_year=YEAR + 1, signed_as=ContractStatus.DEVELOPMENTAL.value
        )

    def test_each_club_drafts_toward_the_opening_band(self):
        before = self.registered_counts()
        limit = self.league().registered_player_limit

        with mock.patch(DECIDE_RETIREMENTS, return_value=()), mock.patch(DECIDE_RELEASES, return_value=()):
            self.close()

        after = self.registered_counts()
        for team_id, registered in before.items():
            with self.subTest(team_id=team_id):
                expected = draftee_count(registered, registered_limit=limit)
                self.assertGreater(expected, 0, "前提: 各23人なので帯（62〜66人）の下")
                self.assertEqual(self.rookies_of(team_id).count(), expected)
                self.assertEqual(after[team_id], registered + expected)

    def test_rookies_record_how_they_joined(self):
        """日本人の本指名は「ドラフト」、外国人は「新外国人」で支配下。育成ドラフトは「育成ドラフト」で育成（3桁）。"""
        self.close()

        rookies = active_stints(self.world_a).filter(from_year=YEAR + 1).select_related("player")
        self.assertTrue(rookies.exists())
        routes = set()
        for stint in rookies:
            if stint.player.is_development_draft:
                self.assertEqual(stint.acquired_via, AcquisitionRoute.DEVELOPMENTAL_DRAFT.value)
                self.assertEqual(stint.signed_as, ContractStatus.DEVELOPMENTAL.value)
                self.assertGreaterEqual(int(stint.number), 100)
            else:
                expected = AcquisitionRoute.NEW_FOREIGN if stint.player.is_foreign_player else AcquisitionRoute.DRAFT
                self.assertEqual(stint.acquired_via, expected.value, stint.player.name)
                self.assertEqual(stint.signed_as, ContractStatus.REGISTERED.value)
            routes.add(stint.acquired_via)
        self.assertEqual(
            routes,
            {
                AcquisitionRoute.DRAFT.value,
                AcquisitionRoute.NEW_FOREIGN.value,
                AcquisitionRoute.DEVELOPMENTAL_DRAFT.value,
            },
        )

    def test_development_rookies_fill_toward_the_target_without_counting_as_registered(self):
        """育成ドラフト: 球団ごとの目標までの不足（1年6人まで）を育成で迎える。支配下の人数と上限には数えない。"""
        managed = self.managed_team_id()
        for offset, stint in enumerate(active_stints(self.world_a).filter(team_id=managed).order_by("id")[:5]):
            stint.signed_as = ContractStatus.DEVELOPMENTAL.value
            stint.number = str(101 + offset)
            stint.save()
        before = self.registered_counts()
        seed = orm_models.PennantWorld.objects.get(id=self.world_a).seed
        with mock.patch(DECIDE_RETIREMENTS, return_value=()), mock.patch(DECIDE_RELEASES, return_value=()):
            result = self.close()

        after = self.registered_counts()
        total = 0
        for team_id, registered in before.items():
            with self.subTest(team_id=team_id):
                current = 5 if team_id == managed else 0
                expected = development_draftee_count(current, target=development_target(team_id, world_seed=seed))
                self.assertGreater(expected, 0)
                rookies = self.development_rookies_of(team_id).select_related("player")
                self.assertEqual(rookies.count(), expected)
                self.assertEqual(sorted(stint.player.draft_round for stint in rookies), list(range(1, expected + 1)))
                self.assertTrue(all(stint.player.draft_year == YEAR for stint in rookies))
                self.assertEqual(after[team_id] - registered, self.rookies_of(team_id).count())
                total += expected
        self.assertEqual(result.development_draftee_count, total)

    def test_developmental_players_are_not_counted(self):
        """育成5人を含む球団: 支配下だけで数えるので、リーグの上限（支配下＋3人）まで迎える。

        育成を数えると在籍の全員（23人）が上限（21人）を超え、新人は0人になる。
        """
        team_id = self.managed_team_id()
        for offset, stint in enumerate(active_stints(self.world_a).filter(team_id=team_id).order_by("id")[:5]):
            stint.signed_as = ContractStatus.DEVELOPMENTAL.value
            stint.number = str(101 + offset)
            stint.save()
        registered = self.registered_counts()[team_id]
        self.assertEqual(registered, 18, "前提: 23人のうち5人が育成")
        league = self.league()
        league.registered_player_limit = registered + 3
        league.save()

        with mock.patch(DECIDE_RETIREMENTS, return_value=()), mock.patch(DECIDE_RELEASES, return_value=()):
            self.close()

        self.assertEqual(self.rookies_of(team_id).count(), 3)
        self.assertEqual(self.registered_counts()[team_id], registered + 3)

    def test_clubs_already_over_the_league_limit_close_without_rookies(self):
        """分岐元が上限を超えていた球団（分岐では検査しない）: 締める処理は失敗せず、新人は0人。"""
        before = self.registered_counts()
        league = self.league()
        league.registered_player_limit = 20
        league.save()
        self.assertTrue(all(count > 20 for count in before.values()), "前提: どの球団も上限を超えている")

        with mock.patch(DECIDE_RETIREMENTS, return_value=()), mock.patch(DECIDE_RELEASES, return_value=()):
            result = self.close()

        self.assertEqual(result.draftee_count, result.development_draftee_count, "育成は上限に数えない")
        self.assertFalse(
            active_stints(self.world_a).filter(from_year=YEAR + 1, signed_as=ContractStatus.REGISTERED.value).exists()
        )
        self.assertEqual(self.registered_counts(), before)

    def test_the_league_limit_is_not_exceeded_over_seasons(self):
        """上限で失敗させない: 2シーズン締めても、支配下はリーグの上限を超えない（新人は上限までで止まる）。"""
        league = self.league()
        league.registered_player_limit = 25
        league.save()

        self.close()
        self.assertTrue(all(count <= 25 for count in self.registered_counts().values()), self.registered_counts())
        build_pennant_season_service(self.world_a).advance(AdvanceTarget.SEASON_END)
        result = self.close(expected_year=YEAR + 1)

        self.assertEqual(result.year, YEAR + 1)
        self.assertTrue(all(count <= 25 for count in self.registered_counts().values()), self.registered_counts())
