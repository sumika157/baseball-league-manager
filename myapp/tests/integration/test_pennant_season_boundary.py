"""年をまたぐ世界の局面と年度、出場機会の参照（P6b1）。

シーズンを締める処理（P6b2）はまだ無いので、翌年の日程は ORM で直接足して「締めた直後」を作る。
1シーズン目の見え方が変わらないことは、既存のテスト（`test_pennant_screens.py` ほか）が守る。
"""

from datetime import date

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from myapp.application.queries import SeasonPlayingTimeQuery
from myapp.domain.pennant.retirement import PlayingTime
from myapp.domain.pennant.season import SeasonPhase
from myapp.domain.pennant.world import WorldScope
from myapp.domain.value_objects import InningsPitched
from myapp.infrastructure import orm_models
from myapp.infrastructure.queries import DjangoSeasonPlayingTimeQuery
from myapp.presentation.views import build_pennant_world_service, build_pennant_world_view

from .world_case import YEAR, WorldCase

NEXT_OPENING = date(YEAR + 1, 3, 26)


class NextYearScheduleTest(WorldCase):
    """世界の試合は YEAR の4月2日の1試合だけ。そこへ翌年の日程だけを足す（締めた直後の形）。"""

    def add_next_year(self) -> None:
        orm_models.PennantFixture.objects.create(
            date=NEXT_OPENING, home_team=self.pennant_team, visitor_team=self.pennant_rival
        )

    def test_before_the_next_schedule_the_season_is_finished(self):
        orm_models.PennantFixture.objects.all().delete()

        context = build_pennant_world_view().get_context(self.world_id)

        self.assertIs(context.phase, SeasonPhase.FINISHED)
        self.assertEqual(context.season_year, YEAR)

    def test_the_next_years_schedule_means_before_opening_of_the_next_year(self):
        orm_models.PennantFixture.objects.all().delete()
        self.add_next_year()

        context = build_pennant_world_view().get_context(self.world_id)

        self.assertIs(context.phase, SeasonPhase.BEFORE_OPENING)
        self.assertEqual(context.season_year, YEAR + 1)
        self.assertEqual(context.today, date(YEAR, 4, 2), "今日は前年の最終日のまま")

    def test_the_home_and_the_world_bar_show_the_next_year(self):
        orm_models.PennantFixture.objects.all().delete()
        self.add_next_year()

        content = self.client.get(reverse("pennant_world", args=[self.world_id])).content.decode()

        self.assertIn(f"{YEAR + 1}年 · 開幕前", content)
        self.assertNotIn("シーズン中", content)

    def test_the_world_list_shows_the_next_year_without_a_standing(self):
        orm_models.PennantFixture.objects.all().delete()
        self.add_next_year()
        orm_models.PennantWorld.objects.filter(id=self.world_id).update(managed_team_id=self.pennant_team.id)

        (row,) = [r for r in build_pennant_world_view().list_rows() if r.context.world_id == self.world_id]

        self.assertEqual(row.context.season_year, YEAR + 1)
        self.assertIs(row.context.phase, SeasonPhase.BEFORE_OPENING)
        self.assertIsNone(row.own_standing, "試合の無い年の順位は出さない")
        content = self.client.get(reverse("pennant_index")).content.decode()
        self.assertIn(f"{YEAR + 1}年 · 開幕前", content)

    def test_the_first_season_still_shows_the_season_in_progress(self):
        """同じ年の日程が残っていれば、いままでどおりシーズン中・その年。"""
        orm_models.PennantFixture.objects.create(
            date=date(YEAR, 4, 3), home_team=self.pennant_team, visitor_team=self.pennant_rival
        )

        context = build_pennant_world_view().get_context(self.world_id)

        self.assertIs(context.phase, SeasonPhase.IN_SEASON)
        self.assertEqual(context.season_year, YEAR)


