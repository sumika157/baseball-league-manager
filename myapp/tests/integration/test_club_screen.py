"""ペナントの編成画面（P5b）。

GET は誰でも、POST はオーナーだけ。区画（1軍登録・オーダー・ローテーション・抑え）ごとに、自動か手動の
どちらかの操作だけを出す。核は次の3つ。

- 自動の区画に編集欄が無く、手動の区画に「手動にする」が無い（両立しない操作を同じ区画に並べない）
- 手動にする → 保存する → 自動に戻す が往復できる
- 不正な入力・古い画面・キーの欠落は保存せず、メッセージと入力を残して再表示する
  （キーの欠落を「空」と扱うと、既存の編成が空で上書きされる）
"""

import re
from datetime import date, timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse

from myapp.application.dto import PitchingOuting, SimulationContext
from myapp.domain.pennant.club_plan import LineupChoice
from myapp.domain.pennant.schedule import AdvanceTarget
from myapp.domain.pennant.world import WorldScope
from myapp.domain.value_objects import FieldingPosition
from myapp.infrastructure import orm_models
from myapp.infrastructure.repositories import DjangoClubPlanRepository, DjangoFixtureRepository
from myapp.presentation.views import build_club_service, build_pennant_season_service

from .test_club_plan import team_of
from .test_pennant_home import HomeCase


class ClubScreenCase(HomeCase):
    def setUp(self):
        self.home_a = team_of(self.world_a, self.home.name)
        self.service = build_club_service(self.world_a)

    def club_url(self, tab=None, world_id=None) -> str:
        url = reverse("pennant_club", args=[self.world_a if world_id is None else world_id])
        return f"{url}?tab={tab}" if tab else url

    def view(self):
        return self.service.view(self.home_a.id)

    def post(self, section, action, **fields):
        return self.client.post(self.club_url(), {"section": section, "action": action, **fields})

    def get_as_owner(self, tab=None):
        self.client.force_login(self.owner)
        return self.client.get(self.club_url(tab))

    @staticmethod
    def lineup_fields(rows) -> dict[str, object]:
        """オーダーのフォームの値。rows は (選手 id, 守備位置) の並び。"""
        fields: dict[str, object] = {}
        for order, (player_id, position) in enumerate(rows, start=1):
            fields[f"lineup_player_{order}"] = player_id
            fields[f"lineup_position_{order}"] = position.value
        return fields

    def lineup_rows(self):
        return [(row.player_id, row.position) for row in self.view().lineup]

    def plan(self):
        return DjangoClubPlanRepository(WorldScope.pennant(self.world_a)).find_by_team(self.home_a.id)


