"""指標。リーグ平均を基準にする相対指標（OPS+・ERA+）と発展的な指標（FIP など）。"""

from django.urls import reverse

from myapp.domain.value_objects import (
    BattingLine,
    InningsPitched,
    PitchingLine,
)
from myapp.infrastructure import orm_models

from ..helpers import (
    build_scorebook,
    give_batting,
    give_pitching,
    lineup_rows,
    login_as_manager,
    play_game,
    post_game_scorebook,
    register_lineup,
)
from .base import BaseCase


class RelativeMetricsTest(BaseCase):
    """対リーグ相対指標（フェーズ4）。OPS+ と ERA+。

    どちらもリーグ平均を100とした指数。リーグ全体の合計から基準を作るため、
    リーグを知らないアプリ層では確定できない。
    """

    def setUp(self):
        super().setUp()
        self.batter = self.service.register_player(self.team.id, "山田", 10, "内野手")
        self.pitcher = self.service.register_player(self.team.id, "佐藤", 18, "投手")
        give_batting(
            self.team,
            self.rival,
            self.batter.id,
            BattingLine(at_bats=10, singles=2, home_runs=1),
            day=1,
        )
        give_pitching(
            self.team,
            self.rival,
            self.pitcher.id,
            PitchingLine(innings=InningsPitched.from_notation("9.0"), earned_runs=3),
            day=2,
        )

    def test_a_lone_player_sits_at_the_league_average(self):
        """リーグにその選手しかいなければ、本人がリーグ平均そのもの＝100。"""
        batter = self.service.get_player_detail(self.team.id, self.batter.id)
        pitcher = self.service.get_player_detail(self.team.id, self.pitcher.id)

        self.assertAlmostEqual(batter.ops_plus, 100.0)
        self.assertAlmostEqual(pitcher.era_plus, 100.0)

    def test_a_better_batter_scores_above_a_hundred(self):
        """リーグ平均より良い打者は100を超える。"""
        weak = self.service.register_player(self.team.id, "田中", 11, "外野手")
        give_batting(self.team, self.rival, weak.id, BattingLine(at_bats=10, singles=1), day=3)

        rows = {r.name: r for r in self.service.list_batters(self.team.id).rows}

        self.assertGreater(rows["山田"].ops_plus, 100.0)
        self.assertLess(rows["田中"].ops_plus, 100.0)

    def test_era_plus_rewards_the_lower_earned_run_average(self):
        """ERA+ は防御率が低いほど大きい（FIP と向きが逆）。"""
        weak = self.service.register_player(self.team.id, "鈴木", 19, "投手")
        give_pitching(
            self.team,
            self.rival,
            weak.id,
            PitchingLine(innings=InningsPitched.from_notation("9.0"), earned_runs=9),
            day=4,
        )

        rows = {r.name: r for r in self.service.list_pitchers(self.team.id).rows}

        self.assertGreater(rows["佐藤"].era_plus, rows["鈴木"].era_plus)
        self.assertGreater(rows["佐藤"].era_plus, 100.0)

    def test_players_in_another_league_do_not_move_the_baseline(self):
        """指数はリーグごとに閉じる。他リーグの成績は基準に混ぜない。"""
        other_league = orm_models.League.objects.create(name="別リーグ")
        other = orm_models.Team.objects.create(league=other_league, name="別チーム")
        opponent = orm_models.Team.objects.create(league=other_league, name="別の相手")
        slugger = self.service.register_player(other.id, "大砲", 3, "内野手")
        give_batting(
            other,
            opponent,
            slugger.id,
            BattingLine(at_bats=10, home_runs=5),
            day=5,
        )

        # 別リーグに強打者が現れても、こちらのリーグの基準は動かない
        detail = self.service.get_player_detail(self.team.id, self.batter.id)

        self.assertAlmostEqual(detail.ops_plus, 100.0)

    def test_lists_and_pages_show_the_indexes(self):
        batters = self.client.get(reverse("player_list", args=[self.team.id]))
        pitchers = self.client.get(f"{reverse('player_list', args=[self.team.id])}?pos=pitcher")
        page = self.client.get(reverse("player_detail", args=[self.team.id, self.batter.id]))

        self.assertContains(batters, "OPS+")
        self.assertContains(pitchers, "ERA+")
        self.assertContains(page, "OPS+")


