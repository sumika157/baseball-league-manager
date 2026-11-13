"""ドラフトの抽選の記録とオフの結果の盤（#298 D4）。

仕様（#298 の計画・2026-10-12 ユーザー決定「抽選の記録は締めた後も残す（`PennantDraftLottery`。オフの結果に
「N球団競合」）」・10-24 決定「オフの結果の盤（巡 × 球団）は D4」）:

- 締める処理が、その年の会議の抽選（会議を開いた年は再生した会議、開かなかった年は全自動で進めた会議）を、
  当たった球団がその巡で指名した新人に結びつけて残す。会議の記録は締めると消えるが、抽選は残る
- 記録は年・巡・当たった球団を持たない（選手の指名と最初の在籍の球団で分かる）
- オフの結果は選んだリーグの盤（巡 × 球団）を出し、抽選のセルと新人の表に「N球団競合」と入札した球団を添える
- 世界の削除で消える。実データ・世界の外の選手と球団には書けない

土台は `world_case.PostseasonWorldCase`（2リーグ×3球団・公式戦を終えた方式「なし」の世界）。抽選が必ず起きるように、
規則を全巡入札にする（毎巡、6球団が入札する）。
"""

from __future__ import annotations

from django.urls import reverse

from myapp.domain.exceptions import InvalidDraftAction, InvalidWorld
from myapp.domain.pennant.draft_lottery import DraftLotteryRecord
from myapp.domain.pennant.draft_order import DraftOrderRule
from myapp.domain.pennant.world import WorldScope
from myapp.infrastructure import orm_models
from myapp.infrastructure.queries import DjangoOffseasonQuery
from myapp.infrastructure.repositories import DjangoDraftLotteryRepository, DjangoDraftRepository
from myapp.presentation.views import (
    build_pennant_draft_service,
    build_pennant_offseason_service,
    build_pennant_world_service,
)

from .world_case import YEAR, PostseasonWorldCase


