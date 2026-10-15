"""戦力分析をペナントの世界の中に置く（#130）。

戦力分析の組み立て口を世界の範囲にしただけで、世界の選手・試合だけを数え、実データの球団は
見つからず、リンクは世界の中に留まることを確かめる。
"""

import re
from datetime import date

from django.contrib.auth.models import User
from django.urls import reverse

from myapp.domain.value_objects import BattingLine
from myapp.infrastructure import orm_models
from myapp.presentation.views import build_pennant_world_service, build_team_analysis_service

from ..helpers import play_game, real_scope, world_scope
from .world_case import PENNANT_PLAYER_PREFIX, PENNANT_RIVAL, PENNANT_TEAM, YEAR, WorldCase

OLD_YEAR = 2019
HEADER_PAGES = ("/", "/players/", "/pennant/")


class WorldAnalysisCase(WorldCase):
    def url(self, team=None, **params) -> str:
        url = reverse("pennant_team_analysis", args=[self.world_id, (team or self.pennant_team).id])
        return f"{url}?" + "&".join(f"{key}={value}" for key, value in params.items()) if params else url


class WorldTeamAnalysisTest(WorldAnalysisCase):
    def test_the_page_opens_in_the_world_without_login(self):
        response = self.client.get(self.url())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, PENNANT_TEAM)
        self.assertEqual(response.context["world"].world_id, self.world_id)

    def test_only_the_worlds_players_are_counted(self):
        # 実データの球団にだけ、もう1人足す
        self.roster.register_player(self.team.id, "実の控え", 50, "投手")
        response = self.client.get(self.url())

        analysis = response.context["analysis"]
        self.assertEqual(analysis.pitchers.count + analysis.fielders.count, 10, "世界の球団は打者9人と投手1人")
        self.assertEqual(analysis.years, [YEAR])
        self.assertContains(response, PENNANT_PLAYER_PREFIX)
        self.assertNotContains(response, "実打者")
        self.assertNotContains(response, "実の控え")

    def test_the_team_switch_offers_only_the_worlds_teams(self):
        response = self.client.get(self.url())

        names = [option.name for option in response.context["analysis"].teams]
        self.assertCountEqual(names, [PENNANT_TEAM, PENNANT_RIVAL])
        self.assertContains(response, f'action="/pennant/{self.world_id}/analysis/"')

    def test_the_index_redirects_to_the_first_team_of_the_world(self):
        response = self.client.get(reverse("pennant_analysis_index", args=[self.world_id]), {"year": YEAR})

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(f"/pennant/{self.world_id}/team/"))
        self.assertIn("/analysis/?year=", response["Location"])

    def test_the_index_ignores_a_real_team_id(self):
        """切り替えフォームに実データの球団の id が来ても、世界の先頭の球団へ移る。"""
        response = self.client.get(reverse("pennant_analysis_index", args=[self.world_id]), {"team": self.team.id})

        self.assertEqual(response.status_code, 302)
        self.assertNotIn(f"/team/{self.team.id}/", response["Location"])
        self.assertEqual(self.client.get(response["Location"]).status_code, 200)

    def test_an_unknown_world_is_404(self):
        for name, args in (("pennant_analysis_index", []), ("pennant_team_analysis", [self.pennant_team.id])):
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(name, args=[self.world_id + 100, *args])).status_code, 404)


class WorldAnalysisIsolationTest(WorldAnalysisCase):
    def test_a_real_team_is_404_in_the_world(self):
        self.assertEqual(self.client.get(self.url(self.team)).status_code, 404)

    def test_a_pennant_team_is_404_in_the_real_data(self):
        self.assertEqual(self.client.get(reverse("team_analysis", args=[self.pennant_team.id])).status_code, 404)

    def test_another_worlds_team_is_404(self):
        other = build_pennant_world_service(real_scope()).create_world(
            name="別の世界", owner_id=None, source_league_ids=[self.league.id], start_year=YEAR + 1, seed=2
        )
        other_team = orm_models.Team.objects.filter(league__world_id=other.world.id).first()

        self.assertEqual(self.client.get(self.url(other_team)).status_code, 404)

    def test_the_real_page_does_not_show_the_world(self):
        response = self.client.get(reverse("team_analysis", args=[self.team.id]))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, PENNANT_PLAYER_PREFIX)
        self.assertNotContains(response, PENNANT_TEAM)
        self.assertNotContains(response, f"/pennant/{self.world_id}/")


