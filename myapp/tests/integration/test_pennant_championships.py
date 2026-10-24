"""ペナントの日本一の記録と、歴代の記録・球団史・世界の終了のポストシーズンの列（#39 段階 S5。#242）。

日本一（ポストシーズンの優勝球団）は締めるときに記録として保存する（表彰と同じ CLAUDE.md の例外）。締めた年は記録を、
終わったが締めていない年（いまの年度）だけを試合から導く。球団ごとの到達段は保存せず、その年の試合から導く。

世界は2リーグ×3球団（`PostseasonWorldCase`。NPB 方式と方式「なし」。同じシードなので公式戦の結果は同じ）。
"""

from __future__ import annotations

from unittest import mock

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from myapp.domain.exceptions import AlreadyClosed, InvalidChampionship, InvalidPostseason, InvalidWorld
from myapp.domain.pennant.championship import Championship
from myapp.domain.pennant.postseason import OutcomeKind, PostseasonFormat, PostseasonStatus, champion_title
from myapp.domain.pennant.schedule import AdvanceTarget
from myapp.domain.pennant.world import WorldScope
from myapp.infrastructure import orm_models
from myapp.infrastructure.queries import DjangoChampionshipQuery
from myapp.infrastructure.repositories import (
    DjangoChampionshipRepository,
    DjangoFixtureRepository,
    DjangoWorldRepository,
)
from myapp.presentation.views import (
    build_pennant_history_service,
    build_pennant_offseason_service,
    build_pennant_postseason_service,
    build_pennant_season_service,
    build_pennant_world_service,
    build_pennant_world_view,
)

from .world_case import YEAR, PostseasonWorldCase


class ChampionshipCase(PostseasonWorldCase):
    # --- 道具 ---

    @staticmethod
    def context(world_id):
        return build_pennant_world_view().get_context(world_id)

    def history(self, world_id):
        return build_pennant_history_service(world_id).get_world_history(self.context(world_id), league_id=None)

    def club(self, world_id, team_id):
        return build_pennant_history_service(world_id).get_club_history(self.context(world_id), team_id)

    @staticmethod
    def recorded(world_id):
        return list(
            orm_models.PennantChampionship.objects.filter(team__league__world_id=world_id).values_list(
                "year", "team_id"
            )
        )

    @staticmethod
    def teams(world_id):
        return list(orm_models.Team.objects.filter(league__world_id=world_id).values_list("id", flat=True))

    def finish_postseason(self, world_id):
        self.advance(world_id, AdvanceTarget.POSTSEASON_END)

    @staticmethod
    def close(world_id):
        build_pennant_offseason_service(world_id).close_season()

    def play_next_regular_season(self, world_id):
        """締めた後の翌年の公式戦を1週間だけ進め、残りの対戦を消して「公式戦を終えた」年にする（土台と同じ）。"""
        build_pennant_season_service(world_id).advance(AdvanceTarget.WEEK)
        fixtures = DjangoFixtureRepository(WorldScope.pennant(world_id))
        fixtures.remove(fixtures.find_all())


class RecordAtCloseTest(ChampionshipCase):
    """締める処理が、その年の優勝球団を記録として書く。"""

    def test_closing_records_the_champion_of_the_bracket(self):
        self.finish_postseason(self.npb_world)
        champion = self.bracket(self.npb_world).champion
        self.assertIsNotNone(champion)

        self.close(self.npb_world)

        self.assertEqual(self.recorded(self.npb_world), [(YEAR, champion)])

    def test_a_year_without_a_postseason_records_nothing(self):
        self.close(self.none_world)

        self.assertEqual(self.recorded(self.none_world), [])

    def test_a_second_close_adds_nothing(self):
        self.finish_postseason(self.npb_world)
        self.close(self.npb_world)

        with self.assertRaises(AlreadyClosed):
            build_pennant_offseason_service(self.npb_world).close_season(expected_year=YEAR)

        self.assertEqual(len(self.recorded(self.npb_world)), 1)

    def test_a_failure_while_writing_the_champion_leaves_nothing(self):
        """日本一は締める処理と同じトランザクションで書く。失敗したら、在籍も能力も日程も残らない。"""
        self.finish_postseason(self.npb_world)
        stints = orm_models.PlayerStint.objects.filter(team__league__world_id=self.npb_world).count()

        with (
            mock.patch.object(DjangoChampionshipRepository, "add", side_effect=InvalidChampionship("失敗")),
            self.assertRaises(InvalidChampionship),
        ):
            self.close(self.npb_world)

        self.assertEqual(orm_models.PlayerStint.objects.filter(team__league__world_id=self.npb_world).count(), stints)
        self.assertFalse(
            orm_models.PennantPlayerRatings.objects.filter(
                year=YEAR + 1, player__stints__team__league__world_id=self.npb_world
            ).exists()
        )
        self.assertEqual(self.recorded(self.npb_world), [])