class PermissionTest(ClubScreenCase):
    def test_anyone_can_read_every_tab(self):
        for user in (None, self.other, self.owner):
            for tab in (None, "active", "lineup", "pitching"):
                with self.subTest(user=user, tab=tab):
                    self.client.logout()
                    if user is not None:
                        self.client.force_login(user)
                    response = self.client.get(self.club_url(tab))
                    self.assertEqual(response.status_code, 200)
                    self.assertIn("編成", response.content.decode())

    def test_the_screen_hides_every_write_control_from_everyone_but_the_owner(self):
        self.service.set_active_roster(self.home_a.id, self.view().active_ids)
        for user in (None, self.other):
            for tab in ("active", "lineup", "pitching"):
                with self.subTest(user=user, tab=tab):
                    self.client.logout()
                    if user is not None:
                        self.client.force_login(user)
                    content = self.client.get(self.club_url(tab)).content.decode()
                    self.assertNotIn(f'action="{self.club_url()}"', content, "編成のフォームは出さない")
                    self.assertNotIn('name="action"', content)

    def test_an_anonymous_post_goes_to_the_login_and_saves_nothing(self):
        response = self.post("lineup", "manual")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])
        self.assertTrue(self.plan().is_empty)

    def test_another_user_and_an_admin_cannot_change_the_plan(self):
        for user in (self.other, User.objects.create_superuser("root", password="x")):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                self.assertEqual(self.post("lineup", "manual").status_code, 403)
                self.assertTrue(self.plan().is_empty)

    def test_other_methods_are_not_allowed(self):
        self.client.force_login(self.owner)

        self.assertEqual(self.client.put(self.club_url()).status_code, 405)

    def test_an_unknown_section_or_action_changes_nothing(self):
        self.client.force_login(self.owner)

        for data in ({"section": "nothing", "action": "manual"}, {"section": "lineup", "action": "nothing"}, {}):
            with self.subTest(data=data):
                response = self.client.post(self.club_url(), data)
                self.assertRedirects(response, self.club_url())
                self.assertTrue(self.plan().is_empty)

    def test_a_world_without_a_managed_team_opens_but_cannot_be_changed(self):
        orm_models.PennantWorld.objects.filter(id=self.world_a).update(managed_team_id=None)
        self.client.force_login(self.owner)

        self.assertEqual(self.client.get(self.club_url()).status_code, 200)
        self.assertIn("受け持つ球団が決まっていません", self.client.get(self.club_url()).content.decode())
        self.assertRedirects(self.post("lineup", "manual"), self.club_url())

    def test_an_unknown_world_is_404(self):
        self.assertEqual(self.client.get(self.club_url(world_id=self.world_a + 1000)).status_code, 404)

    def test_an_unknown_tab_falls_back_to_the_first_tab(self):
        content = self.client.get(self.club_url("zzz")).content.decode()

        self.assertIn('id="club-active"', content)


class AutoAndManualAreExclusiveTest(ClubScreenCase):
    """区画ごとに、自動の操作か手動の操作のどちらかだけ。"""

    def section(self, content, name) -> str:
        """区画のカード（id="club-<name>"）の HTML。"""
        match = re.search(
            rf'<div class="card mb-4" id="club-{name}".*?(?=<div class="card mb-4" id="club-|\Z)', content, re.DOTALL
        )
        assert match is not None, name
        return match.group(0)

    def test_an_automatic_section_has_no_edit_fields_and_offers_to_go_manual(self):
        content = self.get_as_owner("lineup").content.decode()
        lineup = self.section(content, "lineup")

        self.assertIn('data-mode="auto"', lineup)
        self.assertIn("自動編成", lineup)
        self.assertNotIn("lineup_player_1", lineup, "自動の区画に編集欄は無い")
        self.assertNotIn("保存する", lineup)
        self.assertNotIn('value="auto"', lineup, "自動の区画に「自動に戻す」は無い")
        self.assertEqual(lineup.count('value="manual"'), 1)

    def test_a_manual_section_has_edit_fields_and_no_manual_button(self):
        self.client.force_login(self.owner)
        self.post("lineup", "manual")

        lineup = self.section(self.client.get(self.club_url("lineup")).content.decode(), "lineup")

        self.assertIn('data-mode="manual"', lineup)
        self.assertIn("lineup_player_9", lineup)
        self.assertNotIn('value="manual"', lineup, "手動の区画に「手動にする」は無い")
        self.assertIn('value="save"', lineup)
        self.assertIn('value="auto"', lineup)

    def test_the_pitching_tab_switches_the_rotation_and_the_closer_separately(self):
        self.client.force_login(self.owner)
        self.post("rotation", "manual")

        content = self.client.get(self.club_url("pitching")).content.decode()
        rotation, closer = self.section(content, "rotation"), self.section(content, "closer")

        self.assertIn("rotation_1", rotation)
        self.assertNotIn('value="manual"', rotation)
        self.assertNotIn('name="closer"', closer, "自動の抑えに編集欄は無い")
        self.assertEqual(closer.count('value="manual"'), 1, "抑えはまだ自動なので「手動にする」だけ")

    def test_a_non_owner_sees_the_state_of_each_section_but_no_controls(self):
        self.post_as_owner_manual("lineup")
        self.client.logout()

        lineup = self.section(self.client.get(self.club_url("lineup")).content.decode(), "lineup")

        self.assertIn("手動", lineup)
        self.assertNotIn("<form", lineup)  # 区画の中だけを見ている（ヘッダーのフォームは含まない）
        self.assertNotIn("lineup_player_1", lineup)

    def post_as_owner_manual(self, section):
        self.client.force_login(self.owner)
        self.post(section, "manual")


