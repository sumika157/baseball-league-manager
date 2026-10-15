"""結合テストの共通の土台。

リーグ・球場・チーム2つと、組み立て済みのサービスを用意する。
成績は試合の記録から集計されるため、成績を持たせたい場合は
helpers の play_game / give_batting / give_pitching で試合を作る。

リーグ・球場・チームはクラスで1回だけ作る（`setUpTestData`）。テストの中で書き換えても、`TestCase` が
テストごとにトランザクションで巻き戻し、属性（`self.team` など）もテストごとに写しが渡る。
"""

from unittest.mock import patch

from django.test import TestCase

from myapp.infrastructure import orm_models

from ..helpers import build_service, default_workspace, make_league, make_stadium

# 世界の作成の名簿の検査（`skip_roster_check` が外すもの）
ROSTER_CHECK = "myapp.application.pennant_world.ensure_roster_playable"


class BaseCase(TestCase):
    workspace: orm_models.Workspace
    league: orm_models.League
    stadium: orm_models.Stadium
    team: orm_models.Team
    rival: orm_models.Team

    @classmethod
    def setUpTestData(cls):
        cls.workspace = default_workspace()
        cls.league = make_league("テストリーグ", workspace=cls.workspace)
        cls.stadium = make_stadium("テスト球場", workspace=cls.workspace, city="東京")
        cls.team = orm_models.Team.objects.create(league=cls.league, name="テストチーム", home_stadium=cls.stadium)
        cls.rival = orm_models.Team.objects.create(league=cls.league, name="相手チーム")

    def setUp(self):
        self.service = build_service()

    def skip_roster_check(self) -> None:
        """世界の作成の名簿の検査を外す。

        世界の作成は、試合を組めない名簿の球団を弾く（`test_world_roster_check.py`）。分岐の中身や世界の画面を
        小さな名簿で確かめるテストは、選手を足さずに済むよう、検査だけを外す（足すと選手の数を数える
        アサーションがずれる）。
        """
        patcher = patch(ROSTER_CHECK)
        patcher.start()
        self.addCleanup(patcher.stop)