class LotteryCase(PostseasonWorldCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        orm_models.PennantWorld.objects.filter(id=cls.none_world).update(
            draft_order_rule=DraftOrderRule.ALL_LOTTERY.value
        )

    def close(self):
        return build_pennant_offseason_service(self.none_world).close_season(expected_year=YEAR)

    def facts(self):
        return DjangoOffseasonQuery(WorldScope.pennant(self.none_world)).facts(YEAR)

    def assert_records_point_to_the_winners_picks(self, facts) -> None:
        """どの記録も、その年の指名で入団した新人を指し、当たった球団（新人の球団）は入札した球団に含まれる。"""
        rookies = {fact.player_id: fact for fact in facts.rookies}
        for record in facts.lotteries:
            with self.subTest(player=record.player_id):
                fact = rookies[record.player_id]
                self.assertEqual(fact.profile.draft.year, YEAR)
                self.assertIsNotNone(fact.profile.draft.round)
                self.assertIn(fact.team_id, record.bidder_ids)
                self.assertGreaterEqual(len(record.bidder_ids), 2)


class CloseWritesLotteriesTest(LotteryCase):
    def test_closing_without_a_meeting_keeps_the_lotteries_of_the_automatic_draft(self):
        """会議を開かずに締めた年も、全自動で進めた会議の抽選が残る。1位の本指名の抽選の数は締めた結果と同じ。"""
        result = self.close()

        facts = self.facts()
        self.assertTrue(facts.lotteries, "前提: 全巡入札なので抽選がある")
        self.assert_records_point_to_the_winners_picks(facts)
        rookies = {fact.player_id: fact for fact in facts.rookies}
        first_round = [
            record
            for record in facts.lotteries
            if rookies[record.player_id].profile.draft.round == 1
            and not rookies[record.player_id].profile.draft.is_development
        ]
        self.assertEqual(len(first_round), result.first_round_lottery_count)

    def test_closing_a_held_meeting_keeps_the_lotteries_of_that_meeting(self):
        """GM が開いた会議の抽選（当たった球団・指名した候補・入札した球団）が、そのまま記録になる。

        会議の記録は消える。"""
        service = build_pennant_draft_service(self.none_world)
        started = service.start(expected_year=YEAR)
        finished = service.auto_complete(expected_year=YEAR, expected_seq=started.last_seq)
        expected = sorted(
            (move.team_name, move.prospect_name, move.lottery.bidders)
            for move in (*started.moves, *finished.moves)
            if move.lottery is not None
        )
        self.assertTrue(expected, "前提: 全巡入札なので抽選がある")

        self.close()

        facts = self.facts()
        self.assertIsNone(DjangoDraftRepository(WorldScope.pennant(self.none_world)).last_seq(YEAR))
        rookies = {fact.player_id: fact for fact in facts.rookies}
        names = {team.id: team.name for team in facts.teams}
        recorded = sorted(
            (
                rookies[record.player_id].team_name,
                rookies[record.player_id].name,
                tuple(names[team_id] for team_id in record.bidder_ids),
            )
            for record in facts.lotteries
        )
        self.assertEqual(recorded, expected)

    def test_deleting_the_world_removes_the_lotteries(self):
        self.close()
        self.assertTrue(orm_models.PennantDraftLotteryBid.objects.exists())

        build_pennant_world_service().delete_world(self.none_world)

        self.assertFalse(orm_models.PennantDraftLottery.objects.exists())
        self.assertFalse(orm_models.PennantDraftLotteryBid.objects.exists())


class LotteryRepositoryTest(LotteryCase):
    def setUp(self):
        super().setUp()
        scope = WorldScope.pennant(self.none_world)
        team_ids = list(orm_models.Team.objects.filter(league__world_id=self.none_world).values_list("id", flat=True))
        self.teams = team_ids[:2]
        self.player_id = (
            orm_models.PlayerStint.objects.filter(team_id=self.teams[0]).values_list("player_id", flat=True).first()
        )
        self.repository = DjangoDraftLotteryRepository(scope)

    def record(self, **changes) -> DraftLotteryRecord:
        values = {"player_id": self.player_id, "attempt": 1, "bidder_ids": tuple(self.teams)} | changes
        return DraftLotteryRecord(**values)

    def test_writes_the_record_and_its_bidders(self):
        self.repository.add_all([self.record()])

        lottery = orm_models.PennantDraftLottery.objects.get(player_id=self.player_id)
        self.assertEqual(lottery.attempt, 1)
        self.assertEqual(sorted(lottery.bids.values_list("team_id", flat=True)), sorted(self.teams))

    def test_refuses_the_real_scope_outside_rows_and_a_second_record(self):
        with self.assertRaises(InvalidWorld):
            DjangoDraftLotteryRepository(WorldScope.real()).add_all([self.record()])
        npb_team = orm_models.Team.objects.filter(league__world_id=self.npb_world).values_list("id", flat=True).first()
        with self.assertRaises(InvalidDraftAction):
            self.repository.add_all([self.record(bidder_ids=(self.teams[0], npb_team))])
        self.assertFalse(orm_models.PennantDraftLottery.objects.exists(), "どれかが書けなければ何も書かない")

        self.repository.add_all([self.record()])
        with self.assertRaises(InvalidDraftAction):
            self.repository.add_all([self.record()])
        self.assertEqual(orm_models.PennantDraftLottery.objects.count(), 1)


class OffseasonBoardTest(LotteryCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        build_pennant_offseason_service(cls.none_world).close_season(expected_year=YEAR)

    def offseason_url(self, league_id: int | None = None) -> str:
        url = reverse("pennant_offseason", args=[self.none_world, YEAR])
        return url if league_id is None else f"{url}?league={league_id}"

    def test_the_board_lays_out_the_selected_leagues_picks_by_round_and_club(self):
        facts = self.facts()
        for league in facts.leagues:
            with self.subTest(league=league.name):
                response = self.client.get(self.offseason_url(league.id))
                summary = response.context["summary"]
                board = summary.board
                clubs = [team for team in facts.teams if team.league_id == league.id]
                self.assertEqual([column.team_id for column in board.columns], [team.id for team in clubs])
                # どのセルも、その列の球団がその巡で指名して入団した新人
                rookies = {fact.player_id: fact for fact in facts.rookies}
                placed = 0
                for row in board.rows:
                    for column, cell in zip(board.columns, row.cells, strict=True):
                        if cell is None:
                            continue
                        placed += 1
                        fact = rookies[cell.player_id]
                        self.assertEqual(fact.team_id, column.team_id)
                        self.assertEqual(row.is_development, fact.profile.draft.is_development)
                        self.assertTrue(row.round_label.endswith(f"{fact.profile.draft.round}巡目"))
                drafted = [
                    fact for fact in facts.rookies if fact.league_id == league.id and fact.profile.draft is not None
                ]
                self.assertEqual(placed, len(drafted), "指名で入団した新人は全員、盤に1回だけ載る")
                self.assertContains(response, 'data-testid="draft-board"')

    def test_lotteries_are_shown_as_n_clubs_competing(self):
        facts = self.facts()
        record = facts.lotteries[0]
        fact = next(fact for fact in facts.rookies if fact.player_id == record.player_id)
        names = {team.id: team.name for team in facts.teams}

        response = self.client.get(self.offseason_url(fact.league_id))

        label = f"{len(record.bidder_ids)}球団競合（{'・'.join(names[team_id] for team_id in record.bidder_ids)}）"
        self.assertContains(response, label)
        rookie = next(row for row in response.context["summary"].rookies if row.player_id == record.player_id)
        self.assertEqual(rookie.lottery.label, f"{len(record.bidder_ids)}球団競合")
        self.assertEqual(rookie.lottery.winner, fact.team_name)

    def test_a_year_closed_before_the_records_shows_the_board_without_the_lottery_mark(self):
        """D4 より前に締めた年（抽選の記録が無い）は、盤は出るが「競合」の印は無い。"""
        orm_models.PennantDraftLottery.objects.all().delete()

        response = self.client.get(self.offseason_url())

        self.assertContains(response, 'data-testid="draft-board"')
        self.assertNotContains(response, "球団競合（")
