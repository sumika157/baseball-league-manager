"""育成選手は試合に出られない（スコアブックの保存の拒否・編集画面の候補）。

その年の区分は `Stint.contract_in(年)` が出典で、昇格した年の試合なら出られる。
"""

from django.urls import reverse

from myapp.domain.value_objects import ContractStatus
from myapp.infrastructure import orm_models

from ..helpers import (
    build_roster_service,
    build_scorebook,
    lineup_rows,
    login_as_manager,
    play_game,
    post_game_scorebook,
    register_lineup,
)
from .base import BaseCase
from .test_team_analysis import AnalysisCase

DEVELOPMENTAL = ContractStatus.DEVELOPMENTAL.value


class _RecordingCase(BaseCase):
    """ホーム = self.team、ビジター = self.rival。ホームの打順1番と先発投手を差し替えられる土台。"""

    def setUp(self):
        super().setUp()
        login_as_manager(self.client, self.team, self.rival)
        self.pitcher = self.service.register_player(self.team.id, "佐藤", 18, "投手")
        self.rival_pitcher = self.service.register_player(self.rival.id, "相手投手", 19, "投手")
        self.home = register_lineup(self.service, self.team, prefix="ホーム", first_number=31)
        self.away = register_lineup(self.service, self.rival, prefix="ビジター", first_number=61)
        self.game = play_game(self.team, self.rival, home_score=0, away_score=0)

    def developmental(self, name, position, number=120, *, team=None, from_year=2020):
        """育成で登録した選手。加入年は過去にしておく（試合の年に在籍している）。"""
        team = team or self.team
        player = self.service.register_player(team.id, name, number, position, contract_label=DEVELOPMENTAL)
        orm_models.PlayerStint.objects.filter(player_id=player.id).update(from_year=from_year)
        return player

    def post(self, *, home, home_pitcher, year=2026):
        entries = build_scorebook(
            away=[0],
            home=[0],
            away_batters=self.away,
            home_batters=home,
            away_pitchers={1: self.rival_pitcher.id},
            home_pitchers={1: home_pitcher},
        )
        return post_game_scorebook(
            self.client,
            self.game.id,
            {
                "year": year,
                "played_on": f"{year}-04-01",
                "home_team": self.team.id,
                "away_team": self.rival.id,
                "lineup": lineup_rows(self.team, home) + lineup_rows(self.rival, self.away),
                "plate_appearances": entries,
            },
        )

    def assert_rejected(self, response, name):
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn(f"育成選手の「{name}」", response.json()["error"])
        # 拒否したら何も保存しない
        self.assertFalse(orm_models.GamePlateAppearance.objects.filter(game_id=self.game.id).exists())


class DevelopmentalPlayersCannotPlayTest(_RecordingCase):
    def test_a_developmental_batter_is_rejected(self):
        batter = self.developmental("育成の打者", "内野手")

        response = self.post(home=[batter.id, *self.home[1:]], home_pitcher=self.pitcher.id)

        self.assert_rejected(response, "育成の打者")

    def test_a_developmental_pitcher_is_rejected(self):
        pitcher = self.developmental("育成の投手", "投手")

        response = self.post(home=self.home, home_pitcher=pitcher.id)

        self.assert_rejected(response, "育成の投手")

    def test_a_developmental_visitor_is_rejected_too(self):
        batter = self.developmental("育成の相手", "内野手", 130, team=self.rival)
        away = [batter.id, *self.away[1:]]
        entries = build_scorebook(
            away=[0],
            home=[0],
            away_batters=away,
            home_batters=self.home,
            away_pitchers={1: self.rival_pitcher.id},
            home_pitchers={1: self.pitcher.id},
        )

        response = post_game_scorebook(
            self.client,
            self.game.id,
            {
                "year": 2026,
                "played_on": "2026-04-01",
                "home_team": self.team.id,
                "away_team": self.rival.id,
                "lineup": lineup_rows(self.team, self.home) + lineup_rows(self.rival, away),
                "plate_appearances": entries,
            },
        )

        self.assert_rejected(response, "育成の相手")

    def test_registered_players_are_saved(self):
        response = self.post(home=self.home, home_pitcher=self.pitcher.id)

        self.assertEqual(response.status_code, 200, response.content)

    def test_a_player_promoted_in_the_year_of_the_game_can_play(self):
        player = self.developmental("昇格した打者", "内野手")
        build_roster_service().promote_player(self.team.id, player.id, 5, year=2026)

        response = self.post(home=[player.id, *self.home[1:]], home_pitcher=self.pitcher.id)

        self.assertEqual(response.status_code, 200, response.content)

    def test_the_year_before_the_promotion_is_still_developmental(self):
        player = self.developmental("昇格前の打者", "内野手")
        build_roster_service().promote_player(self.team.id, player.id, 5, year=2026)

        response = self.post(home=[player.id, *self.home[1:]], home_pitcher=self.pitcher.id, year=2025)

        self.assert_rejected(response, "昇格前の打者")


class GameEditCandidatesTest(AnalysisCase):
    """編集画面の選手の候補に、その試合の年に育成の選手を出さない。"""

    def roster_names(self, game_id):
        data = self.service.get_game_edit_data(game_id)
        return {roster.team_id: [p.name for p in roster.players] for roster in data.rosters}

    def make_developmental(self, name, number=120, *, promoted_year=None, before=None):
        # 登録の API は区分と背番号の食い違いを弾くので、支配下の番号で作ってから在籍を書き換える
        player_id = self.player(self.team, name, 90 + len(name) % 9, "内野手", from_year=2020)
        orm_models.PlayerStint.objects.filter(player_id=player_id).update(
            signed_as=DEVELOPMENTAL, number=number, promoted_year=promoted_year, number_before_promotion=before
        )
        return player_id

    def test_a_developmental_player_is_not_offered(self):
        self.player(self.team, "支配下", 10, "内野手")
        self.make_developmental("育成")
        game = self.game(self.team, self.rival)

        names = self.roster_names(game.id)[self.team.id]

        self.assertIn("支配下", names)
        self.assertNotIn("育成", names)

    def test_promotion_decides_by_the_year_of_the_game(self):
        self.make_developmental("昇格済み", 30, promoted_year=2026, before=120)
        in_promotion_year = self.game(self.team, self.rival, year=2026)
        year_before = self.game(self.team, self.rival, year=2025)

        self.assertIn("昇格済み", self.roster_names(in_promotion_year.id)[self.team.id])
        self.assertNotIn("昇格済み", self.roster_names(year_before.id)[self.team.id])

    def test_a_player_already_in_the_game_stays_so_the_display_does_not_break(self):
        # 区分を持つ前に出場していた過去の試合（今は育成扱いの選手）。外すと打順の表示が壊れる
        past = self.make_developmental("過去に出場")
        game = self.game(self.team, self.rival, batting=[(past, self.team, "遊", 0)])

        data = self.service.get_game_edit_data(game.id)

        home = next(r for r in data.rosters if r.team_id == self.team.id)
        self.assertEqual([p.name for p in home.players], ["過去に出場"])
        self.assertEqual([slot.player_id for slot in home.lineup], [past])

    def test_the_edit_screen_does_not_list_the_developmental_player(self):
        login_as_manager(self.client, self.team)
        self.make_developmental("画面に出ない育成")
        game = self.game(self.team, self.rival)

        response = self.client.get(reverse("game_edit", args=[game.id]))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "画面に出ない育成")