class RepositoryTest(ChampionshipCase):
    def test_the_real_data_cannot_hold_a_champion(self):
        team = orm_models.Team.objects.filter(league__world__isnull=True).first()
        with self.assertRaises(InvalidWorld):
            DjangoChampionshipRepository(WorldScope.real()).add(Championship(year=YEAR, team_id=team.id))

    def test_a_team_outside_the_world_is_refused(self):
        repository = DjangoChampionshipRepository(WorldScope.pennant(self.npb_world))
        real = orm_models.Team.objects.filter(league__world__isnull=True).first().id
        for team_id in (self.teams(self.none_world)[0], real, 999999):
            with self.subTest(team_id=team_id), self.assertRaises(InvalidChampionship):
                repository.add(Championship(year=YEAR, team_id=team_id))
        self.assertEqual(self.recorded(self.npb_world), [])

    def test_a_year_is_recorded_once_per_world(self):
        """1世界1年に1つ（別の球団でも同じ年は書けない）。別の世界の同じ年は書ける。"""
        first, second = self.teams(self.npb_world)[:2]
        repository = DjangoChampionshipRepository(WorldScope.pennant(self.npb_world))
        repository.add(Championship(year=YEAR, team_id=first))

        with self.assertRaises(InvalidChampionship):
            repository.add(Championship(year=YEAR, team_id=second))

        DjangoChampionshipRepository(WorldScope.pennant(self.none_world)).add(
            Championship(year=YEAR, team_id=self.teams(self.none_world)[0])
        )
        self.assertEqual(self.recorded(self.npb_world), [(YEAR, first)])

    def test_another_world_and_the_real_data_do_not_read_them(self):
        self.finish_postseason(self.npb_world)
        self.close(self.npb_world)

        self.assertEqual(len(DjangoChampionshipQuery(WorldScope.pennant(self.npb_world)).list_championships()), 1)
        self.assertEqual(DjangoChampionshipQuery(WorldScope.pennant(self.none_world)).list_championships(), [])
        self.assertEqual(DjangoChampionshipQuery(WorldScope.real()).list_championships(), [])

    def test_deleting_the_world_deletes_its_champions(self):
        self.finish_postseason(self.npb_world)
        self.close(self.npb_world)

        build_pennant_world_service().delete_world(self.npb_world)

        self.assertFalse(orm_models.PennantChampionship.objects.exists())


