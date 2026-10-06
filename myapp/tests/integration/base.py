"""結合テストの共通の土台。

リーグ・球場・チーム2つと、組み立て済みのサービスを用意する。
成績は試合の記録から集計されるため、成績を持たせたい場合は
helpers の play_game / give_batting / give_pitching で試合を作る。
"""

from unittest.mock import patch

from django.test import TestCase

from myapp.infrastructure import orm_models

from ..helpers import (
    build_service,
)


class BaseCase(TestCase):
    def setUp(self):
        self.league = orm_models.League.objects.create(name="テストリーグ")
        self.stadium = orm_models.Stadium.objects.create(name="テスト球場", city="東京")
        self.team = orm_models.Team.objects.create(league=self.league, name="テストチーム", home_stadium=self.stadium)
        self.rival = orm_models.Team.objects.create(league=self.league, name="相手チーム")
        self.service = build_service()

    def skip_roster_check(self) -> None:
        """世界の作成の名簿の検査を外す。

        世界の作成は、試合を組めない名簿の球団を弾く（`test_world_roster_check.py`）。分岐の中身や世界の画面を
        小さな名簿で確かめるテストは、選手を足さずに済むよう、検査だけを外す（足すと選手の数を数える
        アサーションがずれる）。
        """
        patcher = patch("myapp.application.pennant_world.ensure_roster_playable")
        patcher.start()
        self.addCleanup(patcher.stop)
