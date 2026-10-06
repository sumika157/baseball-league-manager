"""戦力分析ページ（球団×年度のデプス表・年齢構成）。"""

import pathlib
import re
from datetime import date

from django.conf import settings
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from myapp.application.team_analysis import USAGE_AREAS
from myapp.domain.entities import Game
from myapp.domain.services import NEUTRAL_CATEGORY, ColorAxis, color_categories, usage_map_positions
from myapp.domain.services.roster_analysis import MAX_PLAYERS_IN_BOX
from myapp.domain.value_objects import BattingLine, FieldingPosition, PitchingLine, Season
from myapp.infrastructure import orm_models
from myapp.infrastructure.repositories import DjangoGameRepository

from ..helpers import build_team_analysis_service
from .base import BaseCase


class AnalysisCase(BaseCase):
    """選手の登録と、守備位置つきの試合を作る下ごしらえ。"""

    def player(
        self, team, name, number, position="内野手", *, throws="", bats="", birth=None, from_year=2020, to_year=None
    ):
        created = self.service.register_player(team.id, name, number, position)
        orm_models.Player.objects.filter(id=created.id).update(throws=throws, bats=bats, birth_date=birth)
        orm_models.PlayerStint.objects.filter(player_id=created.id).update(from_year=from_year, to_year=to_year)
        return created.id

    def game(self, home, away, *, year=2026, day=1, batting=(), pitching=()):
        """batting は (選手id, チーム, 守備位置の記号, 交代の順)、pitching は (選手id, 登板順)。"""
        game = Game(
            season=Season(year),
            played_on=date(year, 4, day),
            home_team_id=home.id,
            away_team_id=away.id,
            home_score=1,
            away_score=0,
        )
        for player_id, team, label, slot in batting:
            game.record_batting(
                player_id,
                BattingLine(),
                team_id=team.id,
                batting_order=1,
                slot_sequence=slot,
                fielding_position=FieldingPosition.from_label(label),
            )
        for player_id, order in pitching:
            game.record_pitching(player_id, PitchingLine(), appearance_order=order)
        return DjangoGameRepository().save(game)

    @staticmethod
    def names(table):
        return [p.name for row in table.rows for cell in row.cells for p in cell.players]

    @staticmethod
    def find(table, name):
        return next(p for row in table.rows for cell in row.cells for p in cell.players if p.name == name)

    @staticmethod
    def cells(table, label):
        """表の1行に並ぶ選手を {列の見出し: [名前]} で返す。"""
        row = next(r for r in table.rows if r.label == label)
        return {cell.hand_label: [p.name for p in cell.players] for cell in row.cells}


