"""シーズンを締めるときの AI の戦力外（#168 段階A）: 退団の経路の保存・翌年の能力・編成の後始末・オフの結果の表記。

世界は `test_pennant_offseason_screens.py` と同じ（球団2つ・1リーグ、シーズン終了まで進めた世界A）。
各23人の球団は帯（62〜66人）の下で、ふつうは戦力外が出ない。戦力外を書く経路を確かめるテストは
`decide_releases` を差し替えて外す選手を決め、引退は `decide_retirements` を差し替えて決める。
人数の規則（新人の余地まで）は domain の `test_release.py` が見る。
"""

from unittest import mock

from django.contrib.messages import get_messages

from myapp.domain.pennant.club_plan import ClubPlan, LineupChoice, PlanSection
from myapp.domain.pennant.draft import MIN_BY_POSITION
from myapp.domain.value_objects import DepartureRoute, FieldingPosition
from myapp.infrastructure import orm_models
from myapp.infrastructure.repositories import DjangoClubPlanRepository, DjangoTeamRepository
from myapp.presentation.views import build_pennant_offseason_service

from ..helpers import world_scope
from .test_pennant_advance import YEAR
from .test_pennant_offseason_screens import DECIDE_RELEASES, DECIDE_RETIREMENTS, ScreenCase


class ReleaseCase(ScreenCase):
    @classmethod
    def close_releasing(cls, released: list[int], retired: list[int] | None = None):
        """`released` を戦力外、`retired` を引退にして締める（ほかは誰も外さない）。"""
        chosen = set(released)

        def decide(club, remaining, **kwargs):
            return tuple(player.player_id for player in remaining if player.player_id in chosen)

        with (
            mock.patch(DECIDE_RETIREMENTS, return_value=tuple(retired or ())),
            mock.patch(DECIDE_RELEASES, side_effect=decide),
        ):
            return build_pennant_offseason_service(world_scope(cls.world_a)).close_season(expected_year=YEAR)

    def stint(self, player_id: int) -> orm_models.PlayerStint:
        return orm_models.PlayerStint.objects.get(player_id=player_id, team__league__world_id=self.world_a)


class ReleasedOnceTest(ReleaseCase):
    """自軍の1人を引退、2人を戦力外にして、クラスで1回だけ締める（重い準備はクラスで1回。#197）。"""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # `managed_players` は世界の id だけを見るので、クラスからも呼べる
        players = ScreenCase.managed_players(cls, 3)
        cls.retired_ids, cls.released_ids = players[:1], players[1:]
        cls.result = cls.close_releasing(cls.released_ids, cls.retired_ids)

    def test_released_players_leave_in_the_closed_year_as_released(self):
        self.assertEqual((self.result.retired_count, self.result.released_count), (1, 2))
        for player_id in self.released_ids:
            stint = self.stint(player_id)
            self.assertEqual((stint.to_year, stint.left_via), (YEAR, DepartureRoute.RELEASED.value))
        retired_stint = self.stint(self.retired_ids[0])
        self.assertEqual((retired_stint.to_year, retired_stint.left_via), (YEAR, DepartureRoute.RETIRED.value))

    def test_released_players_get_no_next_year_ratings_and_stay_in_the_world(self):
        self.assertFalse(
            orm_models.PennantPlayerRatings.objects.filter(player_id__in=self.released_ids, year=YEAR + 1).exists()
        )
        self.assertEqual(
            orm_models.Player.objects.filter(id__in=self.released_ids).count(), 2, "世界に残す（#40 で拾う側）"
        )

    def test_the_departure_route_round_trips_through_the_repository(self):
        teams = DjangoTeamRepository(world_scope(self.world_a)).find_all_with_roster()
        player = next(p for team in teams for p in team.players if p.id == self.released_ids[0])
        self.assertEqual([stint.left_via for stint in player.career], [DepartureRoute.RELEASED])


