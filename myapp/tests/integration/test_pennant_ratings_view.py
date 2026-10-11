"""ペナントの能力の表示（P4c）。選手ページの能力のカードと、球団の画面の能力の表。

区分と強調の対応そのものは domain のテスト（`tests/domain/test_rating_sorting.py`）。ここでは
世界の画面に出ること・並べ替えが効くこと・実データの画面には何も出ないことを確かめる。
"""

import re
from datetime import date

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from myapp.domain.pennant.schedule import Fixture
from myapp.domain.pennant.world import WorldScope
from myapp.infrastructure import orm_models
from myapp.infrastructure.repositories import DjangoFixtureRepository, DjangoRatingsRepository
from myapp.presentation.views import build_club_service, build_pennant_ratings_service, build_pennant_world_service

from .world_case import YEAR, WorldCase


class RatingsRepositoryByPlayersTest(WorldCase):
    def test_it_reads_only_the_given_players_in_one_query(self):
        repository = DjangoRatingsRepository(self.scope)
        ids = self.pennant_players(self.pennant_team)

        with CaptureQueriesContext(connection) as queries:
            loaded = repository.find_by_players(ids, YEAR)

        self.assertEqual(len(queries), 1)
        self.assertEqual({item.player_id for item in loaded}, set(ids))
        self.assertEqual([item.player_id for item in loaded], sorted(item.player_id for item in loaded))

    def test_a_player_outside_the_world_is_not_read(self):
        repository = DjangoRatingsRepository(self.scope)

        self.assertEqual(repository.find_by_players([self.real_batters[0]], YEAR), [])
        self.assertEqual(repository.find_by_players([], YEAR), [])


class RatingsServiceTest(WorldCase):
    def setUp(self):
        super().setUp()
        self.ratings_service = build_pennant_ratings_service(self.world_id)
        players = self.pennant_players(self.pennant_team)
        self.batter_id, self.pitcher_id = players[0], players[9]

    def test_the_table_reads_the_ratings_of_a_team_in_a_fixed_number_of_queries(self):
        with CaptureQueriesContext(connection) as queries:
            table = self.ratings_service.get_table(self.pennant_team.id, pitchers=False)

        self.assertEqual(len(table.rows), 9)
        rating_queries = [q for q in queries if "pennantplayerratings" in q["sql"].replace("_", "").lower()]
        self.assertEqual(len(rating_queries), 1, "選手ごとに能力を引かない")

    def test_a_card_exists_for_a_player_with_ratings_and_not_otherwise(self):
        card = self.ratings_service.get_card(self.batter_id)
        assert card is not None
        self.assertEqual((card.year, card.is_pitcher), (YEAR, False))
        self.assertEqual([cell.label for cell in card.cells], ["ミート", "パワー", "選球眼", "走力", "守備力"])

        orm_models.PennantPlayerRatings.objects.filter(player_id=self.batter_id).delete()
        self.assertIsNone(self.ratings_service.get_card(self.batter_id))

    def test_a_pitcher_has_four_ratings(self):
        card = self.ratings_service.get_card(self.pitcher_id)
        assert card is not None
        self.assertEqual([cell.label for cell in card.cells], ["球威", "制球", "一発回避", "スタミナ"])

    def _add_next_year_ratings(self, contact: int) -> None:
        row = orm_models.PennantPlayerRatings.objects.get(player_id=self.batter_id, year=YEAR)
        row.pk = None
        row.year = YEAR + 1
        row.contact = contact
        row.save()

    def test_the_year_follows_the_rule_not_the_newest_row(self):
        """翌年の能力ができても、試合の年（まだ開幕年）のうちは開幕年の能力を見せる。"""
        before = self.ratings_service.get_card(self.batter_id)
        assert before is not None
        self._add_next_year_ratings(99)

        card = self.ratings_service.get_card(self.batter_id)

        assert card is not None
        self.assertEqual(card.year, YEAR)
        self.assertEqual(card.cells, before.cells)

    def test_the_display_and_the_club_management_use_the_same_year(self):
        club = build_club_service(self.world_id)

        self.assertEqual(self.ratings_service.current_year(), club._current_year())
        self.assertEqual(self.ratings_service.current_year(), YEAR)

    def test_the_year_moves_with_the_schedule_in_both(self):
        """試合が進んだあとも、表示と編成は同じ年を指す。"""
        club = build_club_service(self.world_id)
        orm_models.PennantFixture.objects.all().delete()
        self.assertEqual(self.ratings_service.current_year(), club._current_year())

    def test_a_player_without_ratings_in_that_year_is_dashed(self):
        orm_models.PennantPlayerRatings.objects.filter(player_id=self.batter_id).update(year=YEAR - 1)

        self.assertIsNone(self.ratings_service.get_card(self.batter_id))
        table = self.ratings_service.get_table(self.pennant_team.id, pitchers=False)
        self.assertEqual(table.year, YEAR)
        self.assertEqual([row.cells for row in table.rows if row.id == self.batter_id], [()])

    def test_another_world_cannot_read_the_ratings(self):
        other = build_pennant_world_service().create_world(
            name="別の世界", owner_id=None, source_league_ids=[self.league.id], start_year=YEAR, seed=9
        )
        other_service = build_pennant_ratings_service(other.world.id)

        self.assertIsNone(other_service.get_card(self.batter_id))
        self.assertEqual(
            DjangoRatingsRepository(WorldScope.pennant(other.world.id)).find_by_players([self.batter_id], YEAR), []
        )
        self.assertEqual(DjangoRatingsRepository(WorldScope.real()).find_by_players([self.batter_id], YEAR), [])


