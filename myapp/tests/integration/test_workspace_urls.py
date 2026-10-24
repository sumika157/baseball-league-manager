"""URL にワークスペースを入れる（W2 T4。#78）。

ワークスペースの中の画面は `/w/<key>/...`。key は URL のパスコンバータ（形はドメインの `KEY_LENGTH`・`KEY_ALPHABET`）
が受け、`workspace_view` が解決する。ここでは URL の形と、ワークスペースの外（`/`・ログイン・404）との行き来を見る。
全パターンの漏れの検査（分類表）は `test_workspace_isolation.py` の `EveryWorkspaceUrlTest`。

- 古い URL（`/teams/` など）は 404（リダイレクトしない）。形の合わない key は DB を読まずに 404、無い key は 404。
- `/`: 未ログインは紹介と見本のワークスペースへのリンク（設定があり、公開のときだけ）、ログインしていれば自分の
  ワークスペースの一覧。ログインの後は `/` へ。非公開のワークスペースの URL からログインすると、同じ URL に戻る。
- 書き込みの後のリダイレクトと API の `redirect_url` は、同じ key の下に戻る（`_url` が今の要求の key を足す）。
- `404.html` はワークスペースの外でも描ける（ヘッダーの中の画面へのリンクを組もうとして 500 にならない）。
"""

import ast
import re
from pathlib import Path

from django.contrib.auth.models import User
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from myapp.domain.workspace import KEY_ALPHABET, KEY_LENGTH, Visibility
from myapp.infrastructure import orm_models
from myapp.presentation import views
from myapp.presentation.views import build_pennant_world_service

from ..helpers import (
    DEFAULT_WORKSPACE_KEY,
    build_workspace_access_query,
    login_as_manager,
    make_editor,
    make_workspace,
    play_game,
    real_scope,
    ws_url,
)
from .base import BaseCase

PRESENTATION = Path(__file__).resolve().parents[2] / "presentation"


class OldAndMalformedUrlsTest(TestCase):
    """ワークスペースの外に、古い URL は残っていない。key の形が合わなければ、DB を読まずに 404。"""

    def test_the_old_urls_are_404(self):
        make_workspace(key=DEFAULT_WORKSPACE_KEY)
        for url in ("/teams/", "/players/", "/standings/", "/games/", "/pennant/", "/team/1/", "/analysis/"):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)
        api = self.client.post("/api/games/1/scorebook/", data="{}", content_type="application/json")
        self.assertEqual(api.status_code, 404)

    def test_a_key_of_the_wrong_shape_is_404_without_reading_the_database(self):
        too_long = "a" * (KEY_LENGTH + 1)
        for key in ("a" * (KEY_LENGTH - 1), too_long, "A" * KEY_LENGTH, "-" * KEY_LENGTH, "ａ" * KEY_LENGTH):
            with self.subTest(key=key), self.assertNumQueries(0):
                self.assertEqual(self.client.get(f"/w/{key}/teams/").status_code, 404)

    def test_the_shape_comes_from_the_domain(self):
        """コンバータの形はドメインの定数から作る（複製しない）。"""
        from myapp.presentation.converters import WorkspaceKeyConverter

        pattern = re.compile(WorkspaceKeyConverter.regex)
        self.assertTrue(pattern.fullmatch(KEY_ALPHABET[:KEY_LENGTH]))
        self.assertIsNone(pattern.fullmatch(KEY_ALPHABET[: KEY_LENGTH - 1]))

    def test_an_unknown_key_is_404(self):
        make_workspace(key=DEFAULT_WORKSPACE_KEY)
        response = self.client.get(ws_url("team_list", workspace=orm_models.Workspace(key="nosuchws00")))
        self.assertEqual(response.status_code, 404)


