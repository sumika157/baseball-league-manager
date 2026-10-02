"""試合詳細のスコアボード（安・失）とスコアブックの読み取り専用表示。"""

import re
from datetime import date

from django.urls import reverse

from myapp.domain.entities import Game
from myapp.domain.value_objects import BattingLine, LineScore, Season
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


def _tables(html: str) -> list[tuple[int, list[int]]]:
    """各表の (見出しの列数, 本体の各行の列数)。見出しと行がずれていないかを見る。"""
    tables = []
    for table in re.findall(r"<table.*?</table>", html, flags=re.DOTALL):
        head, body = table.split("</thead>")
        rows = re.findall(r"<tr.*?</tr>", body, flags=re.DOTALL)
        cells = [len(re.findall(r"<t[dh][ >]", row)) for row in rows]
        tables.append((len(re.findall(r"<th[ >]", head)), cells))
    return tables


class _DetailCase(BaseCase):
    """ホーム = self.team、ビジター = self.rival。試合を記録して詳細を開く土台。"""

    def setUp(self):
        super().setUp()
        login_as_manager(self.client, self.team, self.rival)
        self.pitcher = self.service.register_player(self.team.id, "佐藤", 18, "投手")
        self.rival_pitcher = self.service.register_player(self.rival.id, "相手投手", 19, "投手")
        self.home = register_lineup(self.service, self.team, prefix="ホーム", first_number=31)
        self.away = register_lineup(self.service, self.rival, prefix="ビジター", first_number=61)
        self.game = play_game(self.team, self.rival, home_score=0, away_score=0)

    def _post(self, *, away, home, errors=()):
        """errors は (打席の通し番号, 失策した選手 id) の並び。"""
        entries = build_scorebook(
            away=away,
            home=home,
            away_batters=self.away,
            home_batters=self.home,
            away_pitchers={1: self.rival_pitcher.id},
            home_pitchers={1: self.pitcher.id},
        )
        for sequence, player_id in errors:
            entries[sequence - 1]["errors"] = [{"player_id": player_id, "position": "遊", "kind": "送球"}]
        response = post_game_scorebook(
            self.client,
            self.game.id,
            {
                "year": 2026,
                "played_on": "2026-04-01",
                "home_team": self.team.id,
                "away_team": self.rival.id,
                "lineup": lineup_rows(self.team, self.home) + lineup_rows(self.rival, self.away),
                "plate_appearances": entries,
            },
        )
        self.assertEqual(response.status_code, 200, response.content)

    def _detail(self):
        return self.client.get(reverse("game_detail", args=[self.game.id]))


