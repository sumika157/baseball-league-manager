"""`measure_pages`（主要画面の実測）が、指したワークスペースの画面を測ること（W2 T3。#78）。

`--workspace <key>` は必須（省いた・無い key は日本語のエラー）。画面の id はそのワークスペースの実データから拾い、
`--world` はそのワークスペースの世界だけを受け付ける。`--as` はログインして測る。
"""

from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError

from myapp.infrastructure import orm_models
from myapp.management.commands.measure_pages import Command as MeasurePages

from ..helpers import (
    DEFAULT_WORKSPACE_KEY,
    make_league,
    make_pennant_world,
    make_workspace,
    play_game,
    real_scope,
    register_players,
    ws_url,
)
from .world_case import YEAR, WorldCase


def _measure(*args: str) -> list[str]:
    """コマンドを流し、画面ごとの行を返す。

    見出しと区切りの2行の後から、最初の空行（遅い画面のまとめの前に出る）までが画面の行。
    """
    out = StringIO()
    call_command("measure_pages", *args, stdout=out, stderr=StringIO())
    rows = []
    for line in out.getvalue().splitlines()[2:]:
        if not line.strip():
            break
        rows.append(line)
    return rows


class MeasurePagesTest(WorldCase):
    """既定のワークスペース（今の URL が開くもの）に実データと世界が1つずつ。B に別のチームと試合。"""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.workspace_b = make_workspace(name="ワークスペースB")
        league = make_league("Bリーグ", workspace=cls.workspace_b)
        cls.b_home = orm_models.Team.objects.create(league=league, name="Bホーム")
        cls.b_away = orm_models.Team.objects.create(league=league, name="Bビジター")
        register_players(cls.b_home, [("B打者", 1, "内野手")])
        register_players(cls.b_away, [("B投手", 11, "投手")])
        cls.b_game_id = play_game(cls.b_home, cls.b_away, year=YEAR, scope=real_scope(cls.workspace_b)).id

    def test_omitting_the_workspace_is_an_error(self):
        with self.assertRaisesMessage(CommandError, "--workspace でワークスペースの key を指定してください"):
            _measure()

    def test_an_unknown_key_is_an_error(self):
        with self.assertRaisesMessage(CommandError, "ワークスペースが見つかりません（key=zzzzzzzzzz）"):
            _measure("--workspace", "zzzzzzzzzz")

    def test_it_measures_every_page_of_the_workspace(self):
        rows = _measure("--workspace", DEFAULT_WORKSPACE_KEY)

        self.assertEqual(len(rows), 16)
        for row in rows:
            with self.subTest(row=row):
                self.assertIn(" 200 ", row)

    def test_the_ids_come_from_the_given_workspace(self):
        urls = [url for _, url in MeasurePages._targets(real_scope(self.workspace_b), self.workspace_b.key)]

        self.assertIn(ws_url("game_detail", self.b_game_id, workspace=self.workspace_b), urls)
        b_teams = {self.b_home.id, self.b_away.id}
        team_pages = [url for url in urls if "/team/" in url]
        self.assertTrue(team_pages)
        for url in team_pages:
            self.assertTrue(any(f"/team/{team_id}/" in url for team_id in b_teams), url)

    def test_the_world_pages_are_measured_logged_in(self):
        User.objects.create_user("gm", password="x")

        rows = _measure("--workspace", DEFAULT_WORKSPACE_KEY, "--as", "gm", "--world", str(self.world_id))

        self.assertEqual(len(rows), 26)
        for row in rows:
            with self.subTest(row=row):
                self.assertIn(" 200 ", row)

    def test_the_world_must_belong_to_the_workspace(self):
        other = make_pennant_world("Bの世界", workspace=self.workspace_b, seed=1, start_year=YEAR)

        with self.assertRaisesMessage(CommandError, f"世界 {other.id} は、指定したワークスペースの世界ではありません"):
            _measure("--workspace", DEFAULT_WORKSPACE_KEY, "--world", str(other.id))

    def test_an_unknown_user_is_an_error(self):
        with self.assertRaisesMessage(CommandError, "ユーザーが見つかりません: nobody"):
            _measure("--workspace", DEFAULT_WORKSPACE_KEY, "--as", "nobody")