class RoundTripTest(ClubScreenCase):
    def test_active_roster_round_trip(self):
        self.client.force_login(self.owner)
        before = self.view()
        self.assertFalse(before.active_is_manual)

        self.assertRedirects(self.post("active", "manual"), self.club_url("active"))
        view = self.view()
        self.assertTrue(view.active_is_manual)
        self.assertEqual(set(view.active_ids), set(before.active_ids), "いまの提案が初期値")

        # 1軍の投手のうち、ローテーションにも抑えにもいない1人を2軍に落とす
        dropped = view.bullpen_ids[-1]
        everyone = [row.player_id for row in view.players]
        fields = {"shown": everyone, "active": [pid for pid in everyone if pid != dropped]}
        self.assertRedirects(self.post("active", "save", **fields), self.club_url("active"))
        self.assertNotIn(dropped, self.view().active_ids)
        self.assertEqual(self.plan().active_ids and dropped in self.plan().active_ids, False)

        self.assertRedirects(self.post("active", "auto"), self.club_url("active"))
        self.assertFalse(self.view().active_is_manual)
        self.assertIn(dropped, self.view().active_ids)
        self.assertTrue(self.plan().is_empty)

    def test_lineup_round_trip_changes_the_next_game(self):
        self.client.force_login(self.owner)
        self.post("lineup", "manual")
        rows = list(reversed(self.lineup_rows()))

        self.assertRedirects(self.post("lineup", "save", **self.lineup_fields(rows)), self.club_url("lineup"))

        self.assertEqual(self.lineup_rows(), rows)
        self.assertEqual(self.plan().lineup, tuple(LineupChoice(pid, pos) for pid, pos in rows))
        build_pennant_season_service(self.world_a).advance(AdvanceTarget.DAY)
        game = orm_models.Game.objects.filter(home_team__league__world_id=self.world_a).order_by("played_on", "id")[0]
        lines = orm_models.GameBattingLine.objects.filter(game=game, team=self.home_a, slot_sequence=0)
        played = [line.player_id for line in lines.order_by("batting_order")]
        self.assertEqual(played, [pid for pid, _ in rows], "翌日の試合の打順が画面で決めたとおりになる")

        self.post("lineup", "auto")
        self.assertTrue(self.plan().is_empty)

    def test_rotation_round_trip(self):
        self.client.force_login(self.owner)
        self.post("rotation", "manual")
        chosen = list(reversed(self.view().rotation_ids))[:3]
        fields = {f"rotation_{order}": (chosen[order - 1] if order <= 3 else "") for order in range(1, 7)}

        self.assertRedirects(self.post("rotation", "save", **fields), self.club_url("pitching"))

        self.assertEqual(self.view().rotation_ids, tuple(chosen))
        self.post("rotation", "auto")
        self.assertFalse(self.view().rotation_is_manual)

    def test_closer_round_trip(self):
        self.client.force_login(self.owner)
        self.post("closer", "manual")
        view = self.view()
        other = view.bullpen_ids[0]
        self.assertNotEqual(other, view.closer_id)

        self.assertRedirects(self.post("closer", "save", closer=other), self.club_url("pitching"))

        self.assertEqual(self.view().closer_id, other)
        self.assertTrue(self.view().closer_is_manual)
        self.post("closer", "auto")
        self.assertFalse(self.view().closer_is_manual)

    def test_going_manual_twice_keeps_what_was_decided(self):
        self.client.force_login(self.owner)
        self.post("lineup", "manual")
        rows = list(reversed(self.lineup_rows()))
        self.post("lineup", "save", **self.lineup_fields(rows))

        self.post("lineup", "manual")

        self.assertEqual(self.lineup_rows(), rows, "すでに手動なら、決めたオーダーを提案で上書きしない")