class TopPageTest(TestCase):
    """`/`（ワークスペースの外のトップ）。作成フォームは無い（W4）。"""

    def setUp(self):
        self.owner = User.objects.create_user(username="ws-owner", password="x")
        self.sample = make_workspace(self.owner, "見本のリーグ", key="sample0001")

    def test_anonymous_sees_the_introduction_and_the_entrances(self):
        content = self.client.get(reverse("home")).content.decode()

        self.assertIn(f'href="{reverse("signup")}"', content)
        self.assertIn(f'href="{reverse("login")}"', content)
        self.assertNotIn("見本のワークスペース", content, "設定が無ければ見本のリンクを出さない")

    def test_the_sample_link_is_shown_only_when_set_existing_and_public(self):
        link = f'href="{ws_url("dashboard", workspace=self.sample)}"'
        with override_settings(SAMPLE_WORKSPACE_KEY="sample0001"):
            self.assertIn(link, self.client.get(reverse("home")).content.decode())
        with override_settings(SAMPLE_WORKSPACE_KEY="nosuchws00"):
            self.assertNotIn("見本のワークスペース", self.client.get(reverse("home")).content.decode())
        orm_models.Workspace.objects.filter(id=self.sample.id).update(visibility=Visibility.PRIVATE.value)
        with override_settings(SAMPLE_WORKSPACE_KEY="sample0001"):
            self.assertNotIn("見本のワークスペース", self.client.get(reverse("home")).content.decode())

    def test_a_logged_in_user_sees_the_workspaces_they_belong_to_in_name_order(self):
        user = User.objects.create_user(username="member", password="x")
        owned = make_workspace(user, "ん　最後の名前", key="ownedws001", visibility=Visibility.PRIVATE)
        edited = make_workspace(name="あ　最初の名前", key="editedws01")
        make_editor(user, edited)
        managed = make_workspace(name="か　担当の名前", key="managedws1")
        team = orm_models.Team.objects.create(
            league=orm_models.League.objects.create(name="担当のリーグ", workspace=managed), name="担当の球団"
        )
        team.managers.add(user)
        self.client.force_login(user)

        response = self.client.get(reverse("home"))

        rows = [(row.key, row.role.label, row.visibility.label) for row in response.context["workspaces"]]
        self.assertEqual(
            rows,
            [
                ("editedws01", "編集者", "公開"),
                ("managedws1", "チーム担当", "公開"),
                ("ownedws001", "持ち主", "非公開"),
            ],
        )
        content = response.content.decode()
        for workspace in (owned, edited, managed):
            self.assertIn(f'href="{ws_url("dashboard", workspace=workspace)}"', content)
        self.assertNotIn("見本のリーグ", content, "入っていない公開ワークスペースは一覧に出さない")
        self.assertNotIn('<form method="post" class="card', content, "作成フォームは W4")

    def test_the_list_reads_once_whatever_the_number_of_workspaces(self):
        """一覧はワークスペースの数に比例しない（`Exists` の注釈で1本）。"""
        user = User.objects.create_user(username="counter", password="x")
        make_workspace(user, "1つ目", key="countws001")
        self.client.force_login(user)
        self.client.get(reverse("home"))  # セッションを温める
        with self.assertNumQueries(3) as one:  # セッション・利用者・一覧
            self.client.get(reverse("home"))
        for number in range(2, 5):
            make_workspace(user, f"{number}つ目", key=f"countws00{number}")
        with self.assertNumQueries(len(one.captured_queries)):
            response = self.client.get(reverse("home"))
        self.assertEqual(len(response.context["workspaces"]), 4)


class LoginRoundTripTest(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="ws-owner", password="pw-123456")
        self.workspace = make_workspace(self.owner, "非公開", key="privatews1", visibility=Visibility.PRIVATE)

    def test_after_login_the_user_goes_to_the_top(self):
        response = self.client.post(reverse("login"), {"username": "ws-owner", "password": "pw-123456"})

        self.assertRedirects(response, reverse("home"))

    def test_a_private_url_sends_to_login_and_comes_back_to_the_same_url(self):
        url = ws_url("team_list", workspace=self.workspace)

        first = self.client.get(url)
        self.assertRedirects(first, f"{reverse('login')}?next={url}", fetch_redirect_response=False)

        login = self.client.post(
            f"{reverse('login')}?next={url}", {"username": "ws-owner", "password": "pw-123456", "next": url}
        )
        self.assertRedirects(login, url)

    def test_a_logged_in_outsider_gets_404_and_does_not_learn_it_exists(self):
        self.client.force_login(User.objects.create_user(username="outsider", password="x"))

        response = self.client.get(ws_url("team_list", workspace=self.workspace))

        self.assertEqual(response.status_code, 404)
        self.assertNotContains(response, "非公開", status_code=404)


