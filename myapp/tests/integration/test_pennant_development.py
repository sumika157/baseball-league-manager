"""締める処理の育成の昇格と育成契約の満了（#170 P1）と、オフの結果の画面。

世界は `test_pennant_offseason.py` と同じ（球団2つ・各23人、シーズン終了まで進めた世界A）。分岐元に育成がいないので、
受け持つ球団に育成を3人足してから締める（見込みの高い育成・満了の弱い育成・2年目の弱い育成）。
引退は乱数で決まるので差し替えて誰も引退させない。昇格と満了の規則そのものは `tests/domain/test_development.py`。
"""

from datetime import date
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse

from myapp.domain.pennant.ratings import PlayerRatings
from myapp.domain.pennant.world import WorldScope
from myapp.domain.simulation.ratings import PitcherRatings
from myapp.domain.value_objects import AcquisitionRoute, ContractStatus, DepartureRoute, Position
from myapp.infrastructure import orm_models
from myapp.infrastructure.queries import DjangoSimulationContextQuery
from myapp.infrastructure.repositories import DjangoRatingsRepository

from .test_pennant_advance import YEAR
from .test_pennant_offseason import DECIDE_RETIREMENTS, ClosedSeasonCase


class DevelopmentCloseCase(ClosedSeasonCase):
    strong: int
    expiring: int
    second_year: int

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        team = orm_models.Team.objects.get(id=cls._managed_team_id(cls.world_a))
        cls.strong = cls._add_development(team, "育成強", "150", draft_year=YEAR - 1, value=85)
        # 育成ドラフトの4年後（満了の年を過ぎて再契約した選手も、毎年が満了の年）で、見込みの低い育成
        cls.expiring = cls._add_development(team, "育成満了", "151", draft_year=YEAR - 4, value=10)
        # 2年目（満了の年でない）の見込みの低い育成は残る
        cls.second_year = cls._add_development(team, "育成二年", "152", draft_year=YEAR - 1, value=10)

    @staticmethod
    def _managed_team_id(world_id: int) -> int:
        world = orm_models.PennantWorld.objects.get(id=world_id)
        assert world.managed_team_id is not None
        return world.managed_team_id

    @classmethod
    def _add_development(cls, team, name: str, number: str, *, draft_year: int, value: int) -> int:
        """30歳の育成の投手（見込み＝今の能力）を、育成ドラフトの翌年から在籍させ、締める年の能力を付ける。"""
        player = orm_models.Player.objects.create(
            name=name,
            position=Position.PITCHER.label,
            birth_date=date(YEAR - 30, 1, 1),
            draft_year=draft_year,
            draft_round=1,
            is_development_draft=True,
        )
        orm_models.PlayerStint.objects.create(
            player=player,
            team=team,
            number=number,
            from_year=draft_year + 1,
            signed_as=ContractStatus.DEVELOPMENTAL.value,
            acquired_via=AcquisitionRoute.DEVELOPMENTAL_DRAFT.value,
        )
        DjangoRatingsRepository(WorldScope.pennant(cls.world_a)).add_all(
            [PlayerRatings(player.id, YEAR, PitcherRatings(value, value, value, 50))]
        )
        return player.id

    def stint(self, player_id: int) -> orm_models.PlayerStint:
        return orm_models.PlayerStint.objects.get(player_id=player_id)

    def close_without_retirements(self):
        with mock.patch(DECIDE_RETIREMENTS, return_value=()):
            return self.close()


class DevelopmentCloseTest(DevelopmentCloseCase):
    def test_a_strong_prospect_is_promoted_from_the_next_year(self):
        result = self.close_without_retirements()

        stint = self.stint(self.strong)
        self.assertGreaterEqual(result.promoted_count, 1)
        self.assertEqual(stint.promoted_year, YEAR + 1, "オフの昇格は翌年から（年を渡し忘れると今日の年になる）")
        self.assertEqual(stint.number_before_promotion, "150")
        self.assertLessEqual(int(stint.number), 99)
        self.assertIsNone(stint.to_year)
        # 翌年の試合の候補に入る（育成は入らない）
        candidates = {
            player.player_id
            for team in DjangoSimulationContextQuery(WorldScope.pennant(self.world_a)).teams()
            for player in team.players
        }
        self.assertIn(self.strong, candidates)
        self.assertNotIn(self.second_year, candidates)
        numbers = orm_models.PlayerStint.objects.filter(team_id=stint.team_id, to_year=None).values_list(
            "number", flat=True
        )
        self.assertEqual(len(numbers), len(set(numbers)), "昇格後の背番号は重ならない")

    def test_an_expiring_weak_prospect_is_released_and_a_second_year_one_stays(self):
        result = self.close_without_retirements()

        expired = self.stint(self.expiring)
        self.assertEqual(result.expired_count, 1)
        self.assertEqual(expired.to_year, YEAR)
        self.assertEqual(expired.left_via, DepartureRoute.RELEASED.value)
        stays = self.stint(self.second_year)
        self.assertIsNone(stays.to_year)
        self.assertIsNone(stays.promoted_year)

    def test_the_offseason_page_lists_the_promotion_and_badges_the_expired_prospect(self):
        self.close_without_retirements()

        content = self.client.get(reverse("pennant_offseason", args=[self.world_a, YEAR])).content.decode()
        self.assertIn("支配下登録（昇格）", content)
        self.assertIn('data-testid="promoted-row"', content)
        self.assertIn("育成強", content)
        self.assertIn('data-testid="development-badge"', content)

    def test_the_close_message_reports_promotions_and_expirations(self):
        owner = User.objects.create_user("development-owner")
        orm_models.PennantWorld.objects.filter(id=self.world_a).update(owner_id=owner.id)
        self.client.force_login(owner)
        with mock.patch(DECIDE_RETIREMENTS, return_value=()):
            response = self.client.post(
                reverse("pennant_close_season", args=[self.world_a]), {"expected_year": YEAR}, follow=True
            )
        self.assertContains(response, "支配下登録（昇格）")
        self.assertContains(response, "育成の自由契約 1人")