class InvalidInputTest(ClubScreenCase):
    def test_a_duplicate_in_the_lineup_is_refused_with_the_input_kept(self):
        self.client.force_login(self.owner)
        self.post("lineup", "manual")
        saved = self.lineup_rows()
        rows = list(saved)
        rows[1] = (rows[0][0], rows[1][1])  # 1番と2番に同じ選手

        response = self.post("lineup", "save", **self.lineup_fields(rows))

        self.assertEqual(response.status_code, 400)
        self.assertIn("重複", response.content.decode())
        self.assertIn("alert-danger", response.content.decode())
        self.assertEqual(self.lineup_rows(), saved, "保存されない")
        slots = response.context["lineup_slots"]
        self.assertEqual([slot.player_id for slot in slots], [pid for pid, _ in rows], "入力を残して出し直す")

    def test_a_lineup_missing_a_position_is_refused(self):
        self.client.force_login(self.owner)
        self.post("lineup", "manual")
        saved = self.lineup_rows()
        rows = [(pid, FieldingPosition.CATCHER) for pid, _ in saved]  # 全員が捕手の枠

        response = self.post("lineup", "save", **self.lineup_fields(rows))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.lineup_rows(), saved)

    def test_missing_lineup_keys_are_not_treated_as_empty(self):
        self.client.force_login(self.owner)
        self.post("lineup", "manual")
        saved = self.lineup_rows()

        response = self.post("lineup", "save")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.lineup_rows(), saved, "キーが無いまま送られても、既存のオーダーは消えない")
        self.assertTrue(self.view().lineup_is_manual)

    def test_a_rotation_without_its_keys_is_refused_and_keeps_the_rotation(self):
        self.client.force_login(self.owner)
        self.post("rotation", "manual")
        saved = self.view().rotation_ids

        response = self.post("rotation", "save")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.view().rotation_ids, saved)
        self.assertTrue(self.view().rotation_is_manual)

    def test_a_rotation_with_every_slot_empty_is_refused(self):
        self.client.force_login(self.owner)
        self.post("rotation", "manual")
        saved = self.view().rotation_ids

        response = self.post("rotation", "save", **{f"rotation_{order}": "" for order in range(1, 7)})

        self.assertEqual(response.status_code, 400)
        self.assertIn("1人もいません", response.content.decode())
        self.assertEqual(self.view().rotation_ids, saved)

    def test_a_closer_that_is_not_a_pitcher_is_refused(self):
        self.client.force_login(self.owner)
        self.post("closer", "manual")
        batter = self.view().lineup[0].player_id

        response = self.post("closer", "save", closer=batter)

        self.assertEqual(response.status_code, 400)
        self.assertIn("投手だけ", response.content.decode())
        self.assertEqual(response.context["closer_selected"], batter, "入力を残す")

    def test_a_closer_that_is_missing_is_refused(self):
        self.client.force_login(self.owner)
        self.post("closer", "manual")

        self.assertEqual(self.post("closer", "save").status_code, 400)

    def test_an_active_roster_that_breaks_the_rules_is_refused(self):
        self.client.force_login(self.owner)
        self.post("active", "manual")
        everyone = [row.player_id for row in self.view().players]
        few = everyone[:5]

        response = self.post("active", "save", shown=everyone, active=few)

        self.assertEqual(response.status_code, 400)
        self.assertIn("野手", response.content.decode())
        self.assertEqual(set(response.context["active_checked"]), set(few), "チェックを残す")
        self.assertEqual(len(self.plan().active_ids or ()), len(self.view().active_ids), "保存されない")

    def test_a_stale_active_form_is_refused(self):
        """表示した選手の一覧が、いまの在籍と食い違う（別の画面で選手が増減した）。"""
        self.client.force_login(self.owner)
        self.post("active", "manual")
        saved = self.plan().active_ids
        everyone = [row.player_id for row in self.view().players]

        for shown in (everyone[:-1], everyone + [999999], []):
            with self.subTest(shown=len(shown)):
                response = self.post("active", "save", shown=shown, active=everyone[:3])
                self.assertEqual(response.status_code, 400)
                self.assertIn("開き直して", response.content.decode())
                self.assertEqual(self.plan().active_ids, saved)

    def test_missing_keys_do_not_wipe_the_active_roster(self):
        self.client.force_login(self.owner)
        self.post("active", "manual")
        saved = self.plan().active_ids

        response = self.post("active", "save")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.plan().active_ids, saved, "shown も active も無い送信で、1軍を空にしない")

    def test_a_checked_player_that_was_not_shown_is_refused(self):
        self.client.force_login(self.owner)
        self.post("active", "manual")
        everyone = [row.player_id for row in self.view().players]

        response = self.post("active", "save", shown=everyone, active=everyone + [999999])

        self.assertEqual(response.status_code, 400)
        self.assertIn("開き直して", response.content.decode())

    def test_a_non_numeric_id_is_refused(self):
        self.client.force_login(self.owner)
        self.post("active", "manual")
        everyone = [row.player_id for row in self.view().players]

        response = self.post("active", "save", shown=everyone, active=["abc"])

        self.assertEqual(response.status_code, 400)

    def test_a_player_from_another_world_is_refused(self):
        other_team = team_of(self.world_b, self.home.name)
        stranger = orm_models.PlayerStint.objects.filter(team=other_team).first().player_id
        self.client.force_login(self.owner)
        self.post("closer", "manual")

        response = self.post("closer", "save", closer=stranger)

        self.assertEqual(response.status_code, 400)
        self.assertNotEqual(self.view().closer_id, stranger)


