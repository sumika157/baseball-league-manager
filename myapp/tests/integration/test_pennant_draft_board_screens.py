"""ドラフト会議の画面の盤・候補の絞り込みと並べ替え、世界の一覧の「ドラフト会議中」（#298 D4）。

仕様（#298 の計画）:

- 会議の画面に、受け持つ球団のリーグの盤（巡 × 球団）を出す（オフの結果と同じ部分テンプレート）
- 候補を位置・経歴で絞り込める（値は `Position`・`DraftRoute` の名前。不正な値は絞らない）。並べ替えのキーに「経歴」。
  指名の POST は並べ替えと絞り込みを保って戻る
- 世界の一覧は、いまの年度の会議を始めて締める前の世界に「ドラフト会議中」を出す（会議は再生しない＝世界の数で
  クエリが増えない）。締めたら消える

土台は `test_pennant_draft_screens.DraftScreenCase`（方式「なし」の世界で会議を開く）。
"""

from __future__ import annotations

from urllib.parse import urlencode

from django.db import connection
from django.test.utils import CaptureQueriesContext

from myapp.domain.pennant.draft import DraftRoute
from myapp.domain.pennant.draft_order import DraftOrderRule
from myapp.domain.value_objects import Position
from myapp.infrastructure import orm_models
from myapp.presentation.views import build_pennant_draft_service, build_pennant_offseason_service

from ..helpers import world_scope, world_summary_query, ws_url
from .test_pennant_draft_screens import DraftScreenCase
from .world_case import YEAR


class MeetingBoardTest(DraftScreenCase):
    def test_there_is_no_board_before_any_pick_is_settled(self):
        """1位の入札は抽選が済むまで確定しないので、始めた直後（GM の1位の入札の番）は盤を出さない。"""
        self.start()

        self.assertIsNone(self.view().board)
        self.assertNotContains(self.client.get(self.draft_url()), 'data-testid="draft-board"')

    def test_the_board_shows_the_picks_so_far_for_the_managed_clubs_league(self):
        self.start()
        first = self.view()
        prospect = next(row for row in first.prospects if row.selectable)
        build_pennant_draft_service(world_scope(self.none_world)).nominate(
            prospect.key, expected_year=YEAR, expected_seq=first.last_seq
        )
        draft = self.view()
        managed = orm_models.PennantWorld.objects.get(id=self.none_world).managed_team_id
        league_id = orm_models.Team.objects.get(id=managed).league_id
        league_teams = list(
            orm_models.Team.objects.filter(league_id=league_id).order_by("id").values_list("id", flat=True)
        )

        self.assertIsNotNone(draft.board)
        self.assertEqual([column.team_id for column in draft.board.columns], league_teams)
        self.assertEqual([column.is_mine for column in draft.board.columns], [t == managed for t in league_teams])
        # 確定した指名（自軍のリーグの球団の分）が、その球団の列のその巡に載る（会議の途中はまだ選手がいない）
        cells = [cell for row in draft.board.rows for cell in row.cells if cell is not None]
        self.assertTrue(all(cell.player_id is None for cell in cells))
        own = {move.prospect_name for move in draft.own_moves if move.prospect_key is not None}
        self.assertEqual({cell.name for cell in cells if cell.team_id == managed}, own)

        response = self.client.get(self.draft_url())
        self.assertContains(response, 'data-testid="draft-board"')

    def test_the_board_after_the_meeting_has_every_clubs_picks_with_lottery_marks(self):
        # 抽選が必ず起きるように全巡入札にする（毎巡、6球団が入札する）
        orm_models.PennantWorld.objects.filter(id=self.none_world).update(
            draft_order_rule=DraftOrderRule.ALL_LOTTERY.value
        )
        started = build_pennant_draft_service(world_scope(self.none_world)).start(expected_year=YEAR)
        finished = build_pennant_draft_service(world_scope(self.none_world)).auto_complete(
            expected_year=YEAR, expected_seq=started.last_seq
        )
        draft = self.view()

        columns = {column.team_id for column in draft.board.columns}
        picks = [
            move
            for move in (*started.moves, *finished.moves)
            if move.prospect_key is not None and move.team_id in columns
        ]
        cells = {(cell.team_id, cell.name): cell for row in draft.board.rows for cell in row.cells if cell is not None}
        self.assertEqual(len(cells), len(picks), "自軍のリーグの球団の指名は全員、盤に1回だけ載る")
        moves = [move for move in picks if move.lottery is not None]
        self.assertTrue(moves, "前提: 自軍のリーグの球団が当たった抽選がある")
        for move in moves:
            with self.subTest(move=move.prospect_name):
                cell = cells[(move.team_id, move.prospect_name)]
                self.assertEqual(cell.lottery.label, f"{len(move.lottery.bidders)}球団競合")


