"""チーム詳細の選手一覧（成績表）のレイアウトの E2E テスト。

投手の表は20列を超え、セルの折り返しを許すと、ブラウザは表を横スクロールさせずに
選手名・投打のセルを1文字幅まで縮めて押し込み、行が縦に崩れた（#115）。
CSS の効き方の問題で integration では確かめられないため、実ブラウザで測る。
"""

from django.urls import reverse

from myapp.infrastructure import orm_models

from ..helpers import build_service
from .base import PlaywrightTestCase

# セルの中身が2行以上に割れているセルを返す。
# 各セルの中身の矩形を並べ、最初の矩形の下端より下から始まる矩形があれば折り返している。
WRAPPED_CELLS_JS = """() => {
  const wrapped = [];
  for (const cell of document.querySelectorAll('.table-responsive table tbody td')) {
    const range = document.createRange();
    range.selectNodeContents(cell);
    const rects = [...range.getClientRects()].filter(r => r.width > 0 && r.height > 0);
    if (rects.length === 0) continue;
    const firstBottom = Math.min(...rects.map(r => r.bottom));
    if (rects.some(r => r.top >= firstBottom - 1)) wrapped.push(cell.innerText.trim());
  }
  return wrapped;
}"""


class RosterTableLayoutTest(PlaywrightTestCase):
    def setUp(self):
        super().setUp()
        league = orm_models.League.objects.create(name="Eリーグ")
        self.team = orm_models.Team.objects.create(league=league, name="ホームズ")
        service = build_service()
        # 長い名前・投打・バッジ（外国人）がそろうと、折り返したときに崩れが最も大きい
        pitcher = service.register_player(self.team.id, "サルバドール・グティエレス", 42, "投手")
        orm_models.Player.objects.filter(id=pitcher.id).update(
            throws="右", bats="左", height_cm=190, weight_kg=95, is_foreign_player=True
        )
        service.register_player(self.team.id, "山田太郎", 18, "投手")
        batter = service.register_player(self.team.id, "ホセ・フェルナンデス", 7, "外野手")
        orm_models.Player.objects.filter(id=batter.id).update(throws="右", bats="両", is_foreign_player=True)

    def _open(self, pos: str, width: int) -> None:
        self.page.set_viewport_size({"width": width, "height": 900})
        self.page.goto(self.live_server_url + reverse("player_list", args=[self.team.id]) + f"?pos={pos}")

    def test_pitcher_table_cells_do_not_wrap(self):
        """投手の表のセルが折り返さず、収まらない分は表の中で横スクロールすること。"""
        self._open("pitcher", 1024)

        self.assertEqual(self.page.evaluate(WRAPPED_CELLS_JS), [])
        scroll = self.page.evaluate(
            """() => {
              const w = document.querySelector('.table-responsive');
              return {wrapper: w.clientWidth, table: w.scrollWidth,
                      page: document.documentElement.scrollWidth, viewport: window.innerWidth};
            }"""
        )
        # 表はラッパーの中で横に送られ、ページそのものは横にはみ出さない
        self.assertGreater(scroll["table"], scroll["wrapper"])
        self.assertLessEqual(scroll["page"], scroll["viewport"])

    def test_batter_table_cells_do_not_wrap(self):
        """野手の表も同じ表なので、セルが折り返さないこと。"""
        self._open("batter", 1024)

        self.assertEqual(self.page.evaluate(WRAPPED_CELLS_JS), [])