class GameDetailScoreboardTest(_DetailCase):
    def test_hits_and_errors_are_shown_per_team(self):
        # ビジターは本塁打3本（1回に2本・3回に1本）、ホームは1回に1本。
        # 1回表は本塁打2本 + 三振3つ = 通し番号1〜5、1回裏の先頭は6〜9
        self._post(
            away=[2, 0, 1],
            home=[1, 0],
            errors=[(3, self.home[0]), (9, self.away[0])],
        )

        line_score = self._detail().context["detail"].line_score

        self.assertEqual((line_score.away_hits, line_score.home_hits), (3, 1))
        # 1 回表（ビジターの攻撃）の失策はホームに、1 回裏（ホームの攻撃）の失策はビジターに付く
        self.assertEqual((line_score.away_errors, line_score.home_errors), (1, 1))

    def test_errors_are_charged_to_the_fielding_team_only(self):
        """表に2つ失策があればホームだけが2つ。ビジターは 0。"""
        self._post(away=[0], home=[0], errors=[(1, self.home[0]), (2, self.home[1])])

        line_score = self._detail().context["detail"].line_score

        self.assertEqual((line_score.away_errors, line_score.home_errors), (0, 2))

    def test_the_hits_match_the_box_score_totals(self):
        self._post(away=[2, 1], home=[3, 0])

        detail = self._detail().context["detail"]

        for box, hits in (
            (detail.away_box, detail.line_score.away_hits),
            (detail.home_box, detail.line_score.home_hits),
        ):
            self.assertEqual(sum(row.hits for row in box.batting), hits)

    def test_the_page_has_an_errors_column_aligned_with_the_rows(self):
        self._post(away=[1], home=[0])

        html = self._detail().content.decode()

        # 先頭の表がスコアボード: 見出し = 空 + 1 回 + 計・安・失
        header, rows = _tables(html)[0]
        self.assertEqual(header, 1 + 1 + 3)
        self.assertEqual(rows, [header, header])
        self.assertContains(self._detail(), ">失<")

    def test_a_game_without_plate_appearances_does_not_break_the_page(self):
        """古い記録: 打席が無ければ安打は打撃明細から、失策は「—」。"""
        batter = self.service.register_player(self.team.id, "旧記録", 99, "内野手")
        old = Game(
            season=Season(2026),
            played_on=date(2026, 4, 2),
            home_team_id=self.team.id,
            away_team_id=self.rival.id,
            home_score=2,
            away_score=1,
            line_score=LineScore(away=(1,), home=(2,)),
        )
        old.record_batting(batter.id, BattingLine(at_bats=4, singles=2), team_id=self.team.id)
        game = DjangoGameRepository().save(old)

        response = self.client.get(reverse("game_detail", args=[game.id]))

        self.assertEqual(response.status_code, 200)
        line_score = response.context["detail"].line_score
        self.assertEqual((line_score.home_hits, line_score.away_hits), (2, 0))
        self.assertIsNone(line_score.home_errors)
        self.assertContains(response, "—")
        self.assertEqual(response.context["detail"].scorebook, [])
        self.assertNotContains(response, "scorebook-table")


class GameDetailScorebookTest(_DetailCase):
    """スコアブックの読み取り専用表示。"""

    def test_each_cell_shows_the_result_of_that_plate_appearance(self):
        # 1 回表に本塁打1本 → ビジターの 1 番に「本塁打」、2〜4 番は空振り三振
        self._post(away=[1], home=[0])

        html = self._detail().content.decode()

        self.assertIn("scorebook-table", html)
        self.assertIn("本塁打", html)
        self.assertIn("空振り三振", html)
        grids = self._detail().context["detail"].scorebook
        self.assertEqual([grid.team_name for grid in grids], [self.rival.name, self.team.name])
        first_row = grids[0].rows[0]
        self.assertEqual([mark.result for mark in first_row.cells[0]], ["本塁打"])
        self.assertEqual(first_row.cells[0][0].batter_name, "ビジター1")

    def test_a_lap_of_the_order_stacks_two_plate_appearances_in_one_cell(self):
        # 本塁打を8本打つと 1 回表だけで 11 打席 = 1・2 番が 2 打席ずつ立つ
        self._post(away=[8], home=[0])

        grid = self._detail().context["detail"].scorebook[0]

        self.assertEqual(len(grid.rows[0].cells[0]), 2)
        self.assertEqual(len(grid.rows[8].cells[0]), 1)

    def test_the_grid_is_nine_rows_by_the_innings_played(self):
        self._post(away=[0, 0, 0], home=[0, 0])

        grids = self._detail().context["detail"].scorebook

        self.assertEqual([len(grid.rows) for grid in grids], [9, 9])
        self.assertEqual(grids[0].innings, [1, 2, 3])
        # 9 回裏がない場合と同じく、ホームは行った回までしか列を持たない
        self.assertEqual(grids[1].innings, [1, 2])
        for grid in grids:
            for row in grid.rows:
                self.assertEqual(len(row.cells), len(grid.innings))

    def test_scorebook_tables_keep_header_and_rows_aligned(self):
        self._post(away=[1, 0, 2], home=[0, 1])

        tables = _tables(self._detail().content.decode())

        # スコアボードの次の2つがスコアブック（ビジター・ホーム）
        for index, innings in ((1, 3), (2, 2)):
            header, rows = tables[index]
            self.assertEqual(header, 1 + innings)
            self.assertEqual(rows, [header] * 9)
