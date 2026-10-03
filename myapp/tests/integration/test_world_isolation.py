"""実データとペナントの世界が混ざらないことの検査（混入検査）。

世界の分離は、全リポジトリ・参照クエリ・管理画面・データ投入コマンドが範囲で絞ることで
成り立つ。**どこか1か所の絞り忘れが混入になり、エラーにならずに画面だけがおかしくなる**ので、
次の3つで機械的に捕まえる。

1. 実データの主要画面（`measure_pages` と同じ一覧）に、世界の目印の名前が出ない
2. 世界の id で実データの URL・API・管理画面を開くと、見つからない
3. myapp の全モデルを「世界にどう属すか」で分類した表を持ち、分類されていないモデル
   （＝絞り込みの要否が決められていないモデル）があれば落とす
"""

import json
import re
from datetime import date

from django.apps import apps
from django.contrib.auth.models import User
from django.test import SimpleTestCase
from django.urls import reverse

from myapp.domain.exceptions import GameNotFound, LeagueNotFound, TeamNotFound
from myapp.domain.pennant.world import WorldScope
from myapp.infrastructure import orm_models
from myapp.infrastructure.queries import (
    DjangoGameListQuery,
    DjangoPlayerFieldingQuery,
    DjangoPlayerSearchQuery,
    DjangoTeamListQuery,
    DjangoTeamPermissionQuery,
)
from myapp.infrastructure.repositories import (
    DjangoGameRepository,
    DjangoLeagueRepository,
    DjangoTeamRepository,
)
from myapp.management.commands.measure_pages import Command as MeasurePages
from myapp.presentation.views import (
    build_pennant_world_service,
    build_permission_query,
    build_player_search_query,
)

from ..helpers import post_game_scorebook
from .world_case import (
    PENNANT_LEAGUE,
    PENNANT_PLAYER_PREFIX,
    PENNANT_RIVAL,
    PENNANT_TEAM,
    PENNANT_WORLD,
    YEAR,
    WorldCase,
)

REAL = WorldScope.real()

# 世界の目印。実データの画面のどこにも出てはならない
MARKERS = (PENNANT_LEAGUE, PENNANT_TEAM, PENNANT_RIVAL, PENNANT_PLAYER_PREFIX, PENNANT_WORLD)


