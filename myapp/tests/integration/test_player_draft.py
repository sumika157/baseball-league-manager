"""選手詳細のドラフトの表記（#108）。表記の出典は `DraftPick.label`。"""

from myapp.infrastructure import orm_models

from ..helpers import ws_url
from .base import BaseCase


class PlayerDraftReadoutTest(BaseCase):
    def page(self, **draft):
        player = self.roster.register_player(self.team.id, "山田", 10, "内野手")
        if draft:
            orm_models.Player.objects.filter(id=player.id).update(**draft)
        return self.client.get(ws_url("player_detail", self.team.id, player.id)).content.decode()

    def test_regular_pick(self):
        body = self.page(draft_year=2020, draft_round=1)
        self.assertIn("2020年ドラフト1位", body)
        self.assertNotIn("入団", body)

    def test_developmental_pick(self):
        self.assertIn("2020年育成ドラフト3位", self.page(draft_year=2020, draft_round=3, is_development_draft=True))

    def test_pick_without_a_round(self):
        """入団年から移した選手は順位が分からない。"""
        self.assertIn("2019年ドラフト", self.page(draft_year=2019))

    def test_no_readout_without_a_draft(self):
        self.assertNotIn("ドラフト", self.page(birthplace="米国"))