class NoticesTest(ClubScreenCase):
    """使えなくなった上書きは、編成の該当タブとホームの「注意」に理由が出る。"""

    def drop_a_lineup_player(self):
        view = self.view()
        self.service.set_lineup(self.home_a.id, [LineupChoice(r.player_id, r.position) for r in view.lineup])
        dropped = view.lineup[3].player_id
        self.service.set_active_roster(self.home_a.id, [pid for pid in view.active_ids if pid != dropped])

    def test_the_club_tab_shows_the_reason_above_the_section(self):
        self.drop_a_lineup_player()

        content = self.get_as_owner("lineup").content.decode()

        self.assertIn("alert-warning", content)
        self.assertIn("1軍に登録されていない", content)
        self.assertIn('data-mode="manual"', content, "使えなくても区画は手動のまま")
        other_tab = self.client.get(self.club_url("pitching")).content.decode()
        self.assertNotIn("1軍に登録されていない選手はオーダー", other_tab, "別のタブには出さない")

    def test_the_home_shows_the_notice_only_to_the_owner(self):
        self.drop_a_lineup_player()

        self.client.force_login(self.owner)
        owner_home = self.client.get(self.home_url()).content.decode()
        self.assertIn('id="plan-notices"', owner_home)
        self.assertIn("1軍に登録されていない", owner_home)
        self.assertIn(f'href="{self.club_url("lineup")}"', owner_home, "編成の該当タブへ")

        for user in (None, self.other):
            self.client.logout()
            if user is not None:
                self.client.force_login(user)
            self.assertNotIn("plan-notices", self.client.get(self.home_url()).content.decode())

    def test_the_home_shows_no_notice_when_the_plan_is_usable(self):
        self.client.force_login(self.owner)

        self.assertNotIn("plan-notices", self.client.get(self.home_url()).content.decode())

    def test_the_notice_is_placed_right_after_the_own_team_strip(self):
        self.drop_a_lineup_player()

        self.client.force_login(self.owner)
        content = self.client.get(self.home_url()).content.decode()

        self.assertLess(content.index("stat-strip"), content.index("plan-notices"))
        self.assertLess(content.index("plan-notices"), content.index("進める</div>"))


