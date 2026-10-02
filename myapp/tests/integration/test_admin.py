"""管理画面の表示。一覧・トップの概況・グルーピング。"""

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from myapp.domain.value_objects import (
    BattingLine,
)
from myapp.infrastructure import orm_models

from ..helpers import (
    give_batting,
    play_game,
)
from .base import BaseCase


class AdminTest(BaseCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))

    def test_admin_pages_use_the_admin_theme(self):
        for url in ["/admin/", "/admin/myapp/player/", "/admin/myapp/game/"]:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertContains(response, "myapp/css/admin-theme.css")
                self.assertNotContains(response, "myapp/css/theme.css")

    def test_site_pages_do_not_use_the_admin_theme(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "myapp/css/theme.css")
        self.assertNotContains(response, "myapp/css/admin-theme.css")

    def test_game_list_shows_the_result(self):
        play_game(self.team, self.rival, home_score=5, away_score=3)
        response = self.client.get("/admin/myapp/game/")
        self.assertContains(response, "テストチーム の勝ち")

    def test_game_edit_has_stat_inlines(self):
        game = play_game(self.team, self.rival)
        response = self.client.get(f"/admin/myapp/game/{game.id}/change/")
        self.assertContains(response, "打撃成績")
        self.assertContains(response, "投球成績")

    def test_player_list_shows_appearances(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        give_batting(self.team, self.rival, player.id, BattingLine(at_bats=4, singles=1))

        response = self.client.get("/admin/myapp/player/")
        self.assertContains(response, "field-appearances")


class AdminIndexTest(BaseCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))

    def test_models_are_labelled_in_japanese(self):
        response = self.client.get("/admin/")
        for label in ["野球データ", "リーグ", "チーム", "選手", "試合"]:
            with self.subTest(label=label):
                self.assertContains(response, label)

    def test_models_follow_domain_order(self):
        body = self.client.get("/admin/").content.decode()
        positions = [
            body.index("/admin/myapp/league/"),
            body.index("/admin/myapp/team/"),
            body.index("/admin/myapp/player/"),
            body.index("/admin/myapp/game/"),
        ]
        self.assertEqual(positions, sorted(positions))

    def test_baseball_data_comes_before_auth(self):
        body = self.client.get("/admin/").content.decode()
        self.assertLess(body.index("/admin/myapp/"), body.index("/admin/auth/"))

    def test_overview_counts(self):
        self.service.register_player(self.team.id, "山田", 10, "内野手")
        self.service.register_player(self.team.id, "佐藤", 18, "投手")

        overview = self.service.get_admin_overview()

        self.assertEqual(overview.team_count, 2)
        self.assertEqual(overview.player_count, 2)
        self.assertEqual(overview.pitcher_count, 1)

    def test_overview_flags_players_without_stats(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        self.assertEqual(self.service.get_admin_overview().players_without_stats, 1)

        give_batting(self.team, self.rival, player.id, BattingLine(at_bats=10, singles=3))
        self.assertEqual(self.service.get_admin_overview().players_without_stats, 0)

    def test_overview_flags_empty_teams_and_retired_players(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        self.service.retire_player(self.team.id, player.id)

        overview = self.service.get_admin_overview()

        self.assertEqual(overview.teams_without_players, 2)
        self.assertEqual(overview.retired_count, 1)

    def test_notes_appear_on_the_page(self):
        self.service.register_player(self.team.id, "山田", 10, "内野手")
        self.assertContains(self.client.get("/admin/"), "成績が未入力の選手")


class AdminGroupingTest(BaseCase):
    """管理画面の一覧をリーグ・チームごとに区切る。

    標準テンプレートを差し替えているため、グループ化しない一覧が
    従来どおり出ることも確かめる。
    """

    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))
        self.other = orm_models.League.objects.create(name="別リーグ")
        self.x = orm_models.Team.objects.create(league=self.other, name="Xチーム")
        self.service.register_player(self.team.id, "山田", 10, "内野手")
        self.service.register_player(self.x.id, "田中", 20, "外野手")

    def test_team_list_has_league_headings(self):
        response = self.client.get("/admin/myapp/team/")

        self.assertContains(response, "group-heading-row")
        self.assertContains(response, "テストリーグ")
        self.assertContains(response, "別リーグ")

    def test_team_list_shows_each_league_once(self):
        body = self.client.get("/admin/myapp/team/").content.decode()
        # 見出しには所属チーム数も添える
        # 見出しには絞り込みリンクも付くため、見出し文言そのもので数える
        self.assertEqual(body.count("テストリーグ（2チーム）"), 1)

    def test_stint_list_groups_by_team(self):
        """所属はもう選手ではなく在籍が持つので、区切るのは在籍一覧。"""
        response = self.client.get("/admin/myapp/playerstint/")

        self.assertContains(response, "group-heading-row")
        self.assertContains(response, "テストリーグ · テストチーム")

    def _manually_ordered_teams(self):
        """手動の並びが名前順とは異なるチームを作る。"""
        for order, name in enumerate(("Cチーム", "Aチーム", "Bチーム"), start=1):
            orm_models.Team.objects.create(league=self.other, name=name, display_order=order)
        return ("Cチーム", "Aチーム", "Bチーム")

    def test_columns_cannot_be_sorted(self):
        """列での並べ替えは持たない。

        ドラッグした順がそのまま保存される順なので、列で並べ替えると
        見えている順と食い違う。両立しないため並べ替え自体を置かない。
        """
        for url in ("/admin/myapp/team/", "/admin/myapp/league/"):
            with self.subTest(url=url):
                body = self.client.get(url).content.decode()

                self.assertIn('id="result_list"', body)
                self.assertNotIn("?o=", body)
                self.assertNotIn("sortoptions", body)

    def test_sorting_written_in_the_url_is_dropped(self):
        """古いリンクや履歴から来ても、並べ替えられない状態に入らない。"""
        manual_order = self._manually_ordered_teams()

        response = self.client.get("/admin/myapp/team/?o=1")

        self.assertRedirects(response, "/admin/myapp/team/")
        body = self.client.get("/admin/myapp/team/?o=1", follow=True).content.decode()
        # 名前順ではなく手動の順のまま
        positions = [body.index(name) for name in manual_order]
        self.assertEqual(positions, sorted(positions))
        # 区切りも崩れず、ドラッグもできる
        self.assertEqual(body.count("テストリーグ（2チーム）"), 1)
        self.assertIn("admin-inline-sortable.js", body)

    def test_dropping_the_sort_keeps_the_other_parameters(self):
        """並べ替えだけを落とし、絞り込みは保つ。"""
        response = self.client.get(f"/admin/myapp/team/?league__id__exact={self.other.id}&o=1")

        self.assertRedirects(response, f"/admin/myapp/team/?league__id__exact={self.other.id}")

    def test_league_filter_is_still_available(self):
        """リーグを1つに絞る手段は絞り込みパネルが担う。"""
        self._manually_ordered_teams()

        body = self.client.get(f"/admin/myapp/team/?league__id__exact={self.other.id}").content.decode()

        self.assertNotIn("テストチーム", body)
        self.assertIn("Aチーム", body)

    def test_other_changelists_are_unaffected(self):
        """group_by を持たない一覧は従来どおり描画されること。"""
        for url in ["/admin/myapp/league/", "/admin/myapp/game/", "/admin/auth/user/"]:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, "group-heading-row")

    def test_result_rows_are_still_rendered(self):
        body = self.client.get("/admin/myapp/team/").content.decode()

        self.assertIn("result_list", body)
        self.assertIn("テストチーム", body)
        self.assertIn("Xチーム", body)


