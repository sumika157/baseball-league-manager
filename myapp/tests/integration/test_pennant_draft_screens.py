"""ドラフト会議の画面（#297 D3 T3）: 権限・状態ごとの操作・古い画面・GM ホームのカード・戦力外タブの固定・年の URL。

土台の世界（2リーグ×3球団・公式戦を終えた方式「なし」と NPB 方式）は `world_case.PostseasonWorldCase`。
会議は方式「なし」の世界で開く。NPB 方式の世界はポストシーズンの前なので開けない（#297 Q1）。
仕様は #297 の「D3 実装計画」とユーザーの決定（Q1〜Q5）、`docs/design/draft-meeting.md` の T2 の申し送り。
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.urls import reverse

from myapp.domain.pennant.world import WorldScope
from myapp.infrastructure import orm_models
from myapp.infrastructure.repositories import DjangoDraftRepository
from myapp.presentation.views import build_pennant_draft_service, build_pennant_offseason_service

from .world_case import YEAR, PostseasonWorldCase


class DraftScreenCase(PostseasonWorldCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        users = get_user_model().objects
        cls.owner = users.create_user(username="draft-owner", password="pw-draft-1")
        cls.other = users.create_user(username="draft-other", password="pw-draft-2")
        orm_models.PennantWorld.objects.filter(id__in=[cls.none_world, cls.npb_world]).update(owner_id=cls.owner.id)

    def draft_url(self, year: int = YEAR, world_id: int | None = None) -> str:
        return reverse("pennant_draft", args=[world_id or self.none_world, year])

    def act_url(self, action: str, year: int = YEAR) -> str:
        return reverse("pennant_draft_act", args=[self.none_world, year, action])

    def home_url(self) -> str:
        return reverse("pennant_world", args=[self.none_world])

    def last_seq(self) -> int | None:
        return DjangoDraftRepository(WorldScope.pennant(self.none_world)).last_seq(YEAR)

    def start(self) -> None:
        build_pennant_draft_service(self.none_world).start(expected_year=YEAR)

    def view(self):
        return build_pennant_draft_service(self.none_world).view(expected_year=YEAR)

    @staticmethod
    def messages_of(response) -> list[str]:
        return [str(message) for message in get_messages(response.wsgi_request)]


class PermissionTest(DraftScreenCase):
    def test_anyone_can_read_but_only_the_owner_sees_the_operations(self):
        """GET は誰でも。書き込みの導線は未ログイン・ほかの人には出さない（CLAUDE.md「UI・設計方針」）。"""
        self.start()
        for user in (None, self.other):
            with self.subTest(user=user):
                if user is not None:
                    self.client.force_login(user)
                response = self.client.get(self.draft_url())
                self.assertEqual(response.status_code, 200)
                content = response.content.decode()
                self.assertIn('data-testid="draft-prospects"', content)
                for action in ("nominate", "pass", "delegate", "auto", "reset"):
                    self.assertNotIn(self.act_url(action), content, "操作の URL を出さない")
                self.assertNotIn('name="prospect"', content)
                self.assertNotIn("残りをお任せにする", content)

        self.client.force_login(self.owner)
        content = self.client.get(self.draft_url()).content.decode()
        self.assertIn('name="prospect"', content)
        self.assertIn(f'action="{self.act_url("nominate")}"', content)
        self.assertIn("残りをお任せにする", content)

    def test_writes_need_the_owner(self):
        """未ログインはログインへ、ほかの人は 403。どちらも何も書かない。"""
        response = self.client.post(self.act_url("start"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

        self.client.force_login(self.other)
        self.assertEqual(self.client.post(self.act_url("start")).status_code, 403)
        self.assertIsNone(self.last_seq())

    def test_get_on_an_action_returns_to_the_meeting_and_an_unknown_action_is_404(self):
        self.client.force_login(self.owner)
        response = self.client.get(self.act_url("start"))
        self.assertRedirects(response, self.draft_url())
        self.assertIsNone(self.last_seq())
        self.assertEqual(self.client.post(self.act_url("undo")).status_code, 404)


class StartTest(DraftScreenCase):
    def test_the_owner_starts_the_meeting(self):
        self.client.force_login(self.owner)
        content = self.client.get(self.draft_url()).content.decode()
        self.assertIn(f'action="{self.act_url("start")}"', content)
        self.assertIn("戦力外の指名とドラフトの指名順の規則は変えられません", content)

        response = self.client.post(self.act_url("start"))

        self.assertRedirects(response, self.draft_url())
        self.assertIsNotNone(self.last_seq())
        self.assertIn(f"{YEAR}年のドラフト会議を始めました。", self.messages_of(response))
        content = self.client.get(self.draft_url()).content.decode()
        self.assertNotIn(f'action="{self.act_url("start")}"', content, "始めた後は「始める」を出さない")
        self.assertIn("あなたの番", content)

    def test_the_meeting_cannot_be_opened_before_the_postseason_ends(self):
        """ポストシーズンの前は理由だけを出し、オーナーにも「始める」を出さない（#297 Q1）。"""
        self.client.force_login(self.owner)
        content = self.client.get(self.draft_url(world_id=self.npb_world)).content.decode()

        self.assertIn('data-testid="draft-blocker"', content)
        self.assertIn("ポストシーズン", content)
        self.assertNotIn("ドラフト会議を始める", content)