class OpenYearTest(ChampionshipCase):
    """終わったが締めていない年（いまの年度）は、試合から導く。"""

    def test_before_the_postseason_the_year_is_in_progress(self):
        history = self.history(self.npb_world)

        (year,) = history.years
        self.assertEqual(history.postseason_title, "日本一")
        assert year.postseason is not None
        self.assertEqual((year.postseason.team_id, year.postseason.in_progress), (None, True))
        for team_id in self.teams(self.npb_world):
            with self.subTest(team_id=team_id):
                (season,) = self.club(self.npb_world, team_id).seasons
                self.assertEqual((season.postseason, season.won_postseason), ("進行中", False))

    def test_after_the_postseason_the_champion_and_the_stages_come_from_the_bracket(self):
        self.finish_postseason(self.npb_world)
        bracket = self.bracket(self.npb_world)

        (year,) = self.history(self.npb_world).years

        assert year.postseason is not None
        self.assertEqual((year.postseason.team_id, year.postseason.in_progress), (bracket.champion, False))
        for team_id in self.teams(self.npb_world):
            with self.subTest(team_id=team_id):
                club = self.club(self.npb_world, team_id)
                (season,) = club.seasons
                outcome = bracket.outcome_of(team_id)
                assert outcome is not None
                self.assertEqual(season.postseason, outcome.label)
                self.assertEqual(season.won_postseason, outcome.kind is OutcomeKind.CHAMPION)
                self.assertEqual(club.postseason_titles, int(season.won_postseason))
        self.assertEqual(self.recorded(self.npb_world), [], "締めていない年は保存しない")

    def test_a_world_without_a_postseason_shows_no_column(self):
        history = self.history(self.none_world)
        club = self.club(self.none_world, self.teams(self.none_world)[0])

        self.assertEqual(history.postseason_title, "")
        self.assertIsNone(history.years[0].postseason)
        self.assertEqual((club.postseason_title, club.postseason_titles, club.seasons[0].postseason), ("", 0, ""))
        content = self.client.get(reverse("pennant_history", args=[self.none_world])).content.decode()
        self.assertNotIn("日本一", content)

    def test_the_postseason_does_not_change_the_regular_part(self):
        """ポストシーズンを進めても締めても、公式戦の部分（順位・リーグ優勝・タイトル）は変わらない。"""

        def regular():
            team_id = self.teams(self.npb_world)[0]
            return (
                [
                    (year.year, year.champions, year.batting, year.pitching)
                    for year in self.history(self.npb_world).years
                ],
                [
                    (s.year, s.rank, s.wins, s.losses, s.ties, s.is_champion, s.titles)
                    for s in self.club(self.npb_world, team_id).seasons
                ],
            )

        before = regular()
        self.finish_postseason(self.npb_world)
        finished = regular()
        self.close(self.npb_world)

        self.assertEqual(finished, before)
        self.assertEqual(regular(), before)


class ClosedYearTest(ChampionshipCase):
    """締めた年は記録を読む（同じ年を記録と導出の両方から読まない）。"""

    def setUp(self):
        self.finish_postseason(self.npb_world)
        self.champion = self.bracket(self.npb_world).champion
        self.close(self.npb_world)

    def test_the_shown_champion_is_the_recorded_one(self):
        """出典が記録であることを、記録の球団を書き換えて確かめる（試合から導いた値なら変わらない）。"""
        other = next(team_id for team_id in self.teams(self.npb_world) if team_id != self.champion)
        orm_models.PennantChampionship.objects.filter(team__league__world_id=self.npb_world).update(team_id=other)

        (year,) = self.history(self.npb_world).years

        assert year.postseason is not None
        self.assertEqual((year.postseason.team_id, year.postseason.in_progress), (other, False))

    def test_the_club_history_refuses_a_record_that_disagrees_with_the_games(self):
        """記録と試合が食い違えば、どちらかに黙って寄せずに `InvalidPostseason`（握りつぶさない）。"""
        other = next(team_id for team_id in self.teams(self.npb_world) if team_id != self.champion)
        orm_models.PennantChampionship.objects.filter(team__league__world_id=self.npb_world).update(team_id=other)

        with self.assertRaises(InvalidPostseason):
            self.club(self.npb_world, self.champion)

    def test_the_closed_year_agrees_with_the_bracket(self):
        bracket = self.bracket(self.npb_world)
        for team_id in self.teams(self.npb_world):
            with self.subTest(team_id=team_id):
                (season,) = self.club(self.npb_world, team_id).seasons
                outcome = bracket.outcome_of(team_id)
                assert outcome is not None
                self.assertEqual((season.postseason, season.won_postseason), (outcome.label, team_id == self.champion))

    def test_a_closed_year_without_a_postseason_shows_a_dash(self):
        """方式「なし」で締めた年は、後から方式を NPB に変えても「無かった」（いまの方式で導き直さない）。"""
        self.close(self.none_world)
        orm_models.PennantWorld.objects.filter(id=self.none_world).update(postseason_format=PostseasonFormat.NPB.value)

        history = self.history(self.none_world)
        club = self.club(self.none_world, self.teams(self.none_world)[0])

        self.assertEqual(history.postseason_title, "日本一")
        (year,) = history.years
        assert year.postseason is not None
        self.assertEqual((year.postseason.team_id, year.postseason.in_progress), (None, False))
        self.assertEqual(club.seasons[0].postseason, "")

    def test_the_screens_show_the_champion(self):
        name = orm_models.Team.objects.get(id=self.champion).name

        history = self.client.get(reverse("pennant_history", args=[self.npb_world])).content.decode()
        club = self.client.get(reverse("pennant_team_history", args=[self.npb_world, self.champion])).content.decode()

        self.assertIn("リーグ優勝と日本一", history)
        self.assertIn(name, history)
        self.assertIn('badge bg-primary">日本一</span>', club)
        self.assertIn(reverse("pennant_postseason_by_year", args=[self.npb_world, YEAR]), club)