class TeamAnalysisPageTest(AnalysisCase):
    def setUp(self):
        super().setUp()
        self.ace = self.player(self.team, "左腕エース", 18, "投手", throws="左", birth=date(1998, 4, 1))
        self.setup_man = self.player(self.team, "右の中継ぎ", 20, "投手", throws="右")
        self.shortstop = self.player(self.team, "遊撃手", 6, "内野手", bats="右", birth=date(2000, 4, 2))
        self.rival_pitcher = self.player(self.rival, "相手投手", 11, "投手", throws="左")
        self.game(
            self.team,
            self.rival,
            batting=[(self.shortstop, self.team, "遊", 0)],
            pitching=[(self.ace, 1), (self.setup_man, 2), (self.rival_pitcher, 1)],
        )

    def analysis(self, **kwargs):
        return build_team_analysis_service().get_analysis(self.team.id, **kwargs)

    def url(self, team=None):
        return reverse("team_analysis", args=[(team or self.team).id])

    def test_anonymous_can_view_without_any_write_controls(self):
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'method="post"')
        self.assertNotContains(response, "編集")
        self.assertContains(response, "左腕エース")

    def test_post_is_not_allowed(self):
        self.assertEqual(self.client.post(self.url()).status_code, 405)

    def test_unknown_team_is_404(self):
        self.assertEqual(self.client.get(reverse("team_analysis", args=[99999])).status_code, 404)

    def test_left_handed_starter_lands_in_starter_by_left_cell(self):
        analysis = self.analysis(year=2026)
        self.assertEqual(self.cells(analysis.pitchers, "先発")["左腕"], ["左腕エース"])
        self.assertEqual(self.cells(analysis.pitchers, "救援")["右腕"], ["右の中継ぎ"])

    def test_opposing_pitcher_in_the_same_game_is_not_counted(self):
        analysis = self.analysis(year=2026)
        self.assertNotIn("相手投手", self.names(analysis.pitchers))
        self.assertEqual(analysis.pitchers.count, 2)

    def test_fielder_is_grouped_by_primary_position(self):
        analysis = self.analysis(year=2026)
        self.assertEqual(self.cells(analysis.fielders, "二遊間")["右打"], ["遊撃手"])
        player = self.find(analysis.fielders, "遊撃手")
        self.assertEqual((player.position_label, player.games, player.starts), ("遊", 1, 1))

    def test_other_years_games_and_players_outside_the_year_are_not_counted(self):
        # 2025年に先発した登板は、2026年の表に数えない
        self.game(self.team, self.rival, year=2025, pitching=[(self.setup_man, 1)])
        future = self.player(self.team, "来年入団", 30, "投手", throws="右", from_year=2027)
        retired = self.player(self.team, "引退済み", 31, "投手", throws="右", to_year=2024)
        self.game(self.team, self.rival, day=2, pitching=[(future, 1), (retired, 1)])

        analysis = self.analysis(year=2026)
        names = self.names(analysis.pitchers)
        self.assertNotIn("来年入団", names)
        self.assertNotIn("引退済み", names)
        reliever = self.find(analysis.pitchers, "右の中継ぎ")
        self.assertEqual((reliever.games, reliever.starts), (1, 0))

    def test_invalid_year_falls_back_to_latest(self):
        self.game(self.team, self.rival, year=2025, pitching=[(self.ace, 1)])
        for year in ("1999", "abc", "-1", ""):
            with self.subTest(year=year):
                response = self.client.get(self.url(), {"year": year})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["analysis"].year, 2026)
        self.assertEqual(self.client.get(self.url(), {"year": "2025"}).context["analysis"].year, 2025)

    def test_invalid_tab_falls_back_to_depth(self):
        response = self.client.get(self.url(), {"tab": "unknown"})
        self.assertEqual(response.context["analysis"].tab, "depth")

    def test_team_without_games_still_renders(self):
        lonely = orm_models.Team.objects.create(league=self.league, name="試合なしチーム")
        self.player(lonely, "孤独な投手", 1, "投手", throws="右", from_year=date.today().year)
        response = self.client.get(self.url(lonely))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["analysis"].year, date.today().year)
        self.assertContains(response, "孤独な投手")

    def test_age_is_counted_on_april_first(self):
        analysis = self.analysis(year=2026)
        ace = self.find(analysis.pitchers, "左腕エース")
        shortstop = self.find(analysis.fielders, "遊撃手")
        self.assertEqual((ace.age, shortstop.age), (28, 25))
        bands = {row.band: row for row in analysis.age_rows}
        self.assertEqual(bands["28歳"].pitchers, 1)
        self.assertEqual(bands["25歳"].fielders, 1)
        self.assertEqual(bands["不明"].pitchers, 1)

    def test_query_count_does_not_grow_with_roster(self):
        def count():
            with CaptureQueriesContext(connection) as captured:
                self.analysis(year=2026)
            return len(captured)

        before = count()
        for number in range(40, 70):
            player_id = self.player(
                self.team, f"控え{number}", number, "投手" if number % 2 else "外野手", throws="右", bats="左"
            )
            self.game(self.team, self.rival, day=number % 28 + 1, pitching=[(player_id, 2)])
        self.assertEqual(count(), before)
        self.assertLessEqual(before, 6)


