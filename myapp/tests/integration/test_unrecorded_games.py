"""未記録の試合（登録しただけで、打席も明細も無い試合）の扱い。

0-0 で入っているが、引分ではない。勝敗・試合数・順位・規定の基準に数えず、
画面には「未記録」と出す。スコアブックを保存すると数えられるようになる。
判定の出典は `Game.is_recorded`（ドメイン）で、参照クエリは SQL で同じ意味の条件を書くため、
両者が食い違わないこともここで突き合わせる。
"""

from datetime import date

from django.urls import reverse

from myapp.domain.pennant.world import WorldScope
from myapp.domain.value_objects import BattingLine, InningsPitched, PitchingLine
from myapp.infrastructure import orm_models
from myapp.infrastructure.queries import DjangoGameListQuery
from myapp.infrastructure.repositories import DjangoGameRepository

from ..helpers import (
    build_scorebook,
    lineup_rows,
    login_as_manager,
    play_game,
    post_game_scorebook,
    register_lineup,
)
from .base import BaseCase


class UnrecordedGameTest(BaseCase):
    def setUp(self):
        super().setUp()
        self.pitcher = self.service.register_player(self.team.id, "佐藤", 18, "投手")
        self.rival_pitcher = self.service.register_player(self.rival.id, "鈴木", 19, "投手")

    def _register(self, *, day=1):
        """登録しただけの試合。"""
        return self.service.create_game(
            year=2026, played_on=date(2026, 4, day), home_team_id=self.team.id, away_team_id=self.rival.id
        )

    def _save_scorebook(self, game, *, away_runs=3, home_runs=1):
        batters = register_lineup(self.service, self.team, prefix="打者", first_number=51)
        rivals = register_lineup(self.service, self.rival, prefix="相手", first_number=61)
        return post_game_scorebook(
            self.client,
            game.id,
            {
                "year": 2026,
                "played_on": game.played_on.isoformat(),
                "home_team": self.team.id,
                "away_team": self.rival.id,
                "lineup": lineup_rows(self.team, batters) + lineup_rows(self.rival, rivals),
                "plate_appearances": build_scorebook(
                    away=[away_runs],
                    home=[home_runs],
                    away_batters=rivals,
                    home_batters=batters,
                    away_pitchers={1: self.rival_pitcher.id},
                    home_pitchers={1: self.pitcher.id},
                ),
            },
        )

    # --- 数えない ---

    def test_an_unrecorded_game_is_not_in_the_standings(self):
        self._register()

        board = self.service.get_standings(2026)

        self.assertEqual(board.leagues, [])

    def test_an_unrecorded_game_does_not_change_the_record(self):
        play_game(self.team, self.rival, home_score=5, away_score=3, day=1)
        self._register(day=2)

        row = next(r for r in self.service.get_standings(2026).rows if r.team_name == "テストチーム")

        self.assertEqual((row.wins, row.losses, row.ties, row.games_played), (1, 0, 0, 1))

    def test_an_unrecorded_game_is_not_a_tie(self):
        play_game(self.team, self.rival, home_score=5, away_score=3, day=1)
        self._register(day=2)

        row = next(r for r in self.service.get_standings(2026).rows if r.team_name == "テストチーム")

        self.assertEqual(row.ties, 0)

    def test_an_unrecorded_game_is_not_counted_as_a_game_played(self):
        self._register()
        play_game(self.team, self.rival, day=2)

        self.assertEqual(self.service.get_team_totals(self.team.id).games, 1)
        self.assertEqual(
            DjangoGameListQuery(WorldScope.real()).count_by_team(year=2026), {self.team.id: 1, self.rival.id: 1}
        )

    def test_an_unrecorded_game_does_not_move_the_qualification_denominator(self):
        """規定打席は試合数で決まる。未記録の試合を数えると、規定に届く選手がいなくなる。"""
        batter = self.service.register_player(self.team.id, "山田", 10, "内野手")
        # 1試合で 4打席、規定打席は 3.1 × 試合数 → 1試合なら 4 打席で足りる
        play_game(self.team, self.rival, batting={batter.id: BattingLine(at_bats=4, singles=2)}, day=1)
        play_game(self.team, self.rival, recorded=False, day=2)
        self._register(day=3)

        self.assertEqual(self.service.get_team_totals(self.team.id).games, 1)
        stats = self.service.get_league_stats(self.league.id, qualified=True)
        self.assertEqual([row.player.name for row in stats.listing.rows], ["山田"])

    def test_matchups_and_monthly_splits_skip_unrecorded_games(self):
        play_game(self.team, self.rival, home_score=2, away_score=1, day=1)
        self._register(day=2)

        table = self.service.get_league_detail(self.league.id, 2026).matchups
        row = next(r for r in table.rows if r.team_id == self.team.id)
        monthly = self.service.list_team_monthly_splits(self.team.id)

        self.assertEqual(row.total_label, "1-0-0")
        self.assertEqual([m.games_played for m in monthly], [1])

    def test_a_season_with_only_unrecorded_games_is_not_listed_in_the_standings(self):
        play_game(self.team, self.rival, year=2025)
        self._register()

        self.assertEqual(self.service.get_standings().available_years, [2025])

    # --- 数えられるようになる ---

    def test_saving_a_scorebook_makes_the_game_count(self):
        game = self._register()
        login_as_manager(self.client, self.team, self.rival)

        response = self._save_scorebook(game)

        self.assertEqual(response.status_code, 200, response.content)
        row = next(r for r in self.service.get_standings(2026).rows if r.team_name == "相手チーム")
        self.assertEqual((row.wins, row.losses, row.games_played), (1, 0, 1))
        self.assertEqual(self.service.get_team_totals(self.team.id).games, 1)

    # --- 画面 ---

    def test_the_game_list_says_unrecorded_instead_of_a_tie(self):
        self._register()

        response = self.client.get(reverse("game_list"), {"year": 2026})

        self.assertContains(response, "未記録")
        self.assertNotContains(response, "引分")
        self.assertNotContains(response, "0 - 0")

    def test_a_recorded_tie_is_still_a_tie(self):
        play_game(self.team, self.rival, home_score=2, away_score=2)

        response = self.client.get(reverse("game_list"), {"year": 2026})

        self.assertContains(response, "引分")
        self.assertNotContains(response, "未記録")

    def test_the_game_detail_says_unrecorded(self):
        game = self._register()

        response = self.client.get(reverse("game_detail", args=[game.id]))

        self.assertContains(response, "未記録")
        self.assertNotContains(response, "引分")
        self.assertNotContains(response, "0 - 0")

    def test_no_winner_is_marked_on_an_unrecorded_game(self):
        self._register()

        rows = DjangoGameListQuery(WorldScope.real()).list_rows(year=2026)

        self.assertEqual([(row.is_recorded, row.winner_team_id) for row in rows], [(False, None)])

    def test_the_league_page_and_dashboard_say_unrecorded_in_recent_games(self):
        play_game(self.team, self.rival, home_score=4, away_score=1, day=1)
        self._register(day=2)

        league = self.client.get(reverse("league_detail", args=[self.league.id]))
        dashboard = self.client.get(reverse("dashboard"))

        for response in (league, dashboard):
            self.assertContains(response, "未記録")
            self.assertNotContains(response, "0 - 0")
            self.assertNotContains(response, "引分")


