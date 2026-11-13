"""ドラフト会議の候補の並べ替えと絞り込み（`sorting.sort_prospects`・`filter_prospects`。#297 D3 T3・#298 D4）。

仕様（#297 の D3 実装計画・Wiki「ペナントモードの仕組み」の「ドラフト会議の保存と1巡ずつの進行」）:
既定は見込みの区分の高い順。
同値は候補の番号の小さい順。
不正なキーはエラーにせず既定に落とす（CLAUDE.md「並べ替えのキーと既定の向きはドメイン層が持つ」）。指名済みの候補は
向きによらず末尾（もう選べない候補で上位を埋めない。能力の無い選手を末尾に回す能力の表と同じ考え方）。
"""

import unittest
from dataclasses import dataclass

from myapp.domain.pennant.draft import DraftRoute
from myapp.domain.services.sorting import DEFAULT_PROSPECT_SORT, filter_prospects, sort_prospects
from myapp.domain.simulation.ratings import RatingGrade
from myapp.domain.value_objects import Position


@dataclass(frozen=True)
class Row:
    key: int
    position: Position
    age: int
    grade: RatingGrade
    is_taken: bool = False
    route: DraftRoute = DraftRoute.HIGH_SCHOOL


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


class RouteSortAndFilterTest(unittest.TestCase):
    """経歴の並べ替えと、位置・経歴の絞り込み（#298 D4）。経歴の並びは `DraftRoute` の定義の順（高校・大学・社会人）。
    絞り込みの値は値オブジェクトの名前で、不正な値は絞らない（並べ替えの不正なキーと同じく既定に落とす）。"""

    def setUp(self):
        self.rows = [
            Row(1, Position.PITCHER, 22, RatingGrade.C, route=DraftRoute.UNIVERSITY),
            Row(2, Position.CATCHER, 18, RatingGrade.A, route=DraftRoute.HIGH_SCHOOL),
            Row(3, Position.PITCHER, 24, RatingGrade.B, route=DraftRoute.CORPORATE),
            Row(4, Position.PITCHER, 18, RatingGrade.S, is_taken=True, route=DraftRoute.HIGH_SCHOOL),
            Row(5, Position.OUTFIELDER, 22, RatingGrade.B, route=DraftRoute.UNIVERSITY),
        ]

    def test_route_follows_the_order_of_the_routes(self):
        ordered, key, descending = sort_prospects(self.rows, "route", None)

        self.assertEqual((key, descending), ("route", False))
        # 高校（2）→ 大学（1・5）→ 社会人（3）、指名済みの 4 は末尾
        self.assertEqual(keys(ordered), [2, 1, 5, 3, 4])

    def test_filters_by_position_and_route_including_taken_ones(self):
        kept, position, route = filter_prospects(self.rows, position="PITCHER", route=None)
        self.assertEqual((keys(kept), position, route), ([1, 3, 4], Position.PITCHER, None))

        kept, position, route = filter_prospects(self.rows, position="PITCHER", route="HIGH_SCHOOL")
        self.assertEqual((keys(kept), position, route), ([4], Position.PITCHER, DraftRoute.HIGH_SCHOOL))

    def test_an_unknown_or_empty_value_does_not_filter(self):
        for position, route in ((None, None), ("", ""), ("投手", "pitcher"), ("label", "value")):
            with self.subTest(position=position, route=route):
                kept, chosen_position, chosen_route = filter_prospects(self.rows, position=position, route=route)
                self.assertEqual(keys(kept), [1, 2, 3, 4, 5])
                self.assertEqual((chosen_position, chosen_route), (None, None))