class AnalysisIndexTest(AnalysisCase):
    def test_redirects_to_first_team(self):
        first = build_team_analysis_service().first_team_id()
        response = self.client.get(reverse("analysis_index"))
        self.assertRedirects(response, reverse("team_analysis", args=[first]), fetch_redirect_response=False)

    def test_team_and_year_choice_is_carried_over(self):
        response = self.client.get(reverse("analysis_index"), {"team": self.rival.id, "year": "2025"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"{reverse('team_analysis', args=[self.rival.id])}?year=2025")

    def test_unknown_team_choice_falls_back_to_first_team(self):
        first = build_team_analysis_service().first_team_id()
        response = self.client.get(reverse("analysis_index"), {"team": "99999"})
        self.assertEqual(response["Location"], reverse("team_analysis", args=[first]))

    def test_no_teams_shows_a_notice(self):
        orm_models.Team.objects.all().delete()
        response = self.client.get(reverse("analysis_index"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "チームがまだ登録されていない")


class AnalysisLinksTest(TestCase):
    def setUp(self):
        league = orm_models.League.objects.create(name="リーグ")
        self.team = orm_models.Team.objects.create(league=league, name="チーム")

    def test_header_links_to_analysis_index(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, f'href="{reverse("analysis_index")}"')

    def test_team_page_links_to_its_analysis(self):
        response = self.client.get(reverse("player_list", args=[self.team.id]))
        self.assertContains(response, f'href="{reverse("team_analysis", args=[self.team.id])}"')


class TeamUsageMapTest(AnalysisCase):
    def setUp(self):
        super().setUp()
        self.regular = self.player(self.team, "正遊撃手", 6, "内野手", bats="右")
        self.backup = self.player(self.team, "控え遊撃手", 7, "内野手", bats="右")
        self.dh = self.player(self.team, "指名打者", 9, "外野手", bats="左")
        self.ace = self.player(self.team, "エース", 18, "投手", throws="右")
        self.long_man = self.player(self.team, "中継ぎ", 20, "投手", throws="右")
        self.rival_ss = self.player(self.rival, "相手遊撃手", 6, "内野手")
        for day in (1, 2):
            self.game(
                self.team,
                self.rival,
                day=day,
                batting=[
                    (self.regular, self.team, "遊", 0),
                    (self.dh, self.team, "指", 0),
                    (self.rival_ss, self.rival, "遊", 0),
                ],
                pitching=[(self.ace, 1), (self.long_man, 2)],
            )
        # 3試合目は控えが遊撃に途中から入る
        self.game(
            self.team,
            self.rival,
            day=3,
            batting=[(self.regular, self.team, "打", 0), (self.backup, self.team, "遊", 1)],
            pitching=[(self.ace, 1)],
        )

    def boxes(self, **kwargs):
        analysis = build_team_analysis_service().get_analysis(self.team.id, year=2026, tab="usage", **kwargs)
        return {box.label: box for box in analysis.usage_boxes}

    def triples(self, box):
        return [(p.name, p.starts, p.games) for p in box.players]

    def test_tab_is_shown_and_listed(self):
        response = self.client.get(reverse("team_analysis", args=[self.team.id]), {"tab": "usage"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["analysis"].tab, "usage")
        self.assertEqual([t.key for t in response.context["analysis"].tabs], ["depth", "usage"])
        self.assertContains(response, "正遊撃手")
        self.assertContains(response, "usage-box-short")

    def test_boxes_follow_the_domain_positions(self):
        analysis = build_team_analysis_service().get_analysis(self.team.id, year=2026, tab="usage")
        self.assertEqual(
            [box.label for box in analysis.usage_boxes],
            [position.label for position in usage_map_positions()],
        )
        self.assertEqual(set(USAGE_AREAS), set(usage_map_positions()))

    def test_starts_and_substitute_appearances_are_told_apart(self):
        # 正遊撃手は遊で2先発（3試合目は代打で先発）。控えは遊で途中出場だけ
        self.assertEqual(self.triples(self.boxes()["遊"]), [("正遊撃手", 2, 2), ("控え遊撃手", 0, 1)])

    def test_designated_hitter_has_its_own_box(self):
        self.assertEqual(self.triples(self.boxes()["指"]), [("指名打者", 2, 2)])

    def test_pitcher_box_counts_starting_appearances_from_pitching_lines(self):
        box = self.boxes()["投"]
        self.assertEqual(self.triples(box), [("エース", 3, 3), ("中継ぎ", 0, 2)])

    def test_batting_line_with_pitcher_position_is_not_double_counted(self):
        self.game(self.team, self.rival, day=4, batting=[(self.ace, self.team, "投", 0)], pitching=[(self.ace, 1)])
        self.assertEqual(self.triples(self.boxes()["投"])[0], ("エース", 4, 4))

    def test_opposing_players_are_not_counted(self):
        names = [p.name for box in self.boxes().values() for p in box.players]
        self.assertNotIn("相手遊撃手", names)

    def test_other_years_are_not_counted(self):
        self.game(self.team, self.rival, year=2025, batting=[(self.regular, self.team, "遊", 0)])
        self.assertEqual(self.triples(self.boxes()["遊"])[0], ("正遊撃手", 2, 2))

    def test_unused_position_is_an_empty_box(self):
        box = self.boxes()["捕"]
        self.assertEqual((box.players, box.hidden_count), ([], 0))

    def test_players_beyond_the_cap_are_hidden(self):
        extras = MAX_PLAYERS_IN_BOX + 2
        for number in range(40, 40 + extras):
            extra = self.player(self.team, f"控え{number}", number, "内野手")
            self.game(self.team, self.rival, day=number - 30, batting=[(extra, self.team, "二", 0)])
        box = self.boxes()["二"]
        total = len(box.players) + box.hidden_count
        self.assertGreaterEqual(total, extras)
        self.assertEqual(len(box.players), MAX_PLAYERS_IN_BOX)
        self.assertEqual(box.hidden_count, total - MAX_PLAYERS_IN_BOX)
        # 先発の多い順に並べてから切るので、残るのは先発数の上位
        self.assertEqual([p.starts for p in box.players], sorted((p.starts for p in box.players), reverse=True))

    def test_query_count_does_not_grow_with_roster(self):
        def count():
            with CaptureQueriesContext(connection) as captured:
                build_team_analysis_service().get_analysis(self.team.id, year=2026, tab="usage")
            return len(captured)

        before = count()
        for number in range(50, 80):
            player_id = self.player(self.team, f"控え{number}", number, "内野手")
            self.game(self.team, self.rival, day=number % 28 + 1, batting=[(player_id, self.team, "三", 0)])
        self.assertEqual(count(), before)


class TeamMovesTest(AnalysisCase):
    """入退団（在籍の加入年・退団年から導く）。"""

    def setUp(self):
        super().setUp()
        # 年を選べるよう、両年に記録済みの試合（投球の明細つき）を作っておく
        starter = self.player(self.team, "先発", 1, "投手", throws="右")
        for year in (2025, 2026):
            self.game(self.team, self.rival, year=year, pitching=[(starter, 1)])

    @staticmethod
    def stint(team, player_id, number, from_year, to_year=None):
        orm_models.PlayerStint.objects.create(
            player_id=player_id, team=team, number=number, from_year=from_year, to_year=to_year
        )

    def move(self, team, name, number, from_year, to_year=None, position="内野手"):
        return self.player(team, name, number, position, from_year=from_year, to_year=to_year)

    def analysis(self, team=None, year=2026):
        return build_team_analysis_service().get_analysis((team or self.team).id, year=year)

    @staticmethod
    def summary(rows):
        return [(r.name, r.kind_label, r.other_team_name) for r in rows]

    def test_new_signing_and_departure_are_listed(self):
        self.move(self.team, "新人", 50, 2026)
        self.move(self.team, "去る人", 51, 2020, 2026)
        self.move(self.team, "残る人", 52, 2020)
        analysis = self.analysis()
        self.assertEqual(self.summary(analysis.joiners), [("新人", "新入団", "")])
        self.assertEqual(self.summary(analysis.leavers), [("去る人", "退団", "")])

    def test_transfer_shows_the_previous_team_and_the_destination(self):
        mover = self.move(self.rival, "移籍選手", 7, 2020, 2025)
        self.stint(self.team, mover, 8, 2026)
        self.assertEqual(self.summary(self.analysis().joiners), [("移籍選手", "移籍", self.rival.name)])
        self.assertEqual(
            self.summary(self.analysis(self.rival, year=2025).leavers), [("移籍選手", "移籍", self.team.name)]
        )

    def test_mid_season_move_appears_on_both_teams(self):
        mover = self.move(self.rival, "シーズン途中", 7, 2020, 2026)
        self.stint(self.team, mover, 8, 2026)
        self.assertEqual(self.summary(self.analysis().joiners), [("シーズン途中", "移籍", self.rival.name)])
        self.assertEqual(self.summary(self.analysis(self.rival).leavers), [("シーズン途中", "移籍", self.team.name)])

    def test_rejoining_the_same_team_is_a_rejoin(self):
        returner = self.move(self.team, "出戻り", 9, 2026)
        self.stint(self.team, returner, 9, 2018, 2020)
        self.assertEqual(self.summary(self.analysis().joiners), [("出戻り", "再入団", "")])

    def test_other_years_and_other_teams_are_not_listed(self):
        self.move(self.team, "去年の加入", 50, 2025)
        self.move(self.team, "去年の退団", 51, 2020, 2025)
        self.move(self.team, "在籍中", 52, 2020)
        self.move(self.rival, "相手の加入", 60, 2026)
        self.move(self.rival, "相手の退団", 61, 2020, 2026)
        analysis = self.analysis()
        self.assertEqual((analysis.joiners, analysis.leavers), ([], []))

    def test_rows_are_ordered_by_kind_then_number_and_carry_position(self):
        mover = self.move(self.rival, "移籍組", 1, 2020, 2025)
        self.stint(self.team, mover, 3, 2026)
        self.move(self.team, "新人B", 40, 2026, position="投手")
        self.move(self.team, "新人A", 20, 2026)
        joiners = self.analysis().joiners
        self.assertEqual([r.name for r in joiners], ["新人A", "新人B", "移籍組"])
        self.assertEqual([r.position_label for r in joiners], ["内野手", "投手", "内野手"])

    def test_page_shows_both_tables_with_player_links_and_empty_messages(self):
        new = self.move(self.team, "新人", 50, 2026)
        url = reverse("team_analysis", args=[self.team.id])
        response = self.client.get(url, {"year": 2026})
        self.assertContains(response, "新入団")
        self.assertContains(response, reverse("player_detail", args=[self.team.id, new]))
        self.assertContains(response, "この年の退団はありません。")
        self.assertNotContains(response, "この年の加入はありません。")
        self.assertContains(self.client.get(url, {"year": 2025}), "この年の加入はありません。")

    def test_query_count_does_not_grow_with_moves(self):
        def count():
            with CaptureQueriesContext(connection) as captured:
                self.analysis()
            return len(captured)

        self.move(self.team, "最初の新人", 50, 2026)
        before = count()
        for number in range(60, 80):
            player_id = self.move(self.rival, f"移籍{number}", number, 2020, 2025)
            self.stint(self.team, player_id, number, 2026)
            # 支配下の背番号は99以下（#126）。移籍組の 60〜79 と重ならない 80〜99 を使う
            self.move(self.team, f"退団{number}", number + 20, 2020, 2026)
        self.assertEqual(count(), before)


class TeamColorTest(AnalysisCase):
    """色分け（`?color=`）。デプス表・起用マップ・入退団で同じ規則・同じ色になる。"""

    def setUp(self):
        super().setUp()
        self.ace = self.player(self.team, "左腕エース", 18, "投手", throws="左", bats="右")
        self.setup_man = self.player(self.team, "右の中継ぎ", 20, "投手", throws="右", bats="右")
        self.shortstop = self.player(self.team, "本職遊撃手", 6, "内野手", throws="右", bats="左")
        self.misfit = self.player(self.team, "外野を守る内野手", 8, "内野手", throws="右", bats="右")
        self.bench = self.player(self.team, "守備なし", 99, "内野手", bats="両")
        self.rookie = self.player(self.team, "新人投手", 30, "投手", throws="左", from_year=2026)
        self.game(
            self.team,
            self.rival,
            batting=[(self.shortstop, self.team, "遊", 0), (self.misfit, self.team, "左", 0)],
            pitching=[(self.ace, 1), (self.setup_man, 2)],
        )

    def analysis(self, color=None, tab=None):
        return build_team_analysis_service().get_analysis(self.team.id, year=2026, tab=tab, color=color)

    def usage_tones(self, analysis):
        return {p.name: p.tone.key for box in analysis.usage_boxes for p in box.players}

    def depth_tones(self, analysis):
        tables = (analysis.pitchers, analysis.fielders)
        return {p.name: p.tone.key for t in tables for r in t.rows for c in r.cells for p in c.players}

    def legend(self, analysis):
        return {item.category.label: item.count for item in analysis.legend}

    def test_default_is_hand_and_invalid_values_fall_back(self):
        for color in (None, "", "bogus", "foreign"):
            with self.subTest(color=color):
                analysis = self.analysis(color=color)
                self.assertEqual(analysis.color, "hand")
                self.assertEqual([o.key for o in analysis.color_options], ["hand", "natural"])

    def test_hand_axis_in_usage_map_uses_throwing_arm_in_pitcher_box_and_batting_side_otherwise(self):
        tones = self.usage_tones(self.analysis(tab="usage"))
        self.assertEqual(tones["左腕エース"], "hand-left")
        self.assertEqual(tones["右の中継ぎ"], "hand-right")
        self.assertEqual(tones["本職遊撃手"], "hand-left")
        self.assertEqual(tones["外野を守る内野手"], "hand-right")

    def test_natural_axis_in_usage_map(self):
        analysis = self.analysis(color="natural", tab="usage")
        tones = self.usage_tones(analysis)
        self.assertEqual(tones["左腕エース"], "natural-yes")
        self.assertEqual(tones["本職遊撃手"], "natural-yes")
        self.assertEqual(tones["外野を守る内野手"], "natural-no")
        self.assertEqual(self.legend(analysis), {"本職": 3, "本職外": 1})

    def test_hand_legend_lists_every_category_with_counts(self):
        analysis = self.analysis(tab="usage")
        self.assertEqual(self.legend(analysis), {"左": 2, "両": 0, "右": 2, "不明": 0})
        self.assertEqual([i.category.mark for i in analysis.legend], ["左", "両", "右", "？"])

    def test_same_player_gets_the_same_tone_in_depth_table_and_usage_map(self):
        for color in ("hand", "natural"):
            with self.subTest(color=color):
                depth = self.depth_tones(self.analysis(color=color))
                usage = self.usage_tones(self.analysis(color=color, tab="usage"))
                for name, key in usage.items():
                    self.assertEqual(depth[name], key, name)

    def test_depth_table_natural_axis_is_neutral_without_a_main_position(self):
        tones = self.depth_tones(self.analysis(color="natural"))
        # 守備出場なしの野手・登板なしの投手は主な守備位置が無い
        self.assertEqual(tones["守備なし"], "neutral")
        self.assertEqual(tones["新人投手"], "neutral")
        self.assertEqual(tones["左腕エース"], "natural-yes")
        self.assertEqual(tones["外野を守る内野手"], "natural-no")

    def test_depth_table_hand_axis_follows_the_table(self):
        tones = self.depth_tones(self.analysis())
        self.assertEqual(tones["左腕エース"], "hand-left")  # 投手の表は投げる手
        self.assertEqual(tones["守備なし"], "hand-both")  # 野手の表は打席

    def test_moves_are_colored_by_hand_but_neutral_for_natural(self):
        joiners = {row.name: row.tone.key for row in self.analysis().joiners}
        self.assertEqual(joiners["新人投手"], "hand-left")
        natural = {row.name: row.tone.key for row in self.analysis(color="natural").joiners}
        self.assertEqual(set(natural.values()), {"neutral"})

    def test_page_carries_the_axis_to_year_tab_team_links(self):
        response = self.client.get(reverse("team_analysis", args=[self.team.id]), {"year": 2026, "color": "natural"})
        self.assertContains(response, 'name="color" value="natural"')
        self.assertContains(response, "tab=usage&amp;color=natural")
        self.assertContains(response, "year=2026&amp;tab=depth&amp;color=natural")
        self.assertContains(response, "player-tone-natural-yes")

    def test_invalid_color_page_uses_hand_in_links(self):
        response = self.client.get(reverse("team_analysis", args=[self.team.id]), {"color": "x"})
        self.assertContains(response, 'name="color" value="hand"')

    def test_analysis_index_redirect_keeps_color(self):
        response = self.client.get(reverse("analysis_index"), {"team": self.team.id, "color": "natural"})
        self.assertEqual(response["Location"], f"{reverse('team_analysis', args=[self.team.id])}?color=natural")

    def test_query_count_does_not_depend_on_the_axis(self):
        def count(color):
            with CaptureQueriesContext(connection) as captured:
                self.analysis(color=color, tab="usage")
            return len(captured)

        self.assertEqual(count("hand"), count("natural"))

    def test_depth_tab_legend_counts_the_moves_tables_too(self):
        # 入退団の表も同じページで色が付くので、凡例の人数に含める（画面に出る色は必ず凡例に載る）
        analysis = self.analysis()
        shown = [
            p.tone.key
            for t in (analysis.pitchers, analysis.fielders)
            for r in t.rows
            for c in r.cells
            for p in c.players
        ]
        shown += [row.tone.key for row in (*analysis.joiners, *analysis.leavers)]
        self.assertGreater(len(analysis.joiners), 0)
        self.assertEqual(sum(item.count for item in analysis.legend), len(shown))
        # 入退団の表は本職の軸では対象外。デプス表に対象外が無くても、入退団に出れば凡例に載る
        natural = self.analysis(color="natural")
        self.assertIn("対象外", [item.category.label for item in natural.legend])

    def test_every_tone_key_has_a_css_class(self):
        css = (pathlib.Path(settings.BASE_DIR) / "myapp/static/myapp/css/theme.css").read_text(encoding="utf-8")
        keys = {c.key for axis in ColorAxis for c in color_categories(axis)} | {NEUTRAL_CATEGORY.key}
        for key in keys:
            with self.subTest(key=key):
                self.assertRegex(css, rf"\.player-tone-{re.escape(key)}\b")
