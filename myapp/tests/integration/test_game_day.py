"""試合の開始・終了・中断・観客数（#44 段階1）。保存・読み戻し・表示・管理画面・警告。

保存 API のキーの欠落と検証は `test_game_scorebook_api.py`、編集画面の payload は
`test_game_edit_payload.py`（golden）にある。
"""

from datetime import date, time
from typing import Any

from django.contrib.auth.models import User
from django.urls import reverse

from myapp.domain.entities import Game
from myapp.domain.value_objects import GameTimes, Season
from myapp.infrastructure import orm_models
from myapp.infrastructure.queries import DjangoHomeStadiumQuery
from myapp.infrastructure.repositories import DjangoGameRepository

from ..helpers import play_game, real_scope
from .base import BaseCase
from .world_case import WorldCase

REAL = real_scope()
TIMES = GameTimes(start=time(18, 19), end=time(0, 5), ends_next_day=True, interruption_minutes=25)


class GameDayRepositoryTest(BaseCase):
    """3か所（save・add_all・読み戻し）すべてで値が往復する。"""

    def _new_game(self, **overrides) -> Game:
        values: dict[str, Any] = {
            "season": Season(2026),
            "played_on": date(2026, 4, 1),
            "home_team_id": self.team.id,
            "away_team_id": self.rival.id,
            "times": TIMES,
            "attendance": 42071,
        }
        values.update(overrides)
        return Game(**values)

    def test_save_then_find_by_id_round_trips_the_values(self):
        repository = DjangoGameRepository(REAL)
        saved = repository.save(self._new_game())

        found = repository.find_by_id(saved.id)

        self.assertEqual(found.times, TIMES)
        self.assertEqual(found.attendance, 42071)

    def test_an_unrecorded_game_has_no_times_and_no_attendance(self):
        repository = DjangoGameRepository(REAL)
        saved = repository.save(self._new_game(times=None, attendance=None))

        found = repository.find_by_id(saved.id)

        self.assertIsNone(found.times)
        self.assertIsNone(found.attendance)

    def test_add_all_round_trips_the_values(self):
        repository = DjangoGameRepository(REAL)
        repository.add_all([self._new_game(), self._new_game(played_on=date(2026, 4, 2), times=None, attendance=None)])

        first, second = sorted(repository.find_all(), key=lambda game: game.played_on)

        self.assertEqual((first.times, first.attendance), (TIMES, 42071))
        self.assertEqual((second.times, second.attendance), (None, None))

    def test_saving_an_aggregate_read_for_a_list_keeps_the_recorded_values(self):
        """一覧用に読んだ集約を保存しても、記録済みの値が消えない（`_to_domain` の写し忘れの再発防止）。"""
        repository = DjangoGameRepository(REAL)
        saved = repository.save(self._new_game())

        read = next(game for game in repository.find_all() if game.id == saved.id)
        repository.save(read)

        after = repository.find_by_id(saved.id)
        self.assertEqual(after.times, TIMES)
        self.assertEqual(after.attendance, 42071)


class HomeStadiumQueryTest(BaseCase):
    def test_returns_the_name_and_capacity_of_the_home_stadium(self):
        orm_models.Stadium.objects.filter(id=self.stadium.id).update(capacity=40000)

        stadium = DjangoHomeStadiumQuery(REAL).home_stadium_of(self.team.id)

        assert stadium is not None
        self.assertEqual((stadium.name, stadium.capacity), ("テスト球場", 40000))

    def test_a_team_without_a_home_stadium_gives_none(self):
        self.assertIsNone(DjangoHomeStadiumQuery(REAL).home_stadium_of(self.rival.id))