class LeagueAccordionTest(BaseCase):
    """リーグ一覧で所属チームを折りたたんで確認できること。"""

    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))
        self.service.register_player(self.team.id, "山田", 10, "内野手")
        self.service.register_player(self.team.id, "佐藤", 18, "投手")

    def _body(self):
        return self.client.get("/admin/myapp/league/").content.decode()

    def test_teams_are_listed_inside_details(self):
        body = self._body()

        self.assertIn('<details class="team-accordion">', body)
        self.assertIn("テストチーム", body)
        self.assertIn("相手チーム", body)

    def test_summary_shows_the_count(self):
        self.assertIn("<summary>2チーム</summary>", self._body())

    def test_each_team_links_to_its_edit_page(self):
        self.assertIn(f"/admin/myapp/team/{self.team.id}/change/", self._body())

    def test_active_player_count_is_shown(self):
        """在籍中の人数を出す。退団した選手は数えない。"""
        self.assertIn("2名", self._body())

    def test_retired_players_are_not_counted(self):
        player = self.service.register_player(self.team.id, "退団", 99, "内野手")
        self.service.retire_player(self.team.id, player.id)

        # 在籍中は2名のまま
        self.assertIn("2名", self._body())

    def test_league_without_teams(self):
        orm_models.League.objects.create(name="空リーグ")
        body = self._body()

        self.assertIn("空リーグ", body)
        self.assertIn("—", body)

    def test_team_names_are_escaped(self):
        """チーム名をそのまま埋め込まないこと。"""
        orm_models.Team.objects.create(league=self.league, name="<script>x</script>")

        self.assertNotIn("<script>x</script>", self._body())

    def test_query_count_does_not_grow_with_rows(self):
        """行ごとにチームを引くと一覧で N+1 になるため、先読みしている。

        リーグを増やしても問い合わせ数が変わらないことを確かめる
        （所属チームはまとめて1回で取る）。
        """

        def count_queries():
            with CaptureQueriesContext(connection) as captured:
                self.client.get("/admin/myapp/league/")
            return len(captured)

        before = count_queries()

        for i in range(5):
            league = orm_models.League.objects.create(name=f"L{i}")
            orm_models.Team.objects.create(league=league, name=f"T{i}")

        self.assertEqual(count_queries(), before)