class PlayerDetailCardTest(WorldCase):
    def setUp(self):
        super().setUp()
        players = self.pennant_players(self.pennant_team)
        self.batter_id, self.pitcher_id = players[0], players[9]
        self.batter_url = reverse("pennant_player_detail", args=[self.world_id, self.pennant_team.id, self.batter_id])
        self.pitcher_url = reverse(
            "pennant_player_detail", args=[self.world_id, self.pennant_team.id, self.pitcher_id]
        )

    def test_the_card_shows_the_grade_and_the_value(self):
        orm_models.PennantPlayerRatings.objects.filter(player_id=self.batter_id).update(
            contact=95, power=74, eye=50, speed=45, fielding=10
        )

        content = self.client.get(self.batter_url).content.decode()

        self.assertIn(f"能力（{YEAR}年度）", content)
        for label in ("ミート", "パワー", "選球眼", "走力", "守備力"):
            self.assertIn(label, content)
        self.assertIn('<strong class="stat-tier-high">S</strong> <span class="stat-muted">95</span>', content)
        self.assertIn('<strong class="stat-tier-mid">B</strong> <span class="stat-muted">74</span>', content)
        self.assertIn('<strong class="">D</strong> <span class="stat-muted">50</span>', content)
        self.assertIn('<strong class="stat-muted">E</strong> <span class="stat-muted">45</span>', content)
        self.assertIn('<strong class="stat-muted">G</strong> <span class="stat-muted">10</span>', content)
        self.assertIn("能力の見方", content)

    def test_a_pitcher_shows_pitching_ratings(self):
        content = self.client.get(self.pitcher_url).content.decode()

        for label in ("球威", "制球", "一発回避", "スタミナ"):
            self.assertIn(label, content)
        card = content.split("能力（")[1].split("能力の見方")[0]
        self.assertNotIn("ミート", card)

    def test_the_hidden_growth_type_is_not_shown(self):
        content = self.client.get(self.batter_url).content.decode()

        for growth in ("早熟", "晩成"):
            self.assertNotIn(growth, content)

    def test_a_player_without_ratings_still_opens_without_a_card(self):
        orm_models.PennantPlayerRatings.objects.filter(player_id=self.batter_id).delete()

        response = self.client.get(self.batter_url)

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("能力（", response.content.decode())

    def test_the_real_players_page_has_no_card(self):
        url = reverse("player_detail", args=[self.team.id, self.real_batters[0]])

        content = self.client.get(url).content.decode()

        self.assertNotIn("能力（", content)
        self.assertNotIn("能力の見方", content)