class LimitsAndCountsTest(ClubScreenCase):
    def test_the_counts_and_the_limits_come_from_the_domain(self):
        view = self.view()
        content = self.client.get(self.club_url("active")).content.decode()

        self.assertEqual(view.limits.active_size, 29)
        self.assertIn(f"{view.counts.total}/{view.limits.active_size}", content)
        self.assertEqual(view.counts.total, view.counts.batters + view.counts.pitchers)
        self.assertGreaterEqual(view.counts.catchers, 1)
        self.assertEqual(view.limits.lineup_size, 9)
        self.assertEqual(len(view.limits.lineup_positions), 9)

    def set_foreign_limit(self, limit):
        orm_models.League.objects.filter(id=self.home_a.league_id).update(foreign_player_roster_limit=limit)

    def test_the_foreign_limit_is_shown_as_unlimited_when_the_league_has_none(self):
        self.set_foreign_limit(None)

        view = self.view()
        content = self.client.get(self.club_url("active")).content.decode()

        self.assertIsNone(view.limits.foreign_roster_limit)
        self.assertIn(f"{view.counts.foreign}/無制限", content)

    def test_a_limit_of_zero_is_shown_as_zero_not_unlimited(self):
        self.set_foreign_limit(0)

        content = self.client.get(self.club_url("active")).content.decode()

        self.assertIn("0/0", content)
        self.assertNotIn("無制限", content)


class PitchingUsageTest(ClubScreenCase):
    def test_without_a_schedule_the_usage_is_not_shown(self):
        content = self.client.get(self.club_url("pitching")).content.decode()

        self.assertIn("救援陣", content)
        self.assertIn("日程が残っていない", content)

    def test_the_usage_is_derived_from_the_recent_games(self):
        self.start_season()
        report = build_pennant_season_service(self.world_a).advance(AdvanceTarget.DAY)
        first_day = report.played_dates[0]
        view = self.view()

        usage = self.service.pitching_usage(view)

        assert usage.next_game_on is not None
        self.assertGreater(usage.next_game_on, first_day)
        self.assertEqual(len(usage.recent_days), 3)
        self.assertEqual(
            usage.recent_days[-1], usage.next_game_on - timedelta(days=1), "直近3日は次の試合日の前日まで"
        )
        gap = (usage.next_game_on - first_day).days
        (starter,) = [row for row in usage.rotation if row.days_since_start == gap]
        if first_day in usage.recent_days:
            self.assertTrue(starter.pitched_recently[usage.recent_days.index(first_day)], "先発した日は登板に数える")
        others = [row for row in usage.rotation if row is not starter]
        self.assertTrue(all(row.days_since_start is None for row in others), "ほかの先発はまだ先発していない")

    def test_the_screen_shows_the_next_game_date(self):
        self.start_season()
        usage = self.service.pitching_usage(self.view())

        content = self.client.get(self.club_url("pitching")).content.decode()

        self.assertIn(f"{usage.next_game_on.month}月{usage.next_game_on.day}日の試合", content)
        self.assertIn("投げられる", content)


