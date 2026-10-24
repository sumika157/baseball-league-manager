"""アプリケーションサービスのユースケース。画面を通さず、サービスの手順を直接確認する。"""

from myapp.domain.exceptions import (
    DuplicateJerseyNumber,
)

from .base import BaseCase


class ApplicationServiceTest(BaseCase):
    def test_register_player(self):
        self.roster.register_player(self.team.id, "山田", 10, "内野手")

        rows = self.team_view.list_batters(self.team.id).rows
        self.assertEqual([r.name for r in rows], ["山田"])

    def test_register_duplicate_number_is_rejected(self):
        self.roster.register_player(self.team.id, "山田", 10, "内野手")
        with self.assertRaises(DuplicateJerseyNumber):
            self.roster.register_player(self.team.id, "田中", 10, "外野手")

    def test_retire_player_frees_the_number(self):
        player = self.roster.register_player(self.team.id, "山田", 10, "内野手")
        self.roster.retire_player(self.team.id, player.id)

        # 退団後は同じ背番号を使える
        self.roster.register_player(self.team.id, "田中", 10, "外野手")
        self.assertEqual(len(self.team_view.list_batters(self.team.id).rows), 1)

    def test_team_summary_counts_active_players(self):
        self.roster.register_player(self.team.id, "山田", 10, "内野手")
        self.roster.register_player(self.team.id, "佐藤", 18, "投手")

        summary = self.league_view.list_teams().rows[0]
        self.assertEqual(summary.league_name, "テストリーグ")
        self.assertEqual(summary.player_count, 2)
