"""ポストシーズンの画面（#39 段階 S4a。#241）。

勝ち上がりの表・GM ホームの「ポストシーズン」のカード・画面から進める・作成フォームの方式・試合詳細の印・
最終シーズンの世界の終了。土台の世界（2リーグ×3球団・公式戦を終えた NPB 方式と方式「なし」）は
`world_case.PostseasonWorldCase`。
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from myapp.application.pennant_home import postseason_advance_options
from myapp.domain import services as domain_services
from myapp.domain.exceptions import InvalidPostseason
from myapp.domain.pennant.postseason import PostseasonBracket, PostseasonFormat, PostseasonStatus
from myapp.domain.pennant.schedule import AdvanceTarget, Fixture
from myapp.domain.pennant.world import MAX_SEASONS_PER_WORLD, WorldScope
from myapp.domain.value_objects import FieldingPosition, GameHeader, GameStage, LineupEntry, Season
from myapp.infrastructure import orm_models
from myapp.infrastructure.repositories import DjangoFixtureRepository, DjangoGameRepository
from myapp.presentation.views import build_pennant_postseason_view_service, build_pennant_world_view

from ..helpers import build_scorebook, to_plate_appearances
from .world_case import YEAR, PostseasonWorldCase, WorldCase

DECIDE_RETIREMENTS = "myapp.domain.pennant.offseason.decide_retirements"


class ScreenCase(PostseasonWorldCase):
    """NPB の世界にオーナーを付ける（「なし」の世界も同じオーナー）。"""

    owner: User

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.owner = User.objects.create_user("gm", password="x")
        orm_models.PennantWorld.objects.filter(id__in=[cls.npb_world, cls.none_world]).update(owner_id=cls.owner.id)

    def home(self, world_id=None):
        return self.client.get(reverse("pennant_world", args=[world_id or self.npb_world]))

    def page(self, world_id=None, year=None):
        world_id = world_id or self.npb_world
        if year is None:
            return self.client.get(reverse("pennant_postseason", args=[world_id]))
        return self.client.get(reverse("pennant_postseason_by_year", args=[world_id, year]))

    def post_advance(self, target, *, expected_today="keep", world_id=None):
        world_id = world_id or self.npb_world
        data = {"target": target}
        if expected_today == "keep":
            today = build_pennant_world_view().get_context(world_id).today
            data["expected_today"] = today.isoformat() if today else ""
        elif expected_today is not None:
            data["expected_today"] = expected_today
        return self.client.post(reverse("pennant_advance", args=[world_id]), data)

    def postseason_count(self, world_id=None):
        return self.postseason_games(world_id or self.npb_world).count()


class PostseasonPageTest(ScreenCase):
    def test_anyone_can_read_the_bracket_before_it_starts(self):
        response = self.page(year=YEAR)

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn(f"{YEAR}年 ポストシーズン", content)
        self.assertIn("開始前", content)
        self.assertIn("CS ファーストステージ", content)
        # まだ組めていない先の回は名前だけ
        self.assertIn("CS ファイナルステージ → 日本シリーズ", content)
        self.assertIn("次の試合", content)
        self.assertNotIn('name="target"', content, "進めるボタンは表のページに置かない")

    def test_the_default_year_is_the_latest(self):
        response = self.page()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["postseason"].year, YEAR)

    def test_in_progress_shows_the_games_with_links(self):
        self.advance(self.npb_world)
        response = self.page(year=YEAR)

        self.assertContains(response, "進行中")
        game = self.postseason_games(self.npb_world).first()
        self.assertContains(response, reverse("pennant_game_detail", args=[self.npb_world, game.id]))

    def test_finished_shows_the_champion(self):
        report = self.advance(self.npb_world, AdvanceTarget.POSTSEASON_END)
        response = self.page(year=YEAR)

        self.assertContains(response, "終了")
        self.assertContains(response, f'{YEAR}年 日本一: <span class="fw-semibold">{report.champion_name}</span>')
        self.assertEqual(response.context["postseason"].pending_stage_names, ())
        self.assertEqual([r.name for r in response.context["postseason"].rounds][-1], "日本シリーズ")

    def test_years_without_a_postseason_are_not_found(self):
        self.assertEqual(self.page(year=YEAR - 1).status_code, 404)
        self.assertEqual(self.page(world_id=self.none_world, year=YEAR).status_code, 404)

    def test_a_world_without_a_postseason_shows_an_empty_page_and_no_link(self):
        response = self.page(world_id=self.none_world)

        self.assertContains(response, "この世界はポストシーズンを行いません。")
        home = self.home(self.none_world).content.decode()
        self.assertNotIn(reverse("pennant_postseason", args=[self.none_world]), home)
        self.assertIn(reverse("pennant_postseason", args=[self.npb_world]), self.home().content.decode())

    def test_during_the_regular_season_there_is_nothing_to_show_yet(self):
        self._back_to_the_regular_season()
        response = self.page()

        self.assertContains(response, "まだポストシーズンはありません。")
        self.assertEqual(self.page(year=YEAR).status_code, 404)

    def test_the_page_reads_a_fixed_number_of_queries(self):
        """シリーズや試合の数でクエリが増えない（N+1 でない）。開始前と終了後で同じ数。"""
        before = self._queries()
        self.advance(self.npb_world, AdvanceTarget.POSTSEASON_END)
        self.assertEqual(self._queries(), before)

    def _queries(self) -> int:
        with CaptureQueriesContext(connection) as captured:
            self.assertEqual(self.page(year=YEAR).status_code, 200)
        return len(captured)

    def _back_to_the_regular_season(self):
        teams = list(orm_models.Team.objects.filter(league__world_id=self.npb_world).values_list("id", flat=True))
        DjangoFixtureRepository(WorldScope.pennant(self.npb_world)).add_all(
            [Fixture(date(YEAR, 11, 30), teams[0], teams[1])]
        )


class HomeCardTest(ScreenCase):
    def test_during_the_postseason_the_owner_gets_the_buttons_and_no_close_card(self):
        self.client.force_login(self.owner)
        content = self.home().content.decode()

        self.assertIn('id="postseason"', content)
        for target in ("day", "postseason_end"):
            self.assertIn(f'name="target" value="{target}"', content)
        self.assertIn("ポストシーズンの終わりまで進める", content)
        self.assertIn("戦力外はポストシーズン中も編成の「戦力外」で選べます", content)
        self.assertNotIn('id="season-close"', content, "ポストシーズン中は締めるカードを出さない")
        self.assertNotIn("のシーズンを締める", content)

    def test_the_next_own_game_button_is_only_for_a_team_still_alive(self):
        self.assertEqual(
            [o.target for o in postseason_advance_options(own_alive=True)],
            ["day", "next_managed_game", "postseason_end"],
        )
        self.assertEqual([o.target for o in postseason_advance_options(own_alive=False)], ["day", "postseason_end"])
        options = postseason_advance_options(own_alive=True)
        self.assertTrue(all(o.end_date is None and o.games is None for o in options))

    def test_others_and_guests_see_the_card_without_buttons(self):
        stranger = User.objects.create_user("other", password="x")
        for user in (None, stranger):
            with self.subTest(user=user):
                self.client.logout()
                if user is not None:
                    self.client.force_login(user)
                content = self.home().content.decode()
                self.assertIn('id="postseason"', content)
                self.assertNotIn('name="target"', content)
                self.assertNotIn('id="season-close"', content)
                self.assertNotIn("戦力外はポストシーズン中も", content)

    def test_after_the_postseason_the_champion_line_and_the_close_card(self):
        report = self.advance(self.npb_world, AdvanceTarget.POSTSEASON_END)
        self.client.force_login(self.owner)
        response = self.home()
        content = response.content.decode()

        self.assertIn(f"{YEAR}年 日本一", content)
        self.assertIn(report.champion_name, content)
        self.assertNotIn('name="target"', content)
        self.assertIn('id="season-close"', content)
        self.assertIn(f"{YEAR}年のシーズンを締める", content)

    def test_the_home_derives_the_bracket_only_once(self):
        """カードで導いた状況を締めるカードに渡し、締める処理の側で導き直さない。"""
        self.advance(self.npb_world, AdvanceTarget.POSTSEASON_END)
        self.client.force_login(self.owner)
        with mock.patch.object(PostseasonBracket, "derive", wraps=PostseasonBracket.derive) as derive:
            self.home()
        self.assertEqual(derive.call_count, 1)

    def test_a_world_without_a_postseason_has_no_card(self):
        self.client.force_login(self.owner)
        content = self.home(self.none_world).content.decode()

        self.assertNotIn('id="postseason"', content)
        self.assertNotIn("ポストシーズン", content)
        self.assertIn('id="season-close"', content)

    def test_a_postseason_since_shows_no_summary(self):
        """ポストシーズンの期間を指す `?since=` では「進めた結果」を出さない（まとめは公式戦だけを読む）。"""
        self.advance(self.npb_world)
        today = build_pennant_world_view().get_context(self.npb_world).today
        self.client.force_login(self.owner)
        response = self.client.get(
            reverse("pennant_world", args=[self.npb_world]) + f"?since={today - timedelta(days=1)}"
        )
        self.assertIsNone(response.context["home"].summary)


class AdvanceButtonTest(ScreenCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)

    def test_each_target_advances_the_postseason(self):
        for target in ("day", "next_managed_game", "postseason_end"):
            with self.subTest(target=target):
                before = self.postseason_count()
                response = self.post_advance(target)
                self.assertRedirects(
                    response,
                    reverse("pennant_world", args=[self.npb_world]) + "#postseason",
                    fetch_redirect_response=False,
                )
                self.assertGreater(self.postseason_count(), before)
        self.assertIs(build_pennant_postseason_view_service(self.npb_world).status(YEAR), PostseasonStatus.FINISHED)

    def test_the_end_reports_the_champion(self):
        response = self.post_advance("postseason_end")
        messages = [str(m) for m in response.wsgi_request._messages]

        self.assertTrue(any(m.endswith("試合を進めました。") for m in messages), messages)
        self.assertTrue(any("の優勝です。" in m for m in messages), messages)
        # 終わった後は進める試合が無い
        response = self.post_advance("day")
        self.assertIn("進める試合がありません。", [str(m) for m in response.wsgi_request._messages])

    def test_guests_are_sent_to_log_in_and_others_are_refused(self):
        self.client.logout()
        response = self.post_advance("day", expected_today=None)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])
        self.client.force_login(User.objects.create_user("other", password="x"))
        self.assertEqual(self.post_advance("day").status_code, 403)
        self.assertEqual(self.postseason_count(), 0)

    def test_a_missing_bad_or_stale_today_advances_nothing(self):
        for expected_today in (None, "", "あした", "2026-01-01"):
            with self.subTest(expected_today=expected_today):
                self.post_advance("day", expected_today=expected_today)
                self.assertEqual(self.postseason_count(), 0)

    def test_regular_season_targets_are_refused_in_the_postseason(self):
        for target in ("week", "month_end", "limit", "season_end", "nonsense"):
            with self.subTest(target=target):
                response = self.post_advance(target)
                self.assertIn("その進め方はできません。", [str(m) for m in response.wsgi_request._messages])
        self.assertEqual(self.postseason_count(), 0)

    def test_the_postseason_end_is_refused_during_the_regular_season(self):
        teams = list(orm_models.Team.objects.filter(league__world_id=self.npb_world).values_list("id", flat=True))
        DjangoFixtureRepository(WorldScope.pennant(self.npb_world)).add_all(
            [Fixture(date(YEAR, 11, 30), teams[0], teams[1])]
        )
        response = self.post_advance("postseason_end")

        self.assertIn("その進め方はできません。", [str(m) for m in response.wsgi_request._messages])
        self.assertEqual(self.postseason_count(), 0)


class WorldOverTest(ScreenCase):
    """最終シーズンのポストシーズンが終わるまで、世界は終了にならない。"""

    def setUp(self):
        super().setUp()
        orm_models.PennantWorld.objects.filter(id=self.npb_world).update(start_year=YEAR - MAX_SEASONS_PER_WORLD + 1)

    def test_the_world_is_over_only_after_the_final_postseason(self):
        views = build_pennant_world_view()
        self.assertTrue(views.get_context(self.npb_world).is_final_season)
        self.assertFalse(views.settle(views.get_context(self.npb_world)).is_over)
        self.client.force_login(self.owner)
        content = self.home().content.decode()
        self.assertIn('name="target"', content)
        self.assertIn("ポストシーズンが終わると、この世界は終了します。", content)
        self.assertNotIn('id="world-finale"', content)

        self.advance(self.npb_world, AdvanceTarget.POSTSEASON_END)

        self.assertTrue(views.settle(views.get_context(self.npb_world)).is_over)
        content = self.home().content.decode()
        self.assertIn('id="world-finale"', content)
        self.assertNotIn('id="season-close"', content)
        self.assertNotIn('name="target"', content)
        listing = self.client.get(reverse("pennant_index")).content.decode()
        self.assertIn(f"{YEAR}年 · 終了", listing)

    def test_only_the_pages_that_show_the_end_derive_the_bracket(self):
        """世界の終了を出さない画面は導かず、GM ホームはカードの1回だけ導く（終了した世界のどの画面でも導かない）。"""
        self.advance(self.npb_world, AdvanceTarget.POSTSEASON_END)
        self.client.force_login(self.owner)
        for url, expected in (
            (reverse("pennant_standings", args=[self.npb_world]), 0),
            (reverse("pennant_world", args=[self.npb_world]), 1),
            (reverse("pennant_index"), 1),
        ):
            with self.subTest(url=url):
                with mock.patch.object(PostseasonBracket, "derive", wraps=PostseasonBracket.derive) as derive:
                    self.assertEqual(self.client.get(url).status_code, 200)
                self.assertEqual(derive.call_count, expected)

    def test_a_broken_bracket_does_not_break_the_world_list(self):
        self.advance(self.npb_world, AdvanceTarget.POSTSEASON_END)
        with mock.patch.object(PostseasonBracket, "derive", side_effect=InvalidPostseason("壊れた結果")):
            response = self.client.get(reverse("pennant_index"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, f"{YEAR}年 · 終了")


class WholeFlowOnScreenTest(ScreenCase):
    """フォームで NPB の世界を作る → 公式戦を終える → ボタンで優勝まで → 締める、を画面だけで通す。"""

    def test_create_finish_advance_and_close(self):
        self.client.force_login(self.owner)
        leagues = list(orm_models.League.objects.filter(world__isnull=True).values_list("id", flat=True))
        response = self.client.post(
            reverse("pennant_index"),
            {
                "name": "画面の世界",
                "leagues": [str(league) for league in leagues],
                "managed_team": str(self.managed_source_id),
                "start_year": str(YEAR),
                "seed": "11",
                "postseason_format": "NPB",
            },
        )
        world = orm_models.PennantWorld.objects.get(name="画面の世界")
        self.assertRedirects(response, reverse("pennant_world", args=[world.id]), fetch_redirect_response=False)
        self.assertEqual(world.postseason_format, PostseasonFormat.NPB.value)

        # 公式戦は1週間だけ進めて、残りの対戦を消す（143日を回さない）
        self.post_advance("week", world_id=world.id)
        fixtures = DjangoFixtureRepository(WorldScope.pennant(world.id))
        fixtures.remove(fixtures.find_all())

        self.post_advance("postseason_end", world_id=world.id)
        self.assertIs(build_pennant_postseason_view_service(world.id).status(YEAR), PostseasonStatus.FINISHED)

        with mock.patch(DECIDE_RETIREMENTS, return_value=()):
            response = self.client.post(reverse("pennant_close_season", args=[world.id]), {"expected_year": YEAR})
        self.assertRedirects(
            response, reverse("pennant_offseason", args=[world.id, YEAR]), fetch_redirect_response=False
        )


class CreateFormTest(ScreenCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)

    def _post(self, **overrides):
        data = {
            "name": "方式の世界",
            "leagues": [
                str(league)
                for league in orm_models.League.objects.filter(world__isnull=True).values_list("id", flat=True)
            ],
            "managed_team": str(self.managed_source_id),
            "start_year": str(YEAR),
            "postseason_format": "CHAMPIONS",
        }
        data.update(overrides)
        return self.client.post(reverse("pennant_index"), data)

    def test_the_default_is_the_npb_format_with_descriptions(self):
        response = self.client.get(reverse("pennant_index"))
        form = response.context["form"]

        self.assertEqual(form["postseason_format"].value(), "NPB")
        for postseason_format in PostseasonFormat:
            self.assertContains(response, postseason_format.label)
            self.assertContains(response, postseason_format.description)

    def test_the_chosen_format_is_kept(self):
        self._post()
        self.assertEqual(
            orm_models.PennantWorld.objects.get(name="方式の世界").postseason_format, PostseasonFormat.CHAMPIONS.value
        )

    def test_a_bad_format_is_refused(self):
        for value in ("", "npb", "なし"):
            with self.subTest(value=value):
                response = self._post(postseason_format=value)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context["form"].errors["postseason_format"])
        self.assertFalse(orm_models.PennantWorld.objects.filter(name="方式の世界").exists())


class GameDetailMarkTest(WorldCase):
    """試合詳細のポストシーズンの印と、勝ち上がりの表へのリンク。延長の制限の無い13回以上の試合も開ける。"""

    def _long_postseason_game(self) -> int:
        home = self.pennant_players(self.pennant_team)
        away = self.pennant_players(self.pennant_rival)
        header = GameHeader(
            season=Season(YEAR),
            played_on=date(YEAR, 10, 20),
            home_team_id=self.pennant_team.id,
            away_team_id=self.pennant_rival.id,
            stage=GameStage.POSTSEASON,
        )
        lineup = [
            LineupEntry(
                team_id=team_id,
                player_id=player_id,
                batting_order=order,
                slot_sequence=0,
                fielding_position=FieldingPosition.DESIGNATED_HITTER,
            )
            for team_id, batters in ((self.pennant_team.id, home[:9]), (self.pennant_rival.id, away[:9]))
            for order, player_id in enumerate(batters, start=1)
        ]
        plate_appearances = to_plate_appearances(
            build_scorebook(
                away=[0] * 13,
                home=[0] * 12 + [1],
                away_batters=away[:9],
                home_batters=home[:9],
                away_pitchers={1: away[9]},
                home_pitchers={1: home[9]},
            )
        )
        game = DjangoGameRepository(self.scope).save(domain_services.assemble_game(header, lineup, plate_appearances))
        assert game.id is not None
        return game.id

    def test_a_postseason_game_of_thirteen_innings_opens_with_the_mark(self):
        orm_models.PennantWorld.objects.filter(id=self.world_id).update(postseason_format=PostseasonFormat.NPB.value)
        game_id = self._long_postseason_game()

        response = self.client.get(reverse("pennant_game_detail", args=[self.world_id, game_id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-testid="game-stage"')
        self.assertContains(response, reverse("pennant_postseason_by_year", args=[self.world_id, YEAR]))
        self.assertEqual(len(response.context["detail"].line_score.columns), 13)

    def test_a_regular_game_has_no_mark(self):
        response = self.client.get(reverse("pennant_game_detail", args=[self.world_id, self.pennant_game_id]))
        self.assertNotContains(response, 'data-testid="game-stage"')