class RealPagesDoNotShowThePennantWorldTest(WorldCase):
    """実データ側の画面に、世界の名前が出ない。"""

    def _assert_no_marker(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, url)
        content = response.content.decode()
        for marker in MARKERS:
            self.assertNotIn(marker, content, f"{url} に世界の目印「{marker}」が出ています")

    def test_the_main_pages_do_not_show_the_world(self):
        """`measure_pages` が測る主要画面と同じ一覧を流用する。"""
        targets = MeasurePages._targets()
        self.assertGreaterEqual(len(targets), 10, "画面の一覧を拾えていません")
        for label, url in targets:
            with self.subTest(page=label):
                self._assert_no_marker(url)

    def test_the_pages_measured_are_the_real_ones(self):
        """測る対象の id は実データの行（世界の行を拾うと、実データの画面を見たことにならない）。"""
        urls = " ".join(url for _, url in MeasurePages._targets())
        self.assertNotIn(f"/team/{self.pennant_team.id}/", urls)
        self.assertNotIn(f"/league/{self.pennant_league.id}/", urls)
        self.assertNotIn(f"/games/{self.pennant_game_id}/", urls)

    def test_other_real_pages_do_not_show_the_world(self):
        pages = [
            reverse("standings_by_year", args=[YEAR]),
            reverse("league_detail_by_year", args=[self.league.id, YEAR]),
            reverse("league_titles_by_year", args=[self.league.id, YEAR]),
            reverse("league_stats", args=[self.league.id]) + "?pos=pitcher",
            reverse("league_stats", args=[self.league.id]) + "?qualified=1",
            reverse("game_list") + f"?year={YEAR}",
            reverse("game_list") + f"?team={self.team.id}",
            reverse("player_list", args=[self.team.id]) + "?pos=pitcher",
        ]
        for url in pages:
            with self.subTest(url=url):
                self._assert_no_marker(url)

    def test_the_dashboard_counts_only_the_real_games(self):
        board = self.service.get_dashboard()

        self.assertEqual([league.league_name for league in board.leagues], [self.league.name])

    def test_standings_count_only_the_real_games(self):
        board = self.service.get_standings(YEAR)

        self.assertEqual([row.team_name for row in board.rows], [self.team.name, self.rival.name])
        self.assertEqual(sum(row.games_played for row in board.rows), 2, "実データの1試合を両チームが数える")

    def test_the_game_list_has_only_the_real_game(self):
        rows = DjangoGameListQuery(REAL).list_rows(year=YEAR)

        self.assertEqual([row.id for row in rows], [self.real_game.id])

    def test_a_player_search_does_not_show_the_copy(self):
        """複製した選手は元の選手と同じ名前を持つ。名前で引くと2人ずつ出てしまう。"""
        # 目印に付け替える前の名前で探す。実データの選手は1人ずつだけ出る
        results = build_player_search_query().search("実打者1")

        self.assertEqual([row.name for row in results], ["実打者1"])

    def test_a_player_search_by_the_marker_finds_nobody(self):
        self.assertEqual(build_player_search_query().search(PENNANT_PLAYER_PREFIX), [])
        response = self.client.get(reverse("player_search") + f"?q={PENNANT_PLAYER_PREFIX}")
        self.assertNotContains(response, PENNANT_PLAYER_PREFIX + "01", status_code=200)

    def test_the_world_search_finds_only_its_own_players(self):
        results = DjangoPlayerSearchQuery(self.scope).search(PENNANT_PLAYER_PREFIX)

        self.assertGreater(len(results), 0)
        self.assertTrue(all(row.name.startswith(PENNANT_PLAYER_PREFIX) for row in results))
        self.assertEqual(DjangoPlayerSearchQuery(self.scope).search("実打者"), [])

    def test_the_team_list_has_only_the_real_teams(self):
        names = {summary.name for summary in DjangoTeamListQuery(REAL).list_summaries()}

        self.assertEqual(names, {self.team.name, self.rival.name})
        self.assertEqual(
            {summary.name for summary in DjangoTeamListQuery(self.scope).list_summaries()},
            {PENNANT_TEAM, PENNANT_RIVAL},
        )

    def test_the_stadium_is_shared(self):
        """球場は共有。世界の球団の本拠地は、実データと同じ球場を指す。"""
        self.assertEqual(self.pennant_team.home_stadium_id, self.stadium.id)


