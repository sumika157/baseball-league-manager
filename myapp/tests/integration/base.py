"""結合テストの共通の土台。

リーグ・球場・チーム2つと、組み立て済みのサービスを用意する。
成績は試合の記録から集計されるため、成績を持たせたい場合は
helpers の play_game / give_batting / give_pitching で試合を作る。
"""

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
