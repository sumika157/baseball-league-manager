"""ドラフト会議の候補の並べ替え（`sorting.sort_prospects`。#297 D3 T3）。

仕様（#297 の D3 実装計画・`docs/design/draft-meeting.md`）: 既定は見込みの区分の高い順。同値は候補の番号の小さい順。
不正なキーはエラーにせず既定に落とす（CLAUDE.md「並べ替えのキーと既定の向きはドメイン層が持つ」）。指名済みの候補は
向きによらず末尾（もう選べない候補で上位を埋めない。能力の無い選手を末尾に回す能力の表と同じ考え方）。
"""

import unittest
from dataclasses import dataclass

from myapp.domain.services.sorting import DEFAULT_PROSPECT_SORT, sort_prospects
from myapp.domain.simulation.ratings import RatingGrade
from myapp.domain.value_objects import Position


@dataclass(frozen=True)
class Row:
    key: int
    position: Position
    age: int
    grade: RatingGrade
    is_taken: bool = False


def keys(rows) -> list[int]:
    return [row.key for row in rows]


class SortProspectsTest(unittest.TestCase):
    def setUp(self):
        self.rows = [
            Row(1, Position.OUTFIELDER, 22, RatingGrade.C),
            Row(2, Position.PITCHER, 18, RatingGrade.A),
            Row(3, Position.CATCHER, 24, RatingGrade.C),
            Row(4, Position.PITCHER, 18, RatingGrade.S, is_taken=True),
            Row(5, Position.INFIELDER, 20, RatingGrade.B),
        ]

    def test_the_default_is_the_higher_grade_first_and_taken_ones_last(self):
        ordered, key, descending = sort_prospects(self.rows)

        self.assertEqual(key, DEFAULT_PROSPECT_SORT)
        self.assertTrue(descending)
        # S の 4 は指名済みなので末尾。C どうし（1・3）は番号の小さい順
        self.assertEqual(keys(ordered), [2, 5, 1, 3, 4])

    def test_an_unknown_key_falls_back_to_the_default(self):
        ordered, key, descending = sort_prospects(self.rows, "potential", None)

        self.assertEqual((key, descending), (DEFAULT_PROSPECT_SORT, True))
        self.assertEqual(keys(ordered), [2, 5, 1, 3, 4])

    def test_ties_stay_in_number_order_in_either_direction(self):
        ascending, _, _ = sort_prospects(self.rows, "grade", False)

        self.assertEqual(keys(ascending), [1, 3, 5, 2, 4])

    def test_position_follows_the_order_of_the_positions(self):
        """位置は投手・捕手・内野手・外野手の順（`Position` の定義の順）。既定は昇順。"""
        ordered, key, descending = sort_prospects(self.rows, "position", None)

        self.assertEqual((key, descending), ("position", False))
        self.assertEqual(keys(ordered), [2, 3, 5, 1, 4])

    def test_age_is_younger_first_by_default(self):
        ordered, _, descending = sort_prospects(self.rows, "age", None)

        self.assertFalse(descending)
        self.assertEqual(keys(ordered), [2, 5, 1, 3, 4])
        older_first, _, _ = sort_prospects(self.rows, "age", True)
        self.assertEqual(keys(older_first), [3, 1, 5, 2, 4], "指名済みは向きによらず末尾")