class FinaleTest(ChampionshipCase):
    """世界の終了: 締めた年は記録、最終シーズン（締めない年）は導いた日本一。"""

    def test_the_finale_marks_the_champion_years(self):
        self.finish_postseason(self.npb_world)
        champion = self.bracket(self.npb_world).champion
        self.close(self.npb_world)
        self.play_next_regular_season(self.npb_world)
        self.finish_postseason(self.npb_world)
        world = DjangoWorldRepository().find_by_id(self.npb_world)
        last = build_pennant_postseason_service(self.npb_world)._brackets.bracket(world, YEAR + 1)
        self.assertIs(last.status, PostseasonStatus.FINISHED)
        orm_models.PennantWorld.objects.filter(id=self.npb_world).update(managed_team_id=champion)

        with mock.patch("myapp.application.pennant_view.is_final_season", return_value=True):
            response = self.client.get(reverse("pennant_world", args=[self.npb_world]))

        finale = response.context["home"].finale
        assert finale is not None
        self.assertEqual(finale.postseason_title, "日本一")
        self.assertEqual([s.year for s in finale.seasons], [YEAR, YEAR + 1])
        self.assertEqual([s.won_postseason for s in finale.seasons], [True, last.champion == champion])
        self.assertEqual(finale.postseason_titles, sum(s.won_postseason for s in finale.seasons))
        self.assertIn(f'日本一</span> <span class="stat-key">{finale.postseason_titles}回', response.content.decode())


class QueryCountTest(ChampionshipCase):
    """日本一と到達段の分のクエリ数は、年の数に比例しない（ポストシーズンの試合は全部の年を1本で読む）。

    方式「なし」の世界との差を、1年と2年で比べる（年ごとに増えるのは順位とタイトルの読み込みで、両方の世界に同じだけ）。
    """

    def count(self, url) -> int:
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self.client.get(url).status_code, 200)
        return len(queries)

    def extra(self) -> list[int]:
        npb_team, none_team = self.teams(self.npb_world)[0], self.teams(self.none_world)[0]
        pairs = (
            (reverse("pennant_history", args=[self.npb_world]), reverse("pennant_history", args=[self.none_world])),
            (
                reverse("pennant_team_history", args=[self.npb_world, npb_team]),
                reverse("pennant_team_history", args=[self.none_world, none_team]),
            ),
        )
        return [self.count(npb) - self.count(none) for npb, none in pairs]

    def test_the_postseason_part_does_not_grow_with_the_years(self):
        self.finish_postseason(self.npb_world)
        one_year = self.extra()
        for world_id in (self.npb_world, self.none_world):
            self.close(world_id)
            self.play_next_regular_season(world_id)
        self.finish_postseason(self.npb_world)

        two_years = self.extra()

        self.assertEqual([len(y.years) for y in (self.history(self.npb_world),)], [2])
        self.assertEqual(two_years, one_year)
        self.assertTrue(all(0 < extra <= 4 for extra in one_year), one_year)


class ChampionTitleTest(ChampionshipCase):
    def test_the_label_of_the_bracket_is_the_title_of_the_world(self):
        self.finish_postseason(self.npb_world)

        self.assertEqual(self.bracket(self.npb_world).champion_label, champion_title(2))