class NominateTest(DraftScreenCase):
    def setUp(self):
        super().setUp()
        self.start()
        self.client.force_login(self.owner)

    def test_the_gm_nominates_and_the_screen_moves_to_the_next_turn(self):
        before = self.view()
        chosen = next(row for row in before.prospects if row.selectable)

        response = self.client.post(
            self.act_url("nominate"),
            {"prospect": chosen.key, "expected_seq": before.last_seq, "sort": "age", "dir": "asc"},
        )

        self.assertRedirects(response, f"{self.draft_url()}?sort=age&dir=asc", msg_prefix="並べ替えの指定を保って戻る")
        self.assertGreater(self.last_seq() or 0, before.last_seq)
        after = self.view()
        mine = [move for move in after.own_moves if move.prospect_key == chosen.key]
        lost = [message for message in self.messages_of(response) if "抽選に外れました" in message]
        self.assertTrue(mine or lost, "獲得したか、抽選に外れて知らされる")

    def test_a_missing_or_broken_expected_seq_writes_nothing(self):
        """`expected_seq` の欠落は「検査なし」にせず弾く。"""
        before = self.view()
        chosen = next(row for row in before.prospects if row.selectable)
        for data in ({"prospect": chosen.key}, {"prospect": chosen.key, "expected_seq": "x"}):
            with self.subTest(data=data):
                response = self.client.post(self.act_url("nominate"), data)
                self.assertRedirects(response, self.draft_url())
                self.assertIn("画面が古いか", " ".join(self.messages_of(response)))
                self.assertEqual(self.last_seq(), before.last_seq)

    def test_a_double_submission_is_refused_as_an_old_screen(self):
        before = self.view()
        chosen = next(row for row in before.prospects if row.selectable)
        data = {"prospect": chosen.key, "expected_seq": before.last_seq}
        self.client.post(self.act_url("nominate"), data)
        written = self.last_seq()

        response = self.client.post(self.act_url("nominate"), data)

        self.assertIn("画面が古くなりました", " ".join(self.messages_of(response)))
        self.assertEqual(self.last_seq(), written)

    def test_an_action_the_meeting_does_not_accept_shows_the_reason(self):
        """成り立たない行動（候補にいない・最少に届く前の選択終了）は、理由をそのまま出して何も書かない。"""
        before = self.view()
        for action, data in (
            ("nominate", {"prospect": 10_000}),
            ("nominate", {}),
            ("pass", {}),
            ("delegate", {}),
        ):
            with self.subTest(action=action, data=data):
                response = self.client.post(self.act_url(action), {**data, "expected_seq": before.last_seq})
                self.assertRedirects(response, self.draft_url())
                self.assertTrue(self.messages_of(response))
                self.assertEqual(self.last_seq(), before.last_seq)

    def test_the_operations_follow_the_turn(self):
        """選べる候補がいる間は「この番を任せる」を出さない（候補のボタンと同時に出さない）。最少に届く前は選択終了も出さない。"""
        content = self.client.get(self.draft_url()).content.decode()

        self.assertIn('name="prospect"', content)
        self.assertNotIn(f'action="{self.act_url("delegate")}"', content)
        self.assertNotIn(f'action="{self.act_url("pass")}"', content)
        self.assertIn("入札する", content, "1巡目は入札")


class FinishTest(DraftScreenCase):
    def test_auto_completes_the_meeting_and_the_home_returns_to_the_close_card(self):
        """残りをお任せ → 会議は終了。GM ホームは締めるカードに戻り、ドラフトの行は「会議で指名済み」。"""
        self.start()
        self.client.force_login(self.owner)

        response = self.client.post(self.act_url("auto"), {"expected_seq": self.view().last_seq})

        self.assertIn("ドラフト会議が終わりました", " ".join(self.messages_of(response)))
        content = self.client.get(self.draft_url()).content.decode()
        self.assertIn('id="draft-finished"', content)
        self.assertNotIn('name="expected_seq"', content, "終わった会議には操作を出さない")
        self.assertIn('data-testid="draft-own"', content)
        home = self.client.get(self.home_url()).content.decode()
        self.assertIn('id="season-close"', home)
        self.assertIn("会議で指名済み", home)
        self.assertNotIn('id="draft-meeting"', home)

    def test_after_closing_the_year_goes_to_the_offseason(self):
        """締めた年の会議の URL はオフの結果へ。まだ来ない年は 404。"""
        self.start()
        service = build_pennant_draft_service(self.none_world)
        service.auto_complete(expected_year=YEAR, expected_seq=self.view().last_seq)
        build_pennant_offseason_service(self.none_world).close_season(expected_year=YEAR)

        self.assertRedirects(
            self.client.get(self.draft_url()), reverse("pennant_offseason", args=[self.none_world, YEAR])
        )
        self.assertEqual(self.client.get(self.draft_url(YEAR + 2)).status_code, 404)