class AdvancedMetricsTest(BaseCase):
    """指標の拡充（フェーズ4）。FIP と IsoP。"""

    def setUp(self):
        super().setUp()
        self.pitcher = self.service.register_player(self.team.id, "佐藤", 18, "投手")
        self.batter = self.service.register_player(self.team.id, "山田", 10, "内野手")
        give_pitching(
            self.team,
            self.rival,
            self.pitcher.id,
            PitchingLine(
                innings=InningsPitched.from_notation("9.0"),
                earned_runs=3,
                hits_allowed=6,
                home_runs_allowed=1,
                walks_allowed=2,
                hit_by_pitch_allowed=1,
                strikeouts=9,
            ),
            day=1,
        )
        give_batting(
            self.team,
            self.rival,
            self.batter.id,
            BattingLine(at_bats=10, singles=1, home_runs=2),
            day=2,
        )

    def test_new_counts_survive_the_round_trip(self):
        detail = self.service.get_player_detail(self.team.id, self.pitcher.id)

        self.assertEqual(detail.home_runs_allowed, 1)
        self.assertEqual(detail.hit_by_pitch_allowed, 1)

    def test_fip_uses_the_league_constant(self):
        """リーグに1人しかいなければ、その投手の FIP は防御率と一致する。

        定数はリーグ全体の防御率と素点の差なので、本人＝リーグ全体のとき
        素点との差がそのまま埋まる。
        """
        detail = self.service.get_player_detail(self.team.id, self.pitcher.id)

        self.assertAlmostEqual(detail.fip, detail.earned_run_average)

    def test_isolated_power(self):
        detail = self.service.get_player_detail(self.team.id, self.batter.id)

        self.assertAlmostEqual(detail.isolated_power, detail.slugging_percentage - detail.batting_average)

    def test_pitcher_list_shows_fip(self):
        response = self.client.get(f"{reverse('player_list', args=[self.team.id])}?pos=pitcher")

        self.assertContains(response, "FIP")
        self.assertContains(response, "BB/9")
        self.assertAlmostEqual(response.context["players"][0].fip, 3.0)

    def test_batter_list_shows_iso(self):
        response = self.client.get(reverse("player_list", args=[self.team.id]))

        self.assertContains(response, "IsoP")
        self.assertAlmostEqual(response.context["players"][0].isolated_power, 0.6)

    def test_pitchers_can_be_sorted_by_fip(self):
        weak = self.service.register_player(self.team.id, "田中", 19, "投手")
        give_pitching(
            self.team,
            self.rival,
            weak.id,
            PitchingLine(
                innings=InningsPitched.from_notation("9.0"),
                earned_runs=9,
                hits_allowed=12,
                home_runs_allowed=4,
                walks_allowed=5,
            ),
            day=3,
        )

        listing = self.service.list_pitchers(self.team.id, sort="fip")

        self.assertEqual([r.name for r in listing.rows], ["佐藤", "田中"])

    def test_team_totals_include_fip(self):
        totals = self.service.get_team_totals(self.team.id)

        self.assertAlmostEqual(totals.fip, totals.earned_run_average)

    def test_player_page_shows_fip(self):
        response = self.client.get(reverse("player_detail", args=[self.team.id, self.pitcher.id]))

        self.assertContains(response, "FIP")

    def test_new_counts_are_derived_from_the_scorebook(self):
        """被本塁打・与死球は入力せず、打席の結果から数えること。

        **被本塁打が被安打を超える入力は、もう起こりえない**（本塁打は安打の一種
        として1打席から数えるため）。組み合わせとして成立しないことの検査は
        値オブジェクト側（tests/domain/test_value_objects.py）に残っている。
        """
        login_as_manager(self.client, self.team, username="scorer")
        game = play_game(self.team, self.rival, home_score=0, away_score=0)
        batters = register_lineup(self.service, self.rival, prefix="相手打者", first_number=71)
        home_batters = register_lineup(self.service, self.team, prefix="自軍打者", first_number=81)

        post_game_scorebook(
            self.client,
            game.id,
            {
                "year": 2026,
                "played_on": "2026-05-01",
                "home_team": self.team.id,
                "away_team": self.rival.id,
                "lineup": lineup_rows(self.rival, batters) + lineup_rows(self.team, home_batters),
                # ビジターが1回表に2点（本塁打2本）。投げているのは自軍の投手
                "plate_appearances": build_scorebook(
                    away=[2],
                    home=[0],
                    away_batters=batters,
                    home_batters=home_batters,
                    away_pitchers={1: self.pitcher.id},
                    home_pitchers={1: self.pitcher.id},
                ),
            },
        )

        line = orm_models.GamePitchingLine.objects.get(game_id=game.id, player_id=self.pitcher.id)
        self.assertEqual(line.home_runs_allowed, 2)
        self.assertEqual(line.hits_allowed, 2)
        self.assertEqual(line.strikeouts, 6)