class ProspectFilterTest(DraftScreenCase):
    def setUp(self):
        super().setUp()
        self.start()
        self.client.force_login(self.owner)

    def test_filters_by_position_and_route(self):
        draft = build_pennant_draft_service(world_scope(self.none_world)).view(
            expected_year=YEAR, position=Position.PITCHER.name, route=DraftRoute.UNIVERSITY.name
        )

        self.assertTrue(draft.prospects, "前提: 大学の投手の候補がいる")
        self.assertTrue(
            all(row.position is Position.PITCHER and row.route is DraftRoute.UNIVERSITY for row in draft.prospects)
        )
        self.assertEqual((draft.position_filter, draft.route_filter), (Position.PITCHER, DraftRoute.UNIVERSITY))
        self.assertGreater(draft.prospect_total, len(draft.prospects))
        present = {row.route for row in self.view().prospects}
        self.assertEqual(draft.route_choices, tuple(route for route in DraftRoute if route in present))
        self.assertIn(Position.PITCHER, draft.position_choices)

        response = self.client.get(f"{self.draft_url()}?position=PITCHER&route=UNIVERSITY")
        self.assertContains(response, f"{draft.prospect_total}人中 {len(draft.prospects)}人")
        self.assertContains(response, '<option value="PITCHER" selected>')

    def test_an_unknown_value_does_not_filter(self):
        all_rows = self.view().prospects

        response = self.client.get(f"{self.draft_url()}?position=投手&route=nowhere")

        draft = response.context["draft"]
        self.assertEqual(len(draft.prospects), len(all_rows))
        self.assertEqual((draft.position_filter, draft.route_filter), (None, None))

    def test_route_is_a_sort_key(self):
        response = self.client.get(f"{self.draft_url()}?sort=route&dir=asc")

        draft = response.context["draft"]
        self.assertEqual(draft.sort, "route")
        open_rows = [row for row in draft.prospects if not row.is_taken]
        order = list(DraftRoute)
        self.assertEqual(
            [order.index(row.route) for row in open_rows], sorted(order.index(row.route) for row in open_rows)
        )

    def test_nominating_keeps_the_sort_and_filter(self):
        draft = build_pennant_draft_service(world_scope(self.none_world)).view(expected_year=YEAR, position="PITCHER")
        prospect = next(row for row in draft.prospects if row.selectable)
        sent = {"sort": "age", "dir": "asc", "position": "PITCHER", "route": ""}

        response = self.client.post(
            self.act_url("nominate"), {"expected_seq": draft.last_seq, "prospect": prospect.key, **sent}
        )

        self.assertRedirects(
            response, f"{self.draft_url()}?{urlencode({'sort': 'age', 'dir': 'asc', 'position': 'PITCHER'})}"
        )


class WorldListDraftingTest(DraftScreenCase):
    def list_rows(self):
        response = self.client.get(ws_url("pennant_index"))
        rows = [*response.context["worlds"].mine, *response.context["worlds"].others]
        return response, {row.context.world_id: row for row in rows}

    def test_a_started_meeting_is_listed_until_the_season_is_closed(self):
        response, rows = self.list_rows()
        self.assertFalse(rows[self.none_world].draft_in_progress)
        self.assertNotContains(response, 'data-testid="world-drafting"')

        self.start()
        response, rows = self.list_rows()
        self.assertTrue(rows[self.none_world].draft_in_progress)
        self.assertFalse(rows[self.npb_world].draft_in_progress)
        self.assertContains(response, "ドラフト会議中")

        service = build_pennant_draft_service(world_scope(self.none_world))
        service.auto_complete(expected_year=YEAR, expected_seq=self.last_seq())
        _, rows = self.list_rows()
        self.assertTrue(rows[self.none_world].draft_in_progress, "指名を終えて締める前も会議中")

        build_pennant_offseason_service(world_scope(self.none_world)).close_season(expected_year=YEAR)
        response, rows = self.list_rows()
        self.assertFalse(rows[self.none_world].draft_in_progress)
        self.assertNotContains(response, 'data-testid="world-drafting"')

    def test_the_list_reads_the_meetings_with_one_query_regardless_of_the_worlds(self):
        """会議を再生せず、世界の数にかかわらず一定のクエリ数で読む（会議を始めても数は変わらない）。"""
        query = world_summary_query()
        with CaptureQueriesContext(connection) as before:
            query.list_all()
        self.start()
        with CaptureQueriesContext(connection) as after:
            summaries = {summary.world_id: summary for summary in query.list_all()}

        self.assertEqual(len(after), len(before))
        self.assertEqual(summaries[self.none_world].draft_year, YEAR)
        self.assertIsNone(summaries[self.npb_world].draft_year)