class HomeCardTest(DraftScreenCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)

    def test_before_the_meeting_the_close_card_offers_the_meeting(self):
        """未開始は締めるカード＋ドラフトの行（お任せ・会議へ）。「すべてお任せ」のボタンは作らない（#297）。"""
        content = self.client.get(self.home_url()).content.decode()

        self.assertIn('id="season-close"', content)
        self.assertIn("お任せ（会議を開かない）", content)
        self.assertIn(f'href="{self.draft_url()}"', content)

    def test_while_the_meeting_lasts_the_home_shows_only_the_meeting_card(self):
        """途中は会議のカードだけ（締めるボタンを並べない）。"""
        self.start()

        content = self.client.get(self.home_url()).content.decode()

        self.assertIn('id="draft-meeting"', content)
        self.assertIn("あなたの番", content)
        self.assertNotIn(reverse("pennant_close_season", args=[self.none_world]), content)

    def test_others_see_no_card(self):
        self.start()
        self.client.force_login(self.other)
        content = self.client.get(self.home_url()).content.decode()
        self.assertNotIn('id="draft-meeting"', content)
        self.assertNotIn('id="season-close"', content)

    def test_a_close_refused_by_the_meeting_shows_the_reason(self):
        self.start()
        response = self.client.post(
            reverse("pennant_close_season", args=[self.none_world]), {"expected_year": YEAR}, follow=True
        )
        self.assertIn("ドラフト会議が途中です", response.content.decode())


class ChangedMeetingTest(DraftScreenCase):
    def test_a_meeting_that_cannot_be_replayed_offers_only_the_reset(self):
        """コードが変わって再生できない会議は「会議をやり直す」だけ（#297 Q5）。やり直すと始める前に戻る。"""
        self.start()
        orm_models.PennantDraft.objects.filter(world_id=self.none_world).update(signature="0" * 32)
        self.client.force_login(self.owner)

        content = self.client.get(self.draft_url()).content.decode()
        self.assertIn(f'action="{self.act_url("reset")}"', content)
        self.assertNotIn('name="prospect"', content)
        self.assertNotIn(f'action="{self.act_url("auto")}"', content)
        home = self.client.get(self.home_url()).content.decode()
        self.assertIn('id="draft-meeting"', home)
        self.assertIn("会議を続けられません", home)

        response = self.client.post(self.act_url("reset"), {"expected_seq": self.view().last_seq})

        self.assertRedirects(response, self.draft_url())
        self.assertIsNone(self.last_seq())

    def test_a_healthy_meeting_shows_no_reset(self):
        self.start()
        self.client.force_login(self.owner)
        content = self.client.get(self.draft_url()).content.decode()
        self.assertNotIn(f'action="{self.act_url("reset")}"', content)


class LockedInputsTest(DraftScreenCase):
    def test_the_release_tab_becomes_read_only_after_the_start(self):
        """会議を始めた後は、戦力外の指名を読むだけにして理由を出す（`pre_draft_input_blocker`）。"""
        self.client.force_login(self.owner)
        url = f"{reverse('pennant_club', args=[self.none_world])}?tab=release"
        self.assertIn('value="manual"', self.client.get(url).content.decode())

        self.start()
        content = self.client.get(url).content.decode()

        self.assertIn('data-testid="release-blocker"', content)
        self.assertIn("ドラフト会議が始まっている", content)
        self.assertNotIn('value="manual"', content)


class SortTest(DraftScreenCase):
    def test_the_prospects_follow_the_sort_in_the_url(self):
        """候補は既定で見込みの区分の高い順。`?sort=age&dir=asc` で若い順（並べ替えは domain の `sort_prospects`）。"""
        self.start()
        default = self.view()
        self.assertEqual(default.sort, "grade")
        grades = [row.grade.lower_bound for row in default.prospects if not row.is_taken]
        self.assertEqual(grades, sorted(grades, reverse=True))

        by_age = build_pennant_draft_service(self.none_world).view(expected_year=YEAR, sort="age", descending=False)
        ages = [row.age for row in by_age.prospects if not row.is_taken]
        self.assertEqual(ages, sorted(ages))
        response = self.client.get(f"{self.draft_url()}?sort=age&dir=asc")
        self.assertEqual(response.context["current_sort"], "age")