class WorldServiceHistoryTest(WorldAnalysisCase):
    def test_records_from_year_is_per_world(self):
        """FA 取得の「試合の記録のある最初の年」は、世界の試合から取る（実データの古い年に引きずられない）。"""
        play_game(self.team, self.rival, year=OLD_YEAR, batting={self.real_batters[0]: BattingLine(at_bats=1)})

        real = self.client.get(reverse("team_analysis", args=[self.team.id]), {"tab": "fa"})
        world = self.client.get(self.url(tab="fa"))

        self.assertEqual(real.context["analysis"].records_from_year, OLD_YEAR)
        self.assertEqual(world.context["analysis"].records_from_year, YEAR)
        self.assertContains(world, f"試合の記録は{YEAR}年から")

    def test_the_default_year_is_the_worlds_today_when_there_are_no_games(self):
        """試合の無い世界の既定の年は、暦の今日ではなく、世界の今日（開幕年）。"""
        start_year = date.today().year + 5
        created = build_pennant_world_service(real_scope()).create_world(
            name="先の世界", owner_id=None, source_league_ids=[self.league.id], start_year=start_year, seed=3
        )
        team = orm_models.Team.objects.filter(league__world_id=created.world.id).first()

        analysis = build_team_analysis_service(world_scope(created.world.id)).get_analysis(team.id)

        self.assertEqual(analysis.year, start_year)
        self.assertEqual(analysis.years, [])

    def test_the_real_default_year_is_the_calendar_today(self):
        orm_models.Game.objects.filter(home_team__league__world_id__isnull=True).delete()

        response = self.client.get(reverse("team_analysis", args=[self.team.id]))

        self.assertEqual(response.context["analysis"].year, date.today().year)


class WorldStatsTabTest(WorldAnalysisCase):
    def test_the_stats_tab_shows_the_worlds_stats(self):
        response = self.client.get(self.url(tab="stats"))

        self.assertEqual(response.status_code, 200)
        names = [row.name for row in response.context["players"]]
        self.assertTrue(names)
        self.assertTrue(all(name.startswith(PENNANT_PLAYER_PREFIX) for name in names), names)
        self.assertContains(response, f"{YEAR}年の成績")
        self.assertContains(response, "OPS+")

    def test_pitcher_mode_and_sorting_work_in_the_world(self):
        response = self.client.get(self.url(tab="stats", pos="pitcher", sort="era", dir="asc"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["pos_mode"], "pitcher")
        self.assertTrue(all(row.name.startswith(PENNANT_PLAYER_PREFIX) for row in response.context["players"]))


class WorldAnalysisLinksTest(WorldAnalysisCase):
    def hrefs(self, url) -> list[str]:
        return re.findall(r'<a [^>]*href="([^"]*)"', self.client.get(url).content.decode())

    def test_every_link_stays_in_the_world_on_every_tab(self):
        prefix = f"/pennant/{self.world_id}/"
        for tab in ("depth", "usage", "fa", "stats"):
            for href in self.hrefs(self.url(tab=tab)):
                allowed = href.startswith((prefix, "?", "#", "/accounts/", "/admin/")) or href in HEADER_PAGES
                with self.subTest(tab=tab, href=href):
                    self.assertTrue(allowed)

    def test_the_header_and_the_team_page_link_to_the_worlds_analysis(self):
        links = self.hrefs(reverse("pennant_player_list", args=[self.world_id, self.pennant_team.id]))

        self.assertIn(f"/pennant/{self.world_id}/team/{self.pennant_team.id}/analysis/", links)
        self.assertIn(f"/pennant/{self.world_id}/analysis/", links)

    def test_the_real_pages_still_link_to_the_real_analysis(self):
        links = self.hrefs(reverse("player_list", args=[self.team.id]))

        self.assertIn(reverse("analysis_index"), links)
        self.assertIn(reverse("team_analysis", args=[self.team.id]), links)

    def test_the_page_has_no_write_links_even_for_the_owner(self):
        owner = User.objects.create_user("gm", password="x")
        orm_models.PennantWorld.objects.filter(id=self.world_id).update(
            owner_id=owner.id, managed_team_id=self.pennant_team.id
        )
        self.client.force_login(owner)

        content = self.client.get(self.url(tab="stats")).content.decode()

        for marker in ("/edit/", "/new/", "/advance/"):
            self.assertNotIn(marker, content)