class RedirectsStayUnderTheKeyTest(BaseCase):
    """書き込みの後のリダイレクトは、同じ key の下に戻る。"""

    def setUp(self):
        super().setUp()
        self.inside = f"/w/{self.workspace.key}/"

    def assert_inside(self, response, expected: str) -> None:
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(self.inside), response["Location"])
        self.assertEqual(response["Location"].split("?")[0], expected)

    def test_registering_and_editing_a_player(self):
        login_as_manager(self.client, self.team)
        registered = self.client.post(
            ws_url("player_list", self.team.id), {"name": "山田", "number": "10", "position": "内野手"}
        )
        self.assert_inside(registered, ws_url("player_list", self.team.id))
        player = orm_models.PlayerStint.objects.get(team=self.team).player_id

        retired = self.client.post(ws_url("player_edit", self.team.id, player), {"retire": "1"})

        self.assert_inside(retired, ws_url("player_list", self.team.id))

    def test_creating_a_game(self):
        login_as_manager(self.client, self.team)

        response = self.client.post(
            ws_url("game_create"),
            {"year": "2026", "played_on": "2026-04-01", "home_team": self.team.id, "away_team": self.rival.id},
        )

        self.assert_inside(response, ws_url("game_edit", orm_models.Game.objects.get().id))

    def test_the_analysis_entrance(self):
        response = self.client.get(f"{ws_url('analysis_index')}?team={self.rival.id}&year=2026")

        self.assert_inside(response, ws_url("team_analysis", self.rival.id))
        self.assertIn("year=2026", response["Location"])

    def test_the_scorebook_api_returns_a_url_under_the_key(self):
        """API の `redirect_url`（保存後に開く試合の画面）も key の下。保存の中身は `test_game_scorebook_api.py`。"""
        login_as_manager(self.client, self.team, self.rival)
        game = play_game(self.team, self.rival, home_score=0, away_score=0, recorded=False)
        payload = {
            "year": 2026,
            "played_on": "2026-04-01",
            "home_team": self.team.id,
            "away_team": self.rival.id,
            "lineup": [],
            "plate_appearances": [],
            "start_time": "",
            "end_time": "",
            "ends_next_day": False,
            "interruption_minutes": "",
            "attendance": "",
        }

        response = self.client.post(
            ws_url("api_game_scorebook", game.id), data=payload, content_type="application/json"
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["redirect_url"], ws_url("game_detail", game.id))

    def test_the_pennant_writes(self):
        self.skip_roster_check()
        owner = make_editor(User.objects.create_user(username="gm", password="x"))
        world = (
            build_pennant_world_service(real_scope())
            .create_world(
                name="戻り先の世界", owner_id=owner.id, source_league_ids=[self.league.id], start_year=2030, seed=1
            )
            .world.id
        )
        self.client.force_login(owner)
        home = ws_url("pennant_world", world)

        self.assert_inside(self.client.get(ws_url("pennant_advance", world)), home)
        self.assert_inside(self.client.post(ws_url("pennant_advance", world), {"target": "no-such"}), home)
        self.assert_inside(self.client.get(ws_url("pennant_close_season", world)), home)
        self.assert_inside(self.client.post(ws_url("pennant_close_season", world), {}), home)
        self.assert_inside(self.client.post(ws_url("pennant_delete", world)), ws_url("pennant_index"))


class UrlHelperTest(TestCase):
    """views の `_url` は今の要求のワークスペースの key を足す。views・API は `reverse` を `_url` の外で呼ばない。"""

    def test_the_key_of_the_request_is_put_first(self):
        workspace = make_workspace(key="otherkey01")
        request = RequestFactory().get("/")
        request.workspace = build_workspace_access_query().resolve(workspace.key, None)

        self.assertEqual(views._url(request, "player_list", 3), "/w/otherkey01/team/3/")

    def test_a_request_without_a_workspace_fails_loudly(self):
        with self.assertRaises(views.WorkspaceNotResolved):
            views._url(RequestFactory().get("/"), "team_list")


class NoBareReverseTest(SimpleTestCase):
    """views・API が中の画面の URL を組むのは `_url` だけ（key を足し忘れた URL を作らない）。"""

    ALLOWED = {("views.py", "_url"), ("views.py", "SignUpView")}

    def test_reverse_and_redirect_by_name_are_only_in_the_helper(self):
        offenders = []
        for name in ("views.py", "api.py"):
            tree = ast.parse((PRESENTATION / name).read_text(encoding="utf-8"))
            for owner in tree.body:
                owner_name = getattr(owner, "name", "")
                for node in ast.walk(owner):
                    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                        continue
                    by_name = node.func.id == "redirect" and node.args and isinstance(node.args[0], ast.Constant)
                    builds_a_url = node.func.id in ("reverse", "reverse_lazy") or by_name
                    if builds_a_url and (name, owner_name) not in self.ALLOWED:
                        offenders.append(f"{name}:{node.lineno} {owner_name}")
        self.assertEqual(offenders, [], "中の画面の URL は _url(request, …) で組んでください")


class NotFoundPageTest(TestCase):
    """`404.html` はワークスペースの外でも描ける（再発防止。中の画面へのリンクで NoReverseMatch・500 にしない）。"""

    @override_settings(DEBUG=False)
    def test_the_404_page_outside_a_workspace(self):
        response = self.client.get("/no-such-page/")

        self.assertEqual(response.status_code, 404)
        self.assertTemplateUsed(response, "404.html")
        self.assertContains(response, f'href="{reverse("home")}"', status_code=404)
        self.assertNotContains(response, "選手を探す", status_code=404)

    @override_settings(DEBUG=False)
    def test_the_404_page_inside_a_workspace_keeps_its_header(self):
        workspace = make_workspace(key=DEFAULT_WORKSPACE_KEY)

        response = self.client.get(ws_url("player_list", 999999, workspace=workspace))

        self.assertEqual(response.status_code, 404)
        self.assertContains(response, f'action="{ws_url("player_search", workspace=workspace)}"', status_code=404)