class ReleaseWritesTest(ReleaseCase):
    def test_rookies_may_take_the_numbers_of_released_players(self):
        """戦力外で空いた背番号は、同じオフの新人に渡せる（在籍を Y で閉じてから新人を足す）。"""
        self.close_releasing(self.managed_players(5))

        team_id = orm_models.PennantWorld.objects.get(id=self.world_a).managed_team_id
        numbers = list(
            orm_models.PlayerStint.objects.filter(team_id=team_id, to_year=None).values_list("number", flat=True)
        )
        self.assertEqual(len(numbers), len(set(numbers)))

    def test_a_manual_plan_with_a_released_player_falls_back_to_auto(self):
        team_id = orm_models.PennantWorld.objects.get(id=self.world_a).managed_team_id
        assert team_id is not None
        players = self.managed_players(12)
        releasing, others = players[0], players[1:]
        DjangoClubPlanRepository(world_scope(self.world_a)).save(
            ClubPlan(
                team_id,
                active_ids=tuple(players),
                lineup=tuple(LineupChoice(player_id, FieldingPosition.DESIGNATED_HITTER) for player_id in others[:9]),
                rotation=tuple(others[:2]),
                closer_id=others[2],
            )
        )

        result = self.close_releasing([releasing])

        self.assertIn(PlanSection.ACTIVE, result.reverted_sections)

    def test_the_real_rule_keeps_the_count_and_the_routes_consistent(self):
        """差し替えない規則で締めても失敗せず、戦力外の人数と「戦力外」の在籍の数が合い、上限を超えない。

        上限を20人に下げて空きを無くす（各23人）。23人の球団は位置の最低人数ぎりぎりなので、最低人数は0にして外せるようにする。
        """
        league = orm_models.League.objects.get(world_id=self.world_a)
        league.registered_player_limit = 20
        league.save()

        with (
            mock.patch(DECIDE_RETIREMENTS, return_value=()),
            mock.patch.dict(MIN_BY_POSITION, dict.fromkeys(MIN_BY_POSITION, 0)),
        ):
            result = build_pennant_offseason_service(world_scope(self.world_a)).close_season(expected_year=YEAR)

        released = orm_models.PlayerStint.objects.filter(
            team__league__world_id=self.world_a, to_year=YEAR, left_via=DepartureRoute.RELEASED.value
        )
        self.assertGreater(result.released_count, 0)
        self.assertEqual(released.count(), result.released_count)
        self.assertFalse(
            orm_models.PennantPlayerRatings.objects.filter(
                player_id__in=released.values("player_id"), year=YEAR + 1
            ).exists()
        )
        teams = DjangoTeamRepository(world_scope(self.world_a)).find_all_with_roster()
        self.assertTrue(all(team.registered_player_count <= 20 for team in teams), "上限を超えない")


class ReleaseMessageAndResultTest(ReleaseCase):
    def test_the_message_counts_released_players(self):
        self.client.force_login(self.owner)
        released = self.managed_players(2)

        def decide(club, remaining, **kwargs):
            return tuple(player.player_id for player in remaining if player.player_id in released)

        with mock.patch(DECIDE_RETIREMENTS, return_value=()), mock.patch(DECIDE_RELEASES, side_effect=decide):
            response = self.post_close()

        told = " ".join(str(message) for message in get_messages(response.wsgi_request))
        self.assertIn("引退 0人・戦力外 2人・新人", told)

    def test_the_result_lists_released_players_as_released(self):
        released = self.managed_players(2)[1:]
        retired = self.managed_players(1)
        self.close_releasing(released, retired)

        content = self.client.get(self.offseason_url()).content.decode()

        self.assertIn("引退・退団・戦力外（2人）", content)
        self.assertIn("<td>戦力外</td>", content)
        self.assertIn("（うち戦力外 1人）", content)

    def test_a_departure_without_a_route_keeps_the_old_label(self):
        """経路を記録する前に締めた年（left_via が空）は、今までどおり外国人は「退団」、日本人は「引退」。"""
        released = self.managed_players(1)
        self.close_releasing(released)
        orm_models.PlayerStint.objects.filter(player_id=released[0]).update(left_via=None)
        foreign = orm_models.Player.objects.get(id=released[0]).is_foreign_player

        content = self.client.get(self.offseason_url()).content.decode()

        self.assertNotIn("<td>戦力外</td>", content)
        self.assertIn("<td>退団</td>" if foreign else "<td>引退</td>", content)
        self.assertIn("（うち戦力外 0人）", content)
