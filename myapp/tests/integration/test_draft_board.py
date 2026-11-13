"""ドラフトの盤の組み立て（`application/draft_board.py`。#298 D4）。DB は使わない。

仕様（#298 の計画）: 行は本指名の巡 → 育成の巡の順で、どこかの列が指名した巡まで。列（球団）に無い球団の指名は
置かない。その巡で指名しなかった球団の欄は空。球団は1つの巡で1人しか指名しないので、重なれば例外（黙って上書きしない）。
"""

from django.test import SimpleTestCase

from myapp.application.draft_board import BoardPick, draft_board
from myapp.application.dto import DraftBoardCell, DraftBoardColumn


def pick(team_id: int, round_number: int, name: str, *, development: bool = False) -> BoardPick:
    return BoardPick(
        round_number=round_number,
        is_development=development,
        cell=DraftBoardCell(team_id=team_id, name=name, position="投手"),
    )


COLUMNS = (
    DraftBoardColumn(team_id=1, team_name="A", is_mine=True),
    DraftBoardColumn(team_id=2, team_name="B", is_mine=False),
)


class DraftBoardTest(SimpleTestCase):
    def test_rows_are_registered_rounds_then_development_rounds(self):
        board = draft_board(
            COLUMNS,
            [
                pick(2, 1, "b1"),
                pick(1, 1, "a1"),
                pick(1, 2, "a2"),
                pick(2, 1, "b-dev1", development=True),
                pick(3, 1, "c1"),  # 列に無い球団（別のリーグ）は置かない
            ],
        )

        assert board is not None
        self.assertEqual([row.round_label for row in board.rows], ["1巡目", "2巡目", "育成1巡目"])
        names = [[cell.name if cell else None for cell in row.cells] for row in board.rows]
        self.assertEqual(names, [["a1", "b1"], ["a2", None], [None, "b-dev1"]])

    def test_no_picks_for_the_columns_means_no_board(self):
        self.assertIsNone(draft_board(COLUMNS, []))
        self.assertIsNone(draft_board(COLUMNS, [pick(3, 1, "c1")]))

    def test_two_picks_of_a_club_in_one_round_are_an_error(self):
        with self.assertRaises(ValueError):
            draft_board(COLUMNS, [pick(1, 1, "a1"), pick(1, 1, "a1b")])