class SeasonPlayingTimeTest(WorldCase):
    def query(self) -> DjangoSeasonPlayingTimeQuery:
        return DjangoSeasonPlayingTimeQuery(self.scope)

    def test_it_is_a_season_playing_time_query(self):
        self.assertIsInstance(self.query(), SeasonPlayingTimeQuery)

    def test_plate_appearances_are_counted_per_batter(self):
        expected: dict[int, int] = {}
        for batter_id in orm_models.GamePlateAppearance.objects.filter(game_id=self.pennant_game_id).values_list(
            "batter_id", flat=True
        ):
            expected[batter_id] = expected.get(batter_id, 0) + 1

        result = self.query().for_year(YEAR)

        self.assertTrue(expected)
        for player_id, count in expected.items():
            self.assertEqual(result[player_id].plate_appearances, count)

    def test_outs_come_from_the_innings_pitched(self):
        lines = orm_models.GamePitchingLine.objects.filter(game_id=self.pennant_game_id)
        self.assertTrue(lines.exists())

        result = self.query().for_year(YEAR)

        for line in lines:
            self.assertEqual(result[line.player_id].outs, InningsPitched.from_notation(line.innings_pitched).outs)
        self.assertGreater(sum(r.outs for r in result.values()), 0)

    def test_a_player_who_neither_batted_nor_pitched_is_left_out(self):
        bench = orm_models.Player.objects.filter(stints__team=self.pennant_team).exclude(
            id__in=self.pennant_home_batters
        )
        pitcher_ids = set(orm_models.GamePitchingLine.objects.values_list("player_id", flat=True))
        result = self.query().for_year(YEAR)

        for player in bench:
            if player.id not in pitcher_ids:
                self.assertNotIn(player.id, result)

    def test_another_year_is_not_counted(self):
        self.assertEqual(self.query().for_year(YEAR + 1), {})
        self.assertNotEqual(self.query().for_year(YEAR), {})

    def test_the_real_data_and_other_worlds_are_not_counted(self):
        """実データと別の世界に、同じ年の打撃の明細があっても、この世界の結果には入らない。"""
        real_player = orm_models.GameBattingLine.objects.get(game_id=self.real_game.id).player_id
        other = build_pennant_world_service().create_world(
            name="別の世界", owner_id=None, source_league_ids=[self.league.id], start_year=YEAR, seed=2
        )
        other_teams = list(orm_models.Team.objects.filter(league__world_id=other.world.id).order_by("id"))
        other_game = orm_models.Game.objects.create(
            year=YEAR,
            played_on=date(YEAR, 4, 2),
            home_team=other_teams[0],
            away_team=other_teams[1],
            home_score=1,
            away_score=0,
        )
        other_player = orm_models.PlayerStint.objects.filter(team=other_teams[0]).first().player_id
        orm_models.GameBattingLine.objects.create(
            game=other_game, player_id=other_player, team=other_teams[0], batting_order=1, at_bats=4, singles=1
        )
        self.assertEqual(
            DjangoSeasonPlayingTimeQuery(WorldScope.pennant(other.world.id))
            .for_year(YEAR)[other_player]
            .plate_appearances,
            4,
        )

        result = self.query().for_year(YEAR)

        self.assertNotIn(real_player, result)
        self.assertNotIn(other_player, result)
        world_players = set(
            orm_models.Player.objects.filter(stints__team__league=self.pennant_league).values_list("id", flat=True)
        )
        self.assertTrue(result)
        self.assertTrue(set(result) <= world_players)

    def test_plate_appearances_follow_the_batting_line(self):
        """打席数の出典は `BattingLine.plate_appearances`（打数＋四球＋死球＋犠飛＋犠打）。"""
        player = self.pennant_home_batters[0]
        before = self.query().for_year(YEAR)[player].plate_appearances
        original = orm_models.GameBattingLine.objects.get(game_id=self.pennant_game_id, player_id=player)
        orm_models.GameBattingLine.objects.filter(game_id=self.pennant_game_id, player_id=player).update(
            walks=2, sacrifice_bunts=1
        )
        after = self.query().for_year(YEAR)[player].plate_appearances
        line = orm_models.GameBattingLine.objects.get(game_id=self.pennant_game_id, player_id=player)

        self.assertEqual(
            after, line.at_bats + line.walks + line.hit_by_pitch + line.sacrifice_flies + line.sacrifice_bunts
        )
        # 四球と犠打を書き換えた分だけ増減する（その選手のこの試合以外の打席は変わらない）
        self.assertEqual(after - before, (2 - original.walks) + (1 - original.sacrifice_bunts))

    def test_it_is_a_fixed_number_of_queries(self):
        with CaptureQueriesContext(connection) as captured:
            self.query().for_year(YEAR)

        self.assertLessEqual(len(captured), 3)

    def test_the_value_is_the_domain_playing_time(self):
        result = self.query().for_year(YEAR)

        self.assertTrue(all(isinstance(value, PlayingTime) for value in result.values()))