class PennantIdsAreNotFoundFromRealUrlsTest(WorldCase):
    """世界の id で実データ側の URL を開いても、見つからない。"""

    def setUp(self):
        super().setUp()
        # 管理ユーザーなら「権限が無い」ではなく「見つからない」まで進める
        self.admin = User.objects.create_superuser("root", password="x")
        self.client.force_login(self.admin)
        self.pennant_player = self.pennant_players(self.pennant_team)[0]

    def test_pages_by_pennant_ids_are_404(self):
        urls = [
            reverse("player_list", args=[self.pennant_team.id]),
            reverse("player_detail", args=[self.pennant_team.id, self.pennant_player]),
            reverse("player_edit", args=[self.pennant_team.id, self.pennant_player]),
            reverse("league_detail", args=[self.pennant_league.id]),
            reverse("league_detail_by_year", args=[self.pennant_league.id, YEAR]),
            reverse("league_titles", args=[self.pennant_league.id]),
            reverse("league_stats", args=[self.pennant_league.id]),
            reverse("game_detail", args=[self.pennant_game_id]),
            reverse("game_edit", args=[self.pennant_game_id]),
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)

    def test_a_real_team_url_does_not_open_a_pennant_player(self):
        """実データの球団の URL に、世界の選手の id を組み合わせても開かない。"""
        url = reverse("player_detail", args=[self.team.id, self.pennant_player])

        self.assertEqual(self.client.get(url).status_code, 404)

    def test_the_scorebook_api_does_not_save_a_pennant_game(self):
        response = post_game_scorebook(
            self.client,
            self.pennant_game_id,
            {
                "year": YEAR,
                "played_on": "2026-04-02",
                "home_team": 1,
                "away_team": 2,
                "lineup": [],
                "plate_appearances": [],
            },
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(json.loads(response.content)["ok"], False)

    def test_registering_a_player_to_a_pennant_team_adds_nobody(self):
        before = orm_models.PlayerStint.objects.filter(team=self.pennant_team).count()

        response = self.client.post(
            reverse("player_list", args=[self.pennant_team.id]),
            {"name": "紛れ込み", "number": 77, "position": "内野手"},
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(orm_models.PlayerStint.objects.filter(team=self.pennant_team).count(), before)

    def test_creating_a_game_between_pennant_teams_adds_nothing(self):
        before = orm_models.Game.objects.count()

        self.client.post(
            reverse("game_create"),
            {
                "year": YEAR,
                "played_on": "2026-05-01",
                "home_team": self.pennant_team.id,
                "away_team": self.pennant_rival.id,
            },
        )

        self.assertEqual(orm_models.Game.objects.count(), before)

    def test_repositories_do_not_return_the_other_world(self):
        with self.assertRaises(TeamNotFound):
            DjangoTeamRepository(REAL).find_by_id(self.pennant_team.id)
        with self.assertRaises(TeamNotFound):
            DjangoTeamRepository(self.scope).find_by_id(self.team.id)
        with self.assertRaises(LeagueNotFound):
            DjangoLeagueRepository(REAL).find_by_id(self.pennant_league.id)
        with self.assertRaises(LeagueNotFound):
            DjangoLeagueRepository(self.scope).find_by_id(self.league.id)
        with self.assertRaises(GameNotFound):
            DjangoGameRepository(REAL).find_by_id(self.pennant_game_id)
        with self.assertRaises(GameNotFound):
            DjangoGameRepository(self.scope).find_by_id(self.real_game.id)

    def test_every_read_is_limited_to_its_scope(self):
        """読み出しの口をすべて、両方の範囲で開いて、取り違えが無いことを見る。"""
        real_teams = {self.team.id, self.rival.id}
        pennant_teams = {self.pennant_team.id, self.pennant_rival.id}
        for scope, own, other, own_game in (
            (REAL, real_teams, pennant_teams, self.real_game.id),
            (self.scope, pennant_teams, real_teams, self.pennant_game_id),
        ):
            with self.subTest(scope=str(scope)):
                teams, games = DjangoTeamRepository(scope), DjangoGameRepository(scope)
                self.assertEqual({t.id for t in teams.find_all()}, own)
                self.assertEqual({t.id for t in teams.find_all_with_roster()}, own)
                self.assertEqual({g.id for g in games.find_all()}, {own_game})
                self.assertEqual({g.id for g in games.find_all(YEAR)}, {own_game})
                self.assertEqual({g.id for g in games.find_between_teams(own | other)}, {own_game})
                self.assertEqual({g.id for g in games.find_by_team(next(iter(own)))}, {own_game})
                self.assertEqual([g.id for g in games.find_by_team(next(iter(other)))], [])

                listing = DjangoGameListQuery(scope)
                self.assertEqual({g.id for g in listing.list_for_standings()}, {own_game})
                self.assertEqual({r.id for r in listing.list_rows()}, {own_game})
                self.assertEqual(listing.list_seasons(), [YEAR])
                self.assertEqual(listing.latest_year(), YEAR)
                self.assertEqual(set(listing.count_by_team()), own)
                self.assertEqual(listing.list_months(), [4])

    def test_by_league_reads_do_not_cross_over(self):
        self.assertEqual(
            [t.id for t in DjangoTeamRepository(REAL).find_by_league_with_roster(self.pennant_league.id)], []
        )
        self.assertEqual(
            [t.id for t in DjangoTeamRepository(self.scope).find_by_league_with_roster(self.league.id)], []
        )

    def test_the_fielding_query_is_limited_to_its_scope(self):
        line = orm_models.GameFieldingLine.objects.filter(game_id=self.pennant_game_id).first()
        self.assertIsNotNone(line, "前提: 世界の試合に守備成績がある")
        assert line is not None  # mypy 用
        team_id = orm_models.PlayerStint.objects.filter(player_id=line.player_id).get().team_id
        # 世界の試合の守備成績は、世界の範囲でだけ見える
        self.assertIsNotNone(DjangoPlayerFieldingQuery(self.scope).for_player(line.player_id, team_id))
        self.assertIsNone(DjangoPlayerFieldingQuery(REAL).for_player(line.player_id, team_id))


class PermissionTest(WorldCase):
    def test_a_staff_user_cannot_edit_a_pennant_team_from_the_real_side(self):
        staff = User.objects.create_user("staff", password="x", is_staff=True)

        self.assertFalse(build_permission_query().can_manage(staff, self.pennant_team.id))
        self.assertFalse(build_permission_query().can_manage_any(staff, [self.pennant_team.id, self.pennant_rival.id]))

    def test_a_staff_user_can_still_edit_a_real_team(self):
        staff = User.objects.create_user("staff", password="x", is_staff=True)

        self.assertTrue(build_permission_query().can_manage(staff, self.team.id))

    def test_a_manager_of_a_pennant_team_row_cannot_edit_it_from_the_real_side(self):
        manager = User.objects.create_user("gm", password="x")
        self.pennant_team.managers.add(manager)

        self.assertFalse(build_permission_query().can_manage(manager, self.pennant_team.id))

    def test_the_pennant_scope_never_allows_editing(self):
        """ペナントの書き込みはオーナーで判定する（その判定を足すまで、常に False）。"""
        superuser = User.objects.create_superuser("root", password="x")
        query = DjangoTeamPermissionQuery(self.scope)

        self.assertFalse(query.can_manage(superuser, self.pennant_team.id))
        self.assertFalse(query.can_manage(superuser, self.team.id))


class AdminDoesNotShowThePennantWorldTest(WorldCase):
    """管理画面は実データだけを扱う。世界の行は一覧にも選択肢にも編集画面にも出ない。"""

    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser("root", password="x"))
        self.pennant_player = self.pennant_players(self.pennant_team)[0]

    def _get(self, name, *args, query=""):
        return self.client.get(reverse(f"admin:{name}", args=args) + query)

    def test_changelists_do_not_show_the_world(self):
        lists = ["league", "team", "player", "playerstint", "game", "stadium"]
        for model in lists:
            with self.subTest(model=model):
                response = self._get(f"myapp_{model}_changelist")
                self.assertEqual(response.status_code, 200)
                for marker in MARKERS:
                    self.assertNotContains(response, marker)

    def test_the_admin_index_does_not_show_the_world(self):
        response = self.client.get(reverse("admin:index"))

        self.assertEqual(response.status_code, 200)
        for marker in MARKERS:
            self.assertNotContains(response, marker)

    def test_the_pennant_world_model_is_not_registered(self):
        from django.contrib import admin

        self.assertNotIn(orm_models.PennantWorld, admin.site._registry)
        self.assertNotIn(orm_models.PennantPlayerRatings, admin.site._registry)
        self.assertNotIn(orm_models.PennantFixture, admin.site._registry)

    def test_change_pages_by_pennant_ids_do_not_open(self):
        """世界の id を直接指定しても、編集画面は開かない（Django は管理画面の一覧へ戻す）。"""
        pages = [
            ("myapp_league_change", self.pennant_league.id),
            ("myapp_team_change", self.pennant_team.id),
            ("myapp_player_change", self.pennant_player),
            ("myapp_game_change", self.pennant_game_id),
        ]
        for name, pk in pages:
            with self.subTest(page=name):
                response = self._get(name, pk)
                self.assertIn(response.status_code, (302, 404))
                self.assertNotIn(b"\xe7\x9b\xae\xe5\x8d\xb0", response.content)  # 「目印」

    def test_posting_to_a_pennant_change_page_does_not_change_the_world(self):
        response = self.client.post(
            reverse("admin:myapp_team_change", args=[self.pennant_team.id]),
            {"name": "書き換え", "league": self.league.id},
        )

        self.assertIn(response.status_code, (302, 404))
        self.pennant_team.refresh_from_db()
        self.assertEqual(self.pennant_team.name, PENNANT_TEAM)

    def test_deleting_a_pennant_row_from_the_admin_is_not_possible(self):
        response = self.client.post(
            reverse("admin:myapp_league_delete", args=[self.pennant_league.id]), {"post": "yes"}
        )

        self.assertIn(response.status_code, (302, 404))
        self.assertTrue(orm_models.League.objects.filter(id=self.pennant_league.id).exists())

    def test_choices_do_not_offer_the_world(self):
        """プルダウンやフィルタの選択肢にも、世界のリーグ・球団・選手を出さない。"""
        pages = [
            self._get("myapp_team_add"),
            self._get("myapp_playerstint_add"),
            self._get("myapp_game_add"),
            self._get("myapp_team_changelist"),
            self._get("myapp_player_changelist"),
            self._get("myapp_playerstint_changelist"),
            self._get("myapp_game_changelist"),
            self._get("myapp_stadium_change", self.stadium.id),
        ]
        for response in pages:
            self.assertEqual(response.status_code, 200)
            for marker in MARKERS:
                self.assertNotContains(response, marker)

    def test_autocomplete_does_not_offer_the_world(self):
        for model_name, field in (("playerstint", "team"), ("playerstint", "player")):
            with self.subTest(field=field):
                response = self.client.get(
                    reverse("admin:autocomplete"),
                    {"term": "", "app_label": "myapp", "model_name": model_name, "field_name": field},
                )
                self.assertEqual(response.status_code, 200)
                for marker in MARKERS:
                    self.assertNotContains(response, marker)

    def test_editing_a_stadium_does_not_detach_the_world_teams(self):
        """球場の本拠地の付け替えは、世界の球団に及ばない。"""
        form = {
            "name": self.stadium.name,
            "city": self.stadium.city,
            "home_teams": [self.team.id],
            "surface": "",
            "roof": "",
        }

        response = self.client.post(reverse("admin:myapp_stadium_change", args=[self.stadium.id]), form)

        self.assertEqual(response.status_code, 302)
        self.pennant_team.refresh_from_db()
        self.assertEqual(self.pennant_team.home_stadium_id, self.stadium.id)
        self.rival.refresh_from_db()
        self.assertIsNone(self.rival.home_stadium_id, "実データの球団は外される（既存の動作）")


# --- モデルの分類表 -----------------------------------------------------------------

LEAGUE = "リーグ経由で世界に属す"
PLAYER = "選手経由で世界に属す"
GAME = "試合経由で世界に属す"
SHARED = "共有"
PENNANT_ONLY = "ペナント専用"

# モデル名 -> (分類, League.world に至る道)。道は「そのモデルから見た関連名を `__` でつないだもの」。
# 世界に属すモデルは、League.world までの道が実在することを下のテストが確かめる。
# **モデルを足したら、ここに分類を足す。** 分類が無いとテストが落ちる（絞り込みの要否を
# 決めないままにしない）。世界の削除（`DjangoWorldRepository.delete`）も、ここで世界に属すとした
# モデルをすべて消すことを確かめている。
CLASSIFICATION: dict[str, tuple[str, str | None]] = {
    "League": (LEAGUE, "world"),
    "Team": (LEAGUE, "league__world"),
    "PlayerStint": (LEAGUE, "team__league__world"),
    "Captaincy": (LEAGUE, "team__league__world"),
    "Player": (PLAYER, "stints__team__league__world"),
    "PennantPlayerRatings": (PLAYER, "player__stints__team__league__world"),
    "PennantFixture": (LEAGUE, "home_team__league__world"),
    "Game": (GAME, "home_team__league__world"),
    "GameInningScore": (GAME, "game__home_team__league__world"),
    "GameBattingLine": (GAME, "game__home_team__league__world"),
    "GamePitchingLine": (GAME, "game__home_team__league__world"),
    "GameFieldingLine": (GAME, "game__home_team__league__world"),
    "GamePlateAppearance": (GAME, "game__home_team__league__world"),
    "GameRunnerAdvance": (GAME, "plate_appearance__game__home_team__league__world"),
    "GameRunnerSubstitution": (GAME, "plate_appearance__game__home_team__league__world"),
    "GameFieldingError": (GAME, "plate_appearance__game__home_team__league__world"),
    "Stadium": (SHARED, None),
    "PennantWorld": (PENNANT_ONLY, None),
}


def _follow(model, route):
    """モデルから関連をたどる。たどれなければ例外。たどり着いたフィールドを返す。"""
    field = None
    for name in route.split("__"):
        field = model._meta.get_field(name)
        if field.is_relation:
            model = field.related_model
    return model, field


class ModelClassificationTest(SimpleTestCase):
    """全モデルが、世界にどう属すかを決められていること（`test_stat_fields.py` と同じ突き合わせの形）。"""

    def _models(self):
        return {model.__name__: model for model in apps.get_app_config("myapp").get_models()}

    def test_every_model_is_classified(self):
        unclassified = sorted(set(self._models()) - set(CLASSIFICATION))
        self.assertEqual(
            unclassified,
            [],
            f"世界への属し方が決まっていないモデルがあります: {unclassified}。"
            "この表に分類を足し、絞り込み（infrastructure/scoping.py）と世界の削除も直してください。",
        )

    def test_the_table_has_no_stale_entries(self):
        stale = sorted(set(CLASSIFICATION) - set(self._models()))
        self.assertEqual(stale, [], f"存在しないモデルが表に残っています: {stale}")

    def test_models_in_a_world_reach_league_world(self):
        """世界に属すモデルは、書かれた道をたどると League.world に着く。"""
        models = self._models()
        for name, (kind, route) in CLASSIFICATION.items():
            if kind in (SHARED, PENNANT_ONLY):
                self.assertIsNone(route, f"{name} は世界に属さないので道は要りません")
                continue
            with self.subTest(model=name):
                assert route is not None
                model, field = _follow(models[name], route)
                self.assertIs(model, orm_models.PennantWorld)
                self.assertEqual((field.model, field.name), (orm_models.League, "world"))

    def test_shared_models_do_not_point_into_a_world(self):
        """共有のモデルは、世界に属す行を指さない（指すなら共有ではない）。"""
        models = self._models()
        world_models = {
            models[name] for name, (kind, _) in CLASSIFICATION.items() if kind not in (SHARED, PENNANT_ONLY)
        }
        for name, (kind, _) in CLASSIFICATION.items():
            if kind != SHARED:
                continue
            for field in models[name]._meta.get_fields():
                if field.is_relation and field.concrete:
                    self.assertNotIn(field.related_model, world_models, f"{name}.{field.name}")

    def test_the_world_deletion_covers_every_model_that_belongs_to_a_world(self):
        from myapp.infrastructure.repositories import _WORLD_ROWS_CHILD_FIRST

        models = self._models()
        deleted = {model.__name__ for model, _ in _WORLD_ROWS_CHILD_FIRST}
        # 選手・球団・リーグは個別に消す。ここに挙がるのは、行数が多く collector に任せないもの
        individually = {"Player", "Team", "League"}
        expected = {
            name for name, (kind, _) in CLASSIFICATION.items() if kind in (LEAGUE, PLAYER, GAME)
        } - individually
        self.assertEqual(deleted, expected, "世界の削除が、世界に属すモデルと食い違っています")
        self.assertTrue(deleted <= set(models))


# ペナントの画面のリンクが向いてよい、世界の外の行き先（ヘッダーの出口）
HEADER_PAGES = ("/", "/players/", "/pennant/")
HEADER_PREFIXES = ("/accounts/", "/admin/")


class PennantScreensStayInTheWorldTest(WorldCase):
    """ペナントの全画面のリンクが、世界の外（実データの画面）に出ない。"""

    def _hrefs(self, url):
        content = self.client.get(url, follow=True).content.decode()
        return re.findall(r'<a [^>]*href="([^"]*)"', content)

    def _assert_stays_in(self, world_id, urls):
        prefix = f"/pennant/{world_id}/"
        for url in urls:
            for href in self._hrefs(url):
                # "?" と "#" は今の画面の中の切り替え（並べ替え・絞り込み・月の選択）
                allowed = href.startswith((prefix, "?", "#", *HEADER_PREFIXES)) or href in HEADER_PAGES
                self.assertTrue(allowed, f"{url} のリンク {href} が世界の外へ出ています")

    def test_links_in_every_screen_stay_in_the_world(self):
        urls = self.world_urls()
        self.assertGreaterEqual(len(urls), 13)
        self._assert_stays_in(self.world_id, urls)

    def test_links_stay_in_the_world_for_a_staff_user_too(self):
        """管理画面へのリンクが出るユーザーでも、それ以外は世界の中に留まる。"""
        self.client.force_login(User.objects.create_superuser("root", password="x"))

        self._assert_stays_in(self.world_id, self.world_urls())

    def test_the_owner_screens_stay_in_the_world(self):
        """オーナーにだけ出る導線（進める・削除）と、結果のまとめのリンクも、世界の中に留まる。"""
        owner = User.objects.create_user("gm", password="x")
        orm_models.PennantWorld.objects.filter(id=self.world_id).update(
            owner_id=owner.id, managed_team_id=self.pennant_team.id
        )
        orm_models.PennantFixture.objects.create(
            date=date(YEAR, 4, 3), home_team=self.pennant_team, visitor_team=self.pennant_rival
        )
        self.client.force_login(owner)
        urls = [
            reverse("pennant_world", args=[self.world_id]),
            reverse("pennant_world", args=[self.world_id]) + f"?since={YEAR}-04-01",
            reverse("pennant_delete", args=[self.world_id]),
        ]

        self._assert_stays_in(self.world_id, urls)
        prefix = f"/pennant/{self.world_id}/"
        for url in urls:
            content = self.client.get(url).content.decode()
            for action in re.findall(r'<form [^>]*action="([^"]*)"', content):
                allowed = action.startswith((prefix, *HEADER_PREFIXES)) or action in HEADER_PAGES
                self.assertTrue(allowed, f"{url} のフォーム {action} が世界の外へ出ています")
        home = self.client.get(urls[1]).content.decode()
        self.assertIn(f'action="/pennant/{self.world_id}/advance/"', home, "オーナーには進めるフォームが出る")
        self.assertIn(f"/pennant/{self.world_id}/games/{self.pennant_game_id}/", home, "結果のまとめから試合詳細へ")

    def test_the_links_really_are_collected(self):
        """リンクを拾えていなければ、上の検査は何も確かめていない。"""
        hrefs = self._hrefs(reverse("pennant_standings", args=[self.world_id]))

        self.assertTrue(any(href.startswith(f"/pennant/{self.world_id}/team/") for href in hrefs))


class OtherIdsAreNotFoundInAWorldTest(WorldCase):
    """世界の URL に、実データや別の世界の id を組み合わせても開かない。"""

    def setUp(self):
        super().setUp()
        other = build_pennant_world_service().create_world(
            name="別の世界", owner_id=None, source_league_ids=[self.league.id], start_year=YEAR + 1, seed=2
        )
        self.other_id = other.world.id
        self.other_team = orm_models.Team.objects.filter(league__world_id=self.other_id).first()
        self.other_league = self.other_team.league
        self.other_player = orm_models.PlayerStint.objects.filter(team=self.other_team).first().player_id

    def test_real_data_ids_are_404_in_a_world(self):
        real_player = orm_models.PlayerStint.objects.filter(team=self.team).first().player_id
        w = self.world_id
        urls = [
            reverse("pennant_player_list", args=[w, self.team.id]),
            reverse("pennant_player_detail", args=[w, self.team.id, real_player]),
            reverse("pennant_league_detail", args=[w, self.league.id]),
            reverse("pennant_league_titles", args=[w, self.league.id]),
            reverse("pennant_league_stats", args=[w, self.league.id]),
            reverse("pennant_game_detail", args=[w, self.real_game.id]),
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)

    def test_another_worlds_ids_are_404(self):
        w = self.world_id
        urls = [
            reverse("pennant_player_list", args=[w, self.other_team.id]),
            reverse("pennant_player_detail", args=[w, self.other_team.id, self.other_player]),
            reverse("pennant_league_detail", args=[w, self.other_league.id]),
            reverse("pennant_league_stats", args=[w, self.other_league.id]),
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)

    def test_a_player_of_this_world_is_not_opened_under_another_team(self):
        """球団と選手の組み合わせが世界の中で合っていても、別の世界の球団の下では開かない。"""
        player = self.pennant_players(self.pennant_team)[0]

        url = reverse("pennant_player_detail", args=[self.other_id, self.pennant_team.id, player])

        self.assertEqual(self.client.get(url).status_code, 404)

    def test_the_same_screen_in_the_other_world_shows_only_its_own_data(self):
        """同じ名前の球団がある2つの世界（目印は片方だけ）で、目印が混ざらない。"""
        content = self.client.get(reverse("pennant_standings", args=[self.other_id])).content.decode()

        self.assertNotIn(PENNANT_TEAM, content)
        for url in self.world_urls(self.other_id):
            with self.subTest(url=url):
                self.assertNotIn(PENNANT_TEAM, self.client.get(url, follow=True).content.decode())