class GameDetailHeaderTest(BaseCase):
    def setUp(self):
        super().setUp()
        self.game = play_game(self.team, self.rival)

    def _detail(self):
        return self.client.get(reverse("game_detail", args=[self.game.id]))

    def test_the_recorded_items_are_shown_under_the_title(self):
        orm_models.Game.objects.filter(id=self.game.id).update(
            start_time=time(18, 19),
            end_time=time(21, 9),
            interruption_minutes=0,
            attendance=42071,
        )

        response = self._detail()

        self.assertContains(response, "開始 18:19 ◇ 終了 21:09 ◇ 試合時間 2時間50分 ◇ 観客 42,071人")
        self.assertNotContains(response, "中断")

    def test_the_interruption_and_the_next_day_are_shown(self):
        orm_models.Game.objects.filter(id=self.game.id).update(
            start_time=time(18, 0), end_time=time(0, 30), ends_next_day=True, interruption_minutes=25
        )

        response = self._detail()

        self.assertContains(response, "終了 翌00:30")
        self.assertContains(response, "試合時間 6時間5分")
        self.assertContains(response, "中断 25分")

    def test_unrecorded_items_are_not_shown_at_all(self):
        response = self._detail()

        self.assertNotContains(response, "game-day-items")
        self.assertNotContains(response, "試合時間")
        self.assertNotContains(response, "観客")

    def test_only_the_attendance_can_be_shown_alone(self):
        orm_models.Game.objects.filter(id=self.game.id).update(attendance=1234)

        response = self._detail()

        self.assertContains(response, "観客 1,234人")
        self.assertNotContains(response, "開始")


class WorldGameDetailTest(WorldCase):
    def test_the_world_detail_shows_the_same_items(self):
        orm_models.Game.objects.filter(id=self.pennant_game_id).update(
            start_time=time(14, 0), end_time=time(17, 5), attendance=30000
        )

        response = self.client.get(reverse("pennant_game_detail", args=[self.world_id, self.pennant_game_id]))

        self.assertContains(response, "開始 14:00 ◇ 終了 17:05 ◇ 試合時間 3時間5分 ◇ 観客 30,000人")


class GameAdminTest(BaseCase):
    """管理画面から直す。ドメインの規則が効き、収容人数超えは保存して警告する。"""

    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))
        self.game = play_game(self.team, self.rival)
        self.url = reverse("admin:myapp_game_change", args=[self.game.id])

    def _post(self, **overrides):
        data = {
            "year": 2026,
            "played_on": "2026-04-01",
            "start_time": "",
            "end_time": "",
            "interruption_minutes": "",
            "attendance": "",
            "_save": "1",
            # 読み取り専用の明細も管理フォームの枠（ManagementForm）は送られてくる
            **{
                f"{prefix}-{key}": 0
                for prefix in ("inning_scores", "batting_lines", "pitching_lines", "fielding_lines")
                for key in ("TOTAL_FORMS", "INITIAL_FORMS")
            },
        }
        data.update(overrides)
        return self.client.post(self.url, data, follow=True)

    def test_the_items_can_be_edited(self):
        response = self._post(start_time="18:00", end_time="21:00", interruption_minutes="10", attendance="30000")

        self.assertEqual(response.status_code, 200)
        row = orm_models.Game.objects.get(id=self.game.id)
        self.assertEqual((row.start_time, row.end_time), (time(18, 0), time(21, 0)))
        self.assertEqual((row.interruption_minutes, row.attendance), (10, 30000))

    def test_a_start_without_an_end_is_rejected(self):
        response = self._post(start_time="18:00")

        self.assertContains(response, "開始時刻と終了時刻は両方入力してください")
        self.assertIsNone(orm_models.Game.objects.get(id=self.game.id).start_time)

    def test_an_end_before_the_start_is_rejected(self):
        response = self._post(start_time="21:00", end_time="18:00")

        self.assertContains(response, "終了時刻は開始時刻より後")

    def test_an_attendance_over_the_capacity_is_saved_with_a_warning(self):
        orm_models.Stadium.objects.filter(id=self.stadium.id).update(capacity=40000)

        response = self._post(attendance="50000")

        self.assertEqual(orm_models.Game.objects.get(id=self.game.id).attendance, 50000)
        self.assertContains(response, "50,000人")
        self.assertContains(response, "収容人数（40,000人）を超えています")
        self.assertContains(response, "warning")