class RecordedConditionMatchesTheDomainTest(BaseCase):
    """SQL の条件（参照クエリ）と `Game.is_recorded`（ドメイン）が同じ答えを返す。

    出典はドメインの 1 か所で、SQL は同じ意味の写し。写しがずれると、一覧は「未記録」と出すのに
    順位表は数える、のような食い違いが起きる。
    """

    def test_every_kind_of_game_gets_the_same_answer(self):
        batter = self.service.register_player(self.team.id, "山田", 10, "内野手")
        pitcher = self.service.register_player(self.team.id, "佐藤", 18, "投手")
        cases = {
            "登録しただけ": play_game(self.team, self.rival, recorded=False, day=1),
            "得点だけ": play_game(self.team, self.rival, home_score=4, away_score=2, recorded=False, day=2),
            "打撃の明細だけ": play_game(self.team, self.rival, batting={batter.id: BattingLine(at_bats=3)}, day=3),
            "投球の明細だけ": play_game(
                self.team,
                self.rival,
                pitching={pitcher.id: PitchingLine(innings=InningsPitched.from_notation("9.0"))},
                day=4,
            ),
        }
        # 打席の記録がある試合（打席だけを足す。明細は無いまま）
        with_plate_appearance = play_game(self.team, self.rival, recorded=False, day=5)
        entry = orm_models.GamePlateAppearance.objects.create(
            game_id=with_plate_appearance.id,
            sequence=1,
            inning=1,
            is_bottom=False,
            batter_id=batter.id,
            pitcher_id=pitcher.id,
            batting_order=1,
            result="空振り三振",
        )
        orm_models.GameRunnerAdvance.objects.create(
            plate_appearance=entry, runner_id=batter.id, from_base=0, to_base=-1, reason="アウト"
        )
        cases["打席だけ"] = with_plate_appearance

        repository = DjangoGameRepository(WorldScope.real())
        query = DjangoGameListQuery(WorldScope.real())
        by_id_light = {game.id: game.is_recorded for game in query.list_for_standings()}
        by_id_row = {row.id: row.is_recorded for row in query.list_rows()}

        for label, game in cases.items():
            expected = repository.find_by_id(game.id).is_recorded
            with self.subTest(label):
                self.assertEqual(by_id_light[game.id], expected)
                self.assertEqual(by_id_row[game.id], expected)

        # 定義どおりの答えになっていること（突き合わせが全部 False で通らないように）
        self.assertEqual(
            {label: repository.find_by_id(game.id).is_recorded for label, game in cases.items()},
            {
                "登録しただけ": False,
                "得点だけ": False,
                "打撃の明細だけ": True,
                "投球の明細だけ": True,
                "打席だけ": True,
            },
        )