class GameAdminReadOnlyTest(BaseCase):
    """試合の得点・成績は打席から導く値なので、管理画面からは書き換えられない。

    管理画面はスコアブックの照合を通らない。直せてしまうと、打席の記録と食い違った
    得点・成績が保存されて集計がずれる。
    """

    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        self.game = play_game(
            self.team,
            self.rival,
            home_score=2,
            away_score=1,
            batting={player.id: BattingLine(at_bats=4, singles=2)},
        )
        self.url = f"/admin/myapp/game/{self.game.id}/change/"

    def test_scores_are_read_only_on_the_change_form(self):
        response = self.client.get(self.url)

        admin_form = response.context["adminform"]
        self.assertIn("home_score", admin_form.readonly_fields)
        self.assertIn("away_score", admin_form.readonly_fields)
        self.assertNotIn("home_score", admin_form.form.fields)
        self.assertContains(response, "スコアブック画面")

    def test_inline_counts_are_read_only_and_rows_cannot_be_added_or_deleted(self):
        response = self.client.get(self.url)

        self.assertNotContains(response, 'name="batting_lines-0-at_bats"')
        self.assertNotContains(response, 'name="pitching_lines-0-')
        for inline_formset in response.context["inline_admin_formsets"]:
            inline = inline_formset.opts
            with self.subTest(inline=type(inline).__name__):
                self.assertFalse(inline.has_add_permission(response.wsgi_request, self.game))
                self.assertFalse(inline.has_delete_permission(response.wsgi_request, self.game))
                self.assertFalse(inline.has_change_permission(response.wsgi_request, self.game))

    def test_the_derived_lines_are_shown_as_inlines(self):
        """守備成績・イニングスコアも閲覧できる（書き換えられないものとして）。"""
        response = self.client.get(self.url)

        for label in ("イニングスコア", "打撃成績", "投球成績", "守備成績"):
            with self.subTest(label=label):
                self.assertContains(response, label)

    def test_a_post_cannot_change_the_scores(self):
        response = self.client.post(
            self.url,
            {
                "year": 2026,
                "played_on": "2026-04-01",
                "home_score": 9,
                "away_score": 9,
                "_save": "1",
                # 読み取り専用の明細も管理フォームの枠（ManagementForm）は送られてくる
                **{
                    f"{prefix}-{key}": 0
                    for prefix in ("inning_scores", "batting_lines", "pitching_lines", "fielding_lines")
                    for key in ("TOTAL_FORMS", "INITIAL_FORMS")
                },
            },
        )

        self.assertEqual(response.status_code, 302)
        saved = orm_models.Game.objects.get(pk=self.game.id)
        self.assertEqual((saved.home_score, saved.away_score), (2, 1))
        self.assertEqual(orm_models.GameBattingLine.objects.get(game=saved).at_bats, 4)

    def test_the_matchup_of_a_saved_game_is_fixed(self):
        response = self.client.get(self.url)

        self.assertNotIn("home_team", response.context["adminform"].form.fields)

    def test_a_forged_post_cannot_add_or_change_lines(self):
        """画面に出ていない行を送りつけても、追加も書き換えもされない。"""
        line = orm_models.GameBattingLine.objects.get(game_id=self.game.id)
        payload = {
            "year": 2026,
            "played_on": "2026-04-01",
            "_save": "1",
            "inning_scores-TOTAL_FORMS": 0,
            "inning_scores-INITIAL_FORMS": 0,
            "pitching_lines-TOTAL_FORMS": 0,
            "pitching_lines-INITIAL_FORMS": 0,
            "fielding_lines-TOTAL_FORMS": 0,
            "fielding_lines-INITIAL_FORMS": 0,
            "batting_lines-TOTAL_FORMS": 2,
            "batting_lines-INITIAL_FORMS": 1,
            "batting_lines-0-id": line.id,
            "batting_lines-0-game": self.game.id,
            "batting_lines-0-at_bats": 99,
            "batting_lines-1-game": self.game.id,
            "batting_lines-1-player": line.player_id,
            "batting_lines-1-at_bats": 5,
        }

        self.client.post(self.url, payload)

        self.assertEqual(orm_models.GameBattingLine.objects.filter(game_id=self.game.id).count(), 1)
        line.refresh_from_db()
        self.assertEqual(line.at_bats, 4)