class SavedOverridesAreTheStartingPointTest(ClubScreenCase):
    """使えなくなった手動の区画の編集欄は、解決後（AI の自動）ではなく、保存済みの上書きから始める。"""

    def test_a_lineup_that_became_unusable_opens_with_the_saved_batting_order(self):
        view = self.view()
        saved = [(row.player_id, row.position) for row in view.lineup]
        self.service.set_lineup(self.home_a.id, [LineupChoice(pid, pos) for pid, pos in saved])
        dropped = saved[3][0]
        self.service.set_active_roster(self.home_a.id, [pid for pid in view.active_ids if pid != dropped])
        self.assertNotIn(
            dropped, [row.player_id for row in self.view().lineup], "いま使われるオーダーからは外れている"
        )

        response = self.get_as_owner("lineup")

        slots = response.context["lineup_slots"]
        self.assertEqual([(s.player_id, s.position) for s in slots], saved, "保存した打順と守備がそのまま初期値")
        self.assertIn("1軍外", response.content.decode(), "1軍にいない選手には印が付く")

    def test_saving_the_unusable_lineup_unchanged_is_refused_with_the_input_kept(self):
        view = self.view()
        saved = [(row.player_id, row.position) for row in view.lineup]
        self.service.set_lineup(self.home_a.id, [LineupChoice(pid, pos) for pid, pos in saved])
        self.service.set_active_roster(self.home_a.id, [pid for pid in view.active_ids if pid != saved[3][0]])
        self.client.force_login(self.owner)

        response = self.post("lineup", "save", **self.lineup_fields(saved))

        self.assertEqual(response.status_code, 400)
        self.assertIn("1軍に登録されていない", response.content.decode())
        self.assertEqual(self.plan().lineup, tuple(LineupChoice(pid, pos) for pid, pos in saved), "上書きはそのまま")

    def test_an_unusable_rotation_and_active_roster_open_with_the_saved_values(self):
        view = self.view()
        rotation = list(view.rotation_ids[:3])
        self.service.set_rotation(self.home_a.id, rotation)
        kept = [pid for pid in view.active_ids if pid != rotation[0]]
        self.service.set_active_roster(self.home_a.id, kept)
        self.assertEqual([n.section.value for n in self.view().notices], ["ローテーション"])

        response = self.get_as_owner("pitching")

        self.assertEqual([s.player_id for s in response.context["rotation_slots"] if s.player_id], rotation)
        self.assertIn("1軍外", response.content.decode())
        active = self.client.get(self.club_url("active"))
        self.assertEqual(set(active.context["active_checked"]), set(kept), "1軍登録の初期値も保存済みの手動")

    def test_a_manual_closer_opens_with_the_saved_closer(self):
        chosen = self.view().bullpen_ids[-1]
        self.service.set_closer(self.home_a.id, chosen)

        response = self.get_as_owner("pitching")

        self.assertEqual(response.context["closer_selected"], chosen)


class OtherWorldPlayersAreRefusedTest(ClubScreenCase):
    """別の世界の選手 id を送っても、保存済みの編成は変わらない。"""

    def stranger(self) -> int:
        other = team_of(self.world_b, self.home.name)
        stint = orm_models.PlayerStint.objects.filter(team=other).first()
        assert stint is not None
        return stint.player_id

    def test_no_section_accepts_a_player_of_another_world(self):
        self.client.force_login(self.owner)
        for section in ("active", "lineup", "rotation", "closer"):
            self.post(section, "manual")
        before = self.plan()
        stranger = self.stranger()
        everyone = [row.player_id for row in self.view().players]
        rows = list(self.lineup_rows())
        rows[0] = (stranger, rows[0][1])
        attempts = {
            "active": {"shown": everyone + [stranger], "active": everyone + [stranger]},
            "lineup": self.lineup_fields(rows),
            "rotation": {"rotation_1": stranger, **{f"rotation_{n}": "" for n in range(2, 7)}},
            "closer": {"closer": stranger},
        }

        for section, fields in attempts.items():
            with self.subTest(section=section):
                response = self.post(section, "save", **fields)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.plan(), before, "保存済みの編成が変わらない")