class TeamRatingsTableTest(WorldCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("pennant_player_list", args=[self.world_id, self.pennant_team.id])
        self.players = self.pennant_players(self.pennant_team)
        # 野手 9人のミートを背番号の順に高くそろえる（背番号 1 が最低、9 が最高）
        for offset, player_id in enumerate(self.players[:9]):
            orm_models.PennantPlayerRatings.objects.filter(player_id=player_id).update(contact=30 + offset * 5)

    @staticmethod
    def _names(content: str) -> list[str]:
        return list(dict.fromkeys(re.findall(r"目印選手\d\d", content)))

    def test_the_default_view_is_the_stats_table_with_a_switch(self):
        content = self.client.get(self.url).content.decode()

        self.assertIn("成績</a>", content)
        self.assertIn("view=ratings", content)
        self.assertNotIn("ミート", content)
        self.assertIn("OPS", content)

    def test_the_ratings_view_shows_the_columns_and_the_grades(self):
        content = self.client.get(self.url + "?view=ratings").content.decode()

        for label in ("背番号", "選手名", "年齢", "ミート", "パワー", "選球眼", "走力", "守備力", "打率", "OPS"):
            self.assertIn(label, content)
        self.assertIn(f"能力は{YEAR}年度のものです", content)
        self.assertNotIn("月別成績", content, "能力の表では月別成績を出さない")

    def test_it_is_ordered_by_the_jersey_number_by_default(self):
        rows = self.client.get(self.url + "?view=ratings").context["ratings_table"].rows

        self.assertEqual([row.number for row in rows], sorted(row.number for row in rows))

    def test_sorting_by_a_rating_puts_the_highest_first(self):
        response = self.client.get(self.url + "?view=ratings&sort=contact")

        rows = response.context["ratings_table"].rows
        self.assertEqual([row.number for row in rows], sorted((row.number for row in rows), reverse=True))
        self.assertEqual(response.context["current_sort"], "contact")
        self.assertTrue(response.context["current_descending"])

    def test_the_direction_can_be_reversed(self):
        rows = self.client.get(self.url + "?view=ratings&sort=contact&dir=asc").context["ratings_table"].rows

        self.assertEqual([row.number for row in rows], sorted(row.number for row in rows))

    def test_a_bad_sort_key_falls_back_to_the_default(self):
        response = self.client.get(self.url + "?view=ratings&sort=no_such_key&dir=desc")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "number")

    def test_pitchers_have_their_own_columns(self):
        content = self.client.get(self.url + "?view=ratings&pos=pitcher").content.decode()

        for label in ("球威", "制球", "一発回避", "スタミナ", "防御率", "投球回"):
            self.assertIn(label, content)
        self.assertNotIn("ミート", content)

    def test_the_position_switch_keeps_the_view(self):
        content = self.client.get(self.url + "?view=ratings").content.decode()

        self.assertIn('href="?view=ratings&amp;pos=pitcher"', content)

    def test_a_player_without_ratings_gets_dashes_and_goes_last(self):
        orm_models.PennantPlayerRatings.objects.filter(player_id=self.players[8]).delete()

        response = self.client.get(self.url + "?view=ratings&sort=contact")

        self.assertEqual(response.status_code, 200)
        rows = response.context["ratings_table"].rows
        self.assertEqual(rows[-1].cells, ())
        self.assertIn("—", response.content.decode())

    def test_the_other_teams_ratings_are_visible(self):
        url = reverse("pennant_player_list", args=[self.world_id, self.pennant_rival.id])

        response = self.client.get(url + "?view=ratings")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["ratings_table"].rows), 9)

    def test_the_real_data_ignores_the_ratings_view(self):
        real_url = reverse("player_list", args=[self.team.id])

        plain = self.client.get(real_url).content.decode()
        response = self.client.get(real_url + "?view=ratings")

        self.assertIsNone(response.context["ratings_table"])
        self.assertNotIn("ミート", response.content.decode())
        self.assertNotIn("view=ratings", plain, "実データの画面には成績｜能力の切り替えを出さない")
        self.assertNotIn("能力", plain)

    def test_the_names_link_stays_in_the_world(self):
        content = self.client.get(self.url + "?view=ratings").content.decode()

        self.assertEqual(len(self._names(content)), 9)
        self.assertIn(f"/pennant/{self.world_id}/", content)


class FixtureFirstDateTest(WorldCase):
    def setUp(self):
        super().setUp()
        self.repository = DjangoFixtureRepository(self.scope)
        orm_models.PennantFixture.objects.all().delete()

    def test_it_returns_the_first_date_in_one_query(self):
        self.repository.add_all(
            [
                Fixture(
                    date=date(YEAR + 1, 4, 3), home_team_id=self.pennant_team.id, visitor_team_id=self.pennant_rival.id
                ),
                Fixture(
                    date=date(YEAR + 1, 3, 29),
                    home_team_id=self.pennant_rival.id,
                    visitor_team_id=self.pennant_team.id,
                ),
            ]
        )

        with CaptureQueriesContext(connection) as queries:
            first = self.repository.first_date()

        self.assertEqual(first, date(YEAR + 1, 3, 29))
        self.assertEqual(len(queries), 1)

    def test_a_world_without_a_schedule_has_no_first_date(self):
        self.assertIsNone(self.repository.first_date())

    def test_the_year_of_the_next_game_is_the_year_both_screens_use(self):
        """日程が翌年にまたがるなら、表示も編成も翌年を指す（その年の能力が無ければ「—」）。"""
        self.repository.add_all(
            [
                Fixture(
                    date=date(YEAR + 1, 3, 29),
                    home_team_id=self.pennant_team.id,
                    visitor_team_id=self.pennant_rival.id,
                )
            ]
        )

        service = build_pennant_ratings_service(self.world_id)

        self.assertEqual(service.current_year(), YEAR + 1)
        self.assertEqual(build_club_service(self.world_id)._current_year(), YEAR + 1)
        table = service.get_table(self.pennant_team.id, pitchers=False)
        self.assertEqual(table.year, YEAR + 1)
        self.assertTrue(all(row.cells == () for row in table.rows))
