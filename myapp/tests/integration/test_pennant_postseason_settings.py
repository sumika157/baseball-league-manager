"""ポストシーズンの方式の設定と、ポストシーズンの個人成績の表（#39 段階 S4b。#280）。

土台の世界（2リーグ×3球団・公式戦を終えた NPB 方式と方式「なし」）は `world_case.PostseasonWorldCase`。
決定（#280）: 過去の年の勝ち上がりは、その年の試合から方式を導く・終了した世界の方式は変えられない。
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.db.models import F, Sum
from django.urls import reverse

from myapp.domain.exceptions import AlreadyAdvanced, PostseasonFormatLocked, PostseasonNotFinished
from myapp.domain.pennant.postseason import PostseasonFormat
from myapp.domain.pennant.schedule import AdvanceTarget
from myapp.domain.pennant.world import MAX_SEASONS_PER_WORLD, WorldScope
from myapp.domain.value_objects import GameStage, InningsPitched
from myapp.infrastructure import orm_models
from myapp.infrastructure.repositories import DjangoFixtureRepository, DjangoWorldRepository
from myapp.presentation.views import (
    build_pennant_offseason_service,
    build_pennant_postseason_service,
    build_pennant_season_service,
    build_pennant_settings_service,
)

from .world_case import YEAR, PostseasonWorldCase

DECIDE_RETIREMENTS = "myapp.domain.pennant.offseason.decide_retirements"
NPB_LABEL = PostseasonFormat.NPB.label
# 安打（`BattingLine.hits` は単打・二塁打・三塁打・本塁打の和。列には持たない）
HITS = Sum(F("singles") + F("doubles") + F("triples") + F("home_runs"))


class SettingsCase(PostseasonWorldCase):
    """2つの世界にオーナーを付ける。"""

    owner: User

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.owner = User.objects.create_user("gm", password="x")
        orm_models.PennantWorld.objects.filter(id__in=[cls.npb_world, cls.none_world]).update(owner_id=cls.owner.id)

    def settings_page(self, world_id=None):
        return self.client.get(reverse("pennant_settings", args=[world_id or self.npb_world]))

    def post_format(self, postseason_format: str, world_id=None):
        return self.client.post(
            reverse("pennant_settings", args=[world_id or self.npb_world]), {"postseason_format": postseason_format}
        )

    @staticmethod
    def format_of(world_id) -> PostseasonFormat:
        return DjangoWorldRepository().find_by_id(world_id).postseason_format

    def close(self, world_id, year):
        with mock.patch(DECIDE_RETIREMENTS, return_value=()):
            build_pennant_offseason_service(world_id).close_season(expected_year=year)

    def finish_regular_season(self, world_id):
        """締めた後の翌年を1週間だけ進め、残りの対戦を消して公式戦を終えた状態にする（土台と同じやり方）。"""
        build_pennant_season_service(world_id).advance(AdvanceTarget.WEEK)
        fixtures = DjangoFixtureRepository(WorldScope.pennant(world_id))
        fixtures.remove(fixtures.find_all())

    def bracket_page(self, world_id, year=None):
        if year is None:
            return self.client.get(reverse("pennant_postseason", args=[world_id]))
        return self.client.get(reverse("pennant_postseason_by_year", args=[world_id, year]))


class SettingsPageTest(SettingsCase):
    def test_anyone_can_read_the_format_but_only_the_owner_gets_the_form(self):
        response = self.settings_page()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'data-testid="postseason-format">{NPB_LABEL}<')
        self.assertNotContains(response, 'name="postseason_format"')

        self.client.force_login(User.objects.create_user("other", password="x"))
        self.assertNotContains(self.settings_page(), 'name="postseason_format"')

        self.client.force_login(self.owner)
        content = self.settings_page().content.decode()
        self.assertIn('name="postseason_format"', content)
        # いまの方式が選ばれている。選択肢は domain の `PostseasonFormat` の全部
        self.assertRegex(content, r'value="NPB"[^>]*checked')
        for option in PostseasonFormat:
            self.assertIn(f'value="{option.name}"', content)
            self.assertIn(option.description, content)

    def test_guests_are_sent_to_log_in_and_others_are_refused(self):
        response = self.post_format("NONE")
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

        self.client.force_login(User.objects.create_user("other", password="x"))
        self.assertEqual(self.post_format("NONE").status_code, 403)

        self.assertIs(self.format_of(self.npb_world), PostseasonFormat.NPB)

    def test_the_owner_changes_the_format_before_the_postseason_starts(self):
        self.client.force_login(self.owner)
        home = self.client.get(reverse("pennant_world", args=[self.npb_world])).content.decode()
        self.assertIn(reverse("pennant_settings", args=[self.npb_world]), home)
        self.assertIn('id="postseason"', home)

        response = self.post_format("NONE")

        self.assertRedirects(
            response, reverse("pennant_settings", args=[self.npb_world]), fetch_redirect_response=False
        )
        self.assertIs(self.format_of(self.npb_world), PostseasonFormat.NONE)
        self.assertContains(self.client.get(response["Location"]), "ポストシーズンの方式を「なし」にしました。")
        # ポストシーズンの試合が無い世界は、カードも世界バーのリンクも消え、そのまま締められる
        home = self.client.get(reverse("pennant_world", args=[self.npb_world])).content.decode()
        self.assertNotIn('id="postseason"', home)
        self.assertNotIn(reverse("pennant_postseason", args=[self.npb_world]), home)
        self.assertIn('id="season-close"', home)

    def test_the_settings_link_is_only_for_the_owner(self):
        url = reverse("pennant_settings", args=[self.npb_world])
        self.assertNotIn(url, self.client.get(reverse("pennant_world", args=[self.npb_world])).content.decode())

    def test_the_same_format_changes_nothing_and_a_bad_value_is_refused(self):
        self.client.force_login(self.owner)

        response = self.post_format("NPB")
        self.assertContains(self.client.get(response["Location"]), "ポストシーズンの方式は変わっていません。")

        response = self.post_format("BAD")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)
        self.assertIs(self.format_of(self.npb_world), PostseasonFormat.NPB)

    def test_the_format_cannot_change_once_the_postseason_has_started(self):
        self.advance(self.npb_world)
        self.client.force_login(self.owner)

        response = self.settings_page()
        self.assertContains(response, 'data-testid="settings-blocker"')
        self.assertContains(response, "今年のポストシーズンが始まっているので")
        self.assertNotContains(response, 'name="postseason_format"')

        response = self.post_format("NONE", world_id=self.npb_world)
        self.assertContains(self.client.get(response["Location"]), "今年のポストシーズンが始まっているので")
        self.assertIs(self.format_of(self.npb_world), PostseasonFormat.NPB)
        with self.assertRaises(PostseasonFormatLocked):
            build_pennant_settings_service(self.npb_world).change_postseason_format(PostseasonFormat.CHAMPIONS)

    def test_after_closing_the_season_the_format_can_change(self):
        self.advance(self.npb_world, AdvanceTarget.POSTSEASON_END)
        self.close(self.npb_world, YEAR)
        self.client.force_login(self.owner)

        self.assertContains(self.settings_page(), 'name="postseason_format"')
        self.post_format("CHAMPIONS")

        self.assertIs(self.format_of(self.npb_world), PostseasonFormat.CHAMPIONS)

    def test_a_world_that_is_over_cannot_change(self):
        """最終シーズンを終えた世界（方式「なし」で公式戦を終えた・NPB 方式でポストシーズンまで終えた）。"""
        orm_models.PennantWorld.objects.filter(id__in=[self.npb_world, self.none_world]).update(
            start_year=YEAR - MAX_SEASONS_PER_WORLD + 1
        )
        self.client.force_login(self.owner)

        # NPB 方式で、ポストシーズンがまだ始まっていない最終シーズンは変えられる（「なし」にすれば終了する）
        self.assertContains(self.settings_page(), 'name="postseason_format"')
        self.advance(self.npb_world, AdvanceTarget.POSTSEASON_END)
        for world_id in (self.npb_world, self.none_world):
            with self.subTest(world_id=world_id):
                response = self.settings_page(world_id)
                self.assertContains(response, "最終シーズンを終えている")
                self.assertNotContains(response, 'name="postseason_format"')
                before = self.format_of(world_id)
                self.post_format("CHAMPIONS", world_id=world_id)
                self.assertIs(self.format_of(world_id), before)


class FormatChangeWhileAdvancingTest(SettingsCase):
    def test_a_format_changed_while_computing_writes_no_games(self):
        """進める計算の間に方式が「なし」に変わったら、書かずに AlreadyAdvanced（古い方式の試合を書かない）。"""
        service = build_pennant_postseason_service(self.npb_world)
        atomic = service._atomic

        def racing():
            build_pennant_settings_service(self.npb_world).change_postseason_format(PostseasonFormat.NONE)
            return atomic()

        service._atomic = racing
        with self.assertRaises(AlreadyAdvanced):
            service.advance(AdvanceTarget.DAY)

        self.assertFalse(self.postseason_games(self.npb_world).exists())
        self.assertIs(self.format_of(self.npb_world), PostseasonFormat.NONE)

    def test_a_format_changed_while_closing_closes_nothing(self):
        """「なし」の世界を締める計算の間に方式を NPB に変えたら、締めずに PostseasonNotFinished。"""
        service = build_pennant_offseason_service(self.none_world)
        atomic = service._atomic

        def racing():
            build_pennant_settings_service(self.none_world).change_postseason_format(PostseasonFormat.NPB)
            return atomic()

        service._atomic = racing
        with mock.patch(DECIDE_RETIREMENTS, return_value=()), self.assertRaises(PostseasonNotFinished):
            service.close_season(expected_year=YEAR)

        self.assertFalse(orm_models.PennantPlayerRatings.objects.filter(year=YEAR + 1).exists())
        self.assertFalse(build_pennant_offseason_service(self.none_world).close_option().can_close)


class PastYearsKeepTheirFormatTest(SettingsCase):
    def test_npb_then_champions_then_none_keeps_each_years_bracket(self):
        """NPB → 優勝球団 → なし と年ごとに変えても、過去の年の表はその年の試合から導いた方式のまま開ける。"""
        world = self.npb_world
        self.advance(world, AdvanceTarget.POSTSEASON_END)
        first = self.bracket_page(world, YEAR).context["postseason"]
        self.close(world, YEAR)

        build_pennant_settings_service(world).change_postseason_format(PostseasonFormat.CHAMPIONS)
        self.finish_regular_season(world)
        self.advance(world, AdvanceTarget.POSTSEASON_END)
        self.close(world, YEAR + 1)
        build_pennant_settings_service(world).change_postseason_format(PostseasonFormat.NONE)

        again = self.bracket_page(world, YEAR).context["postseason"]
        self.assertEqual(again.format_label, NPB_LABEL)
        self.assertEqual(again.rounds, first.rounds)
        self.assertEqual(again.champion_team_id, first.champion_team_id)

        second = self.bracket_page(world, YEAR + 1).context["postseason"]
        self.assertEqual(second.format_label, PostseasonFormat.CHAMPIONS.label)
        self.assertEqual([r.name for r in second.rounds], ["日本シリーズ"])
        self.assertIsNotNone(second.champion_team_id)
        self.assertEqual(second.years, (YEAR + 1, YEAR))

        # いまの方式が「なし」でも、過去の年の表と個人成績・世界バーのリンク・試合詳細のリンクは出る
        self.assertEqual(self.bracket_page(world).context["postseason"].year, YEAR + 1)
        standings = self.client.get(reverse("pennant_standings", args=[world])).content.decode()
        self.assertIn(reverse("pennant_postseason", args=[world]), standings)
        self.assertEqual(self.client.get(reverse("pennant_postseason_stats", args=[world, YEAR])).status_code, 200)
        game = self.postseason_games(world).first()
        detail = self.client.get(reverse("pennant_game_detail", args=[world, game.id])).content.decode()
        self.assertIn(reverse("pennant_postseason_by_year", args=[world, YEAR]), detail)


class PostseasonStatsTest(SettingsCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        build_pennant_postseason_service(cls.npb_world).advance(AdvanceTarget.POSTSEASON_END)

    def stats(self, query="", year=YEAR, world_id=None):
        url = reverse("pennant_postseason_stats", args=[world_id or self.npb_world, year])
        return self.client.get(url + query)

    def postseason_lines(self, model):
        return model.objects.filter(
            game__home_team__league__world_id=self.npb_world, game__stage=GameStage.POSTSEASON.value, game__year=YEAR
        )

    def test_batters_are_the_players_who_played_in_the_postseason_by_hits(self):
        response = self.stats()

        self.assertEqual(response.status_code, 200)
        stats = response.context["stats"]
        self.assertEqual((stats.listing.sort, stats.listing.descending), ("hits", True))
        rows = stats.listing.rows
        lines = self.postseason_lines(orm_models.GameBattingLine)
        batters = set(lines.exclude(player__position="投手").values_list("player_id", flat=True))
        self.assertEqual({row.player.id for row in rows}, batters)
        self.assertEqual([row.player.hits for row in rows], sorted((row.player.hits for row in rows), reverse=True))
        # 成績はポストシーズンの試合だけ。率は合算した実数から（試合ごとの率の平均ではない）
        for row in rows[:5]:
            totals = lines.filter(player_id=row.player.id).aggregate(hits=HITS, at_bats=Sum("at_bats"))
            self.assertEqual(row.player.hits, totals["hits"])
            self.assertAlmostEqual(row.player.batting_average, totals["hits"] / totals["at_bats"])
            games = lines.filter(player_id=row.player.id).values("game_id").distinct().count()
            self.assertEqual(row.player.games, games)

    def test_pitchers_are_sorted_by_innings_and_use_innings_pitched(self):
        stats = self.stats("?pos=pitcher").context["stats"]

        self.assertEqual((stats.listing.sort, stats.listing.descending), ("innings", True))
        lines = self.postseason_lines(orm_models.GamePitchingLine)
        self.assertEqual({row.player.id for row in stats.listing.rows}, set(lines.values_list("player_id", flat=True)))
        top = stats.listing.rows[0]
        own = lines.filter(player_id=top.player.id)
        innings = sum(
            (InningsPitched.from_notation(v) for v in own.values_list("innings_pitched", flat=True)),
            InningsPitched.zero(),
        )
        self.assertEqual(top.player.innings_pitched, str(innings))
        earned = own.aggregate(earned=Sum("earned_runs"))["earned"]
        self.assertAlmostEqual(top.player.earned_run_average, earned * 27 / innings.outs)
        self.assertEqual(top.player.appearances, own.count())

    def test_players_who_did_not_play_are_not_listed(self):
        played = set(self.postseason_lines(orm_models.GameBattingLine).values_list("player_id", flat=True)) | set(
            self.postseason_lines(orm_models.GamePitchingLine).values_list("player_id", flat=True)
        )
        listed = {row.player.id for row in self.stats().context["players"]} | {
            row.player.id for row in self.stats("?pos=pitcher").context["players"]
        }
        self.assertEqual(listed, played)
        on_roster = orm_models.PlayerStint.objects.filter(team__league__world_id=self.npb_world).count()
        self.assertLess(len(listed), on_roster)

    def test_a_bad_sort_key_falls_back_to_the_default(self):
        for query, expected in (
            ("?sort=bogus", ("hits", True)),
            ("?pos=pitcher&sort=bogus&dir=sideways", ("innings", True)),
            ("?sort=home_runs&dir=asc", ("home_runs", False)),
            ("?pos=pitcher&sort=era", ("era", False)),
        ):
            with self.subTest(query=query):
                listing = self.stats(query).context["stats"].listing
                self.assertEqual((listing.sort, listing.descending), expected)

    def test_the_regular_season_numbers_are_not_in_the_table(self):
        """公式戦の成績一覧の安打とは別の値（ポストシーズンの試合だけを数える）。"""
        rows = self.stats().context["players"]
        regular = dict(
            orm_models.GameBattingLine.objects.filter(
                player_id__in=[row.player.id for row in rows], game__stage=GameStage.REGULAR.value
            )
            .values("player_id")
            .annotate(hits=HITS)
            .values_list("player_id", "hits")
        )
        self.assertTrue(any(hits for hits in regular.values()), "前提: 公式戦にも安打がある")
        listed = sum(row.player.hits for row in rows)
        lines = self.postseason_lines(orm_models.GameBattingLine).exclude(player__position="投手")
        postseason = lines.aggregate(hits=HITS)["hits"]
        self.assertEqual(listed, postseason)

    def test_years_without_a_postseason_are_not_found(self):
        self.assertEqual(self.stats(year=YEAR - 1).status_code, 404)
        self.assertEqual(self.stats(world_id=self.none_world).status_code, 404)

    def test_the_tabs_link_the_bracket_and_the_stats(self):
        bracket = self.bracket_page(self.npb_world, YEAR).content.decode()
        self.assertIn(reverse("pennant_postseason_stats", args=[self.npb_world, YEAR]), bracket)
        stats = self.stats().content.decode()
        self.assertIn(reverse("pennant_postseason_by_year", args=[self.npb_world, YEAR]), stats)
        self.assertIn('data-testid="postseason-stats"', stats)


class PostseasonStatsBeforeStartTest(SettingsCase):
    def test_before_the_postseason_starts_the_table_is_empty(self):
        response = self.client.get(reverse("pennant_postseason_stats", args=[self.npb_world, YEAR]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["players"], [])
        self.assertContains(response, f"{YEAR}年のポストシーズンの試合は、まだありません。")