class ConsecutiveDaysTest(ClubScreenCase):
    """連投の上限に達した投手は、次の試合に投げられない。"""

    LOAD = "myapp.infrastructure.queries.DjangoSimulationContextQuery.load"

    def next_day(self):
        self.start_season()
        day = self.service.pitching_usage(self.view()).next_game_on
        assert day is not None
        return day

    def test_a_pitcher_who_pitched_the_last_two_days_cannot_pitch_next(self):
        next_day = self.next_day()
        tired = self.view().bullpen_ids[0]
        outings = (
            PitchingOuting(next_day - timedelta(days=2), (tired,)),
            PitchingOuting(next_day - timedelta(days=1), (tired,)),
        )
        context = SimulationContext(teams=(), last_starts=(), recent_outings=outings)

        with mock.patch(self.LOAD, return_value=context):
            usage = self.service.pitching_usage(self.view())
            content = self.get_as_owner("pitching").content.decode()

        by_id = {row.player_id: row for row in (*usage.bullpen, *usage.rotation)}
        self.assertFalse(by_id[tired].can_pitch)
        self.assertEqual(by_id[tired].pitched_recently[1:], (True, True))
        self.assertTrue(all(row.can_pitch for pid, row in by_id.items() if pid != tired))
        self.assertIn("連投のため休み", content)

    def test_a_day_of_rest_in_between_does_not_count_as_consecutive(self):
        next_day = self.next_day()
        pitcher = self.view().bullpen_ids[0]
        outings = (
            PitchingOuting(next_day - timedelta(days=3), (pitcher,)),
            PitchingOuting(next_day - timedelta(days=2), (pitcher,)),
        )
        context = SimulationContext(teams=(), last_starts=(), recent_outings=outings)

        with mock.patch(self.LOAD, return_value=context):
            usage = self.service.pitching_usage(self.view())

        (row,) = [r for r in usage.bullpen if r.player_id == pitcher]
        self.assertTrue(row.can_pitch)


class NextGameOfTheClubTest(ClubScreenCase):
    """次の試合日は受け持つ球団の対戦で決める（自軍が休みの日は数えない）。"""

    def test_the_first_date_of_the_club_skips_days_it_does_not_play(self):
        third = orm_models.Team.objects.create(league_id=self.home_a.league_id, name="第三球団")
        rival = team_of(self.world_a, self.rival.name)
        early, late = date(2026, 4, 1), date(2026, 4, 3)
        orm_models.PennantFixture.objects.create(date=early, home_team=rival, visitor_team=third)
        orm_models.PennantFixture.objects.create(date=late, home_team=self.home_a, visitor_team=rival)
        fixtures = DjangoFixtureRepository(WorldScope.pennant(self.world_a))

        self.assertEqual(fixtures.first_date(), early, "世界の最初の日")
        self.assertEqual(fixtures.first_date(self.home_a.id), late, "自軍の最初の日")
        self.assertEqual(fixtures.first_date(third.id), early)
        usage = self.service.pitching_usage(self.view())
        self.assertEqual(usage.next_game_on, late)
        self.assertEqual(usage.recent_days[-1], late - timedelta(days=1))

    def test_a_team_without_fixtures_has_no_first_date(self):
        self.assertIsNone(DjangoFixtureRepository(WorldScope.pennant(self.world_a)).first_date(self.home_a.id))


class UnreadableClubTest(ClubScreenCase):
    def test_a_managed_team_that_cannot_be_found_shows_a_reason_instead_of_failing(self):
        orm_models.PennantWorld.objects.filter(id=self.world_a).update(
            managed_team_id=team_of(self.world_b, self.home.name).id
        )
        self.client.force_login(self.owner)

        response = self.client.get(self.club_url())

        self.assertEqual(response.status_code, 200)
        self.assertIn("編成を読めません", response.content.decode())
        self.assertRedirects(self.post("lineup", "manual"), self.club_url())
