"""ドラフトの抽選の記録（`draft_lottery`。#298 D4）。

仕様（#298 の計画・2026-10-12 ユーザー決定「抽選の記録は締めた後も残す」）: 締めた年の抽選を、当たった球団がその巡で
指名した選手に結びつけて残す。年・巡・当たった球団は持たない（選手の指名と最初の在籍で分かる）。抽選は2球団以上の
重ならない入札で、表記は「N球団競合」。結びつく新人が見つからないときは黙って落とさない。
"""

import unittest

from myapp.domain.pennant.draft_lottery import DraftLotteryRecord, contest_label, lottery_records
from myapp.domain.pennant.draft_meeting import Lottery


class DraftLotteryRecordTest(unittest.TestCase):
    def test_bidders_are_kept_in_id_order_and_labelled_by_their_count(self):
        record = DraftLotteryRecord(player_id=10, attempt=1, bidder_ids=(5, 2, 9))

        self.assertEqual(record.bidder_ids, (2, 5, 9))
        self.assertEqual(record.label, "3球団競合")
        self.assertEqual(contest_label(2), "2球団競合")

    def test_a_lottery_needs_two_distinct_bidders_and_a_positive_attempt(self):
        for bidders, attempt in (((1,), 1), ((1, 1), 1), ((1, 2), 0)):
            with self.subTest(bidders=bidders, attempt=attempt), self.assertRaises(ValueError):
                DraftLotteryRecord(player_id=10, attempt=attempt, bidder_ids=bidders)


class LotteryRecordsTest(unittest.TestCase):
    def test_each_lottery_points_to_the_player_the_winner_drafted_in_that_round(self):
        """1位の抽選（1回目）・外れた球団の再入札の抽選（2回目）・育成の抽選を、当たった球団のその巡の新人に結びつける。"""
        lotteries = [
            Lottery(round_number=1, attempt=1, prospect_key=7, bidders=(1, 2, 3), winner=2),
            Lottery(round_number=1, attempt=2, prospect_key=9, bidders=(1, 3), winner=3),
            Lottery(round_number=2, attempt=1, prospect_key=4, bidders=(1, 2), winner=1, is_development=True),
        ]
        drafted = {(2, 1, False): 100, (3, 1, False): 101, (1, 1, False): 102, (1, 2, True): 103, (1, 2, False): 104}

        records = lottery_records(lotteries, drafted)

        self.assertEqual(
            records,
            (
                DraftLotteryRecord(player_id=100, attempt=1, bidder_ids=(1, 2, 3)),
                DraftLotteryRecord(player_id=101, attempt=2, bidder_ids=(1, 3)),
                DraftLotteryRecord(player_id=103, attempt=1, bidder_ids=(1, 2)),
            ),
        )

    def test_a_missing_draftee_is_an_error_not_a_silent_drop(self):
        lottery = Lottery(round_number=1, attempt=1, prospect_key=7, bidders=(1, 2), winner=2)

        with self.assertRaises(ValueError):
            lottery_records([lottery], {(2, 1, True): 100})

    def test_no_lotteries_means_no_records(self):
        self.assertEqual(lottery_records([], {(1, 1, False): 100}), ())