class PlayerDeleteProtectedTest(BaseCase):
    """打席に登場する選手は削除できない（他の選手の打席・走者の記録まで巻き込んで消えるため）。"""

    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))
        self.batter = self.service.register_player(self.team.id, "打者", 10, "内野手")
        self.pitcher = self.service.register_player(self.rival.id, "投手", 18, "投手")
        self.runner = self.service.register_player(self.team.id, "走者", 11, "内野手")
        self.game = play_game(self.team, self.rival, home_score=0, away_score=0)
        entry = orm_models.GamePlateAppearance.objects.create(
            game_id=self.game.id,
            sequence=1,
            inning=1,
            is_bottom=True,
            batter_id=self.batter.id,
            pitcher_id=self.pitcher.id,
            batting_order=1,
            result="四球",
        )
        # 他の選手の打席に走者として載っている
        orm_models.GameRunnerAdvance.objects.create(
            plate_appearance=entry, runner_id=self.runner.id, from_base=0, to_base=1, reason="打撃"
        )

    def _delete(self, player_id, **extra):
        return self.client.post(f"/admin/myapp/player/{player_id}/delete/", {"post": "yes"}, **extra)

    def test_the_confirmation_page_explains_why_it_cannot_be_deleted(self):
        for player in (self.batter, self.pitcher, self.runner):
            with self.subTest(player=player.name):
                response = self.client.get(f"/admin/myapp/player/{player.id}/delete/")

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "削除できません")

    def test_posting_the_deletion_does_not_delete_or_crash(self):
        for player in (self.batter, self.pitcher, self.runner):
            with self.subTest(player=player.name):
                response = self._delete(player.id)

                self.assertEqual(response.status_code, 200)  # 500 にならず、確認画面に戻る
                self.assertTrue(orm_models.Player.objects.filter(pk=player.id).exists())
        self.assertEqual(orm_models.GamePlateAppearance.objects.count(), 1)
        self.assertEqual(orm_models.GameRunnerAdvance.objects.count(), 1)

    def test_a_player_without_any_plate_appearance_can_still_be_deleted(self):
        bystander = self.service.register_player(self.team.id, "控え", 99, "内野手")

        response = self._delete(bystander.id)

        self.assertEqual(response.status_code, 302)
        self.assertFalse(orm_models.Player.objects.filter(pk=bystander.id).exists())
