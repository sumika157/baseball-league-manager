"""世界の作成は、試合を組めない名簿の球団があるリーグを弾く（作ってから進行が止まるのを防ぐ）。

足りない名簿の世界は、日程は組めても、最初の「進める」で永久に止まる。作る前に日本語のエラーで弾き、
世界は何も残さない。規則そのものは `tests/domain/test_roster_playable.py`。
"""

from django.test import TestCase

from myapp.domain.exceptions import InvalidWorld
from myapp.infrastructure import orm_models
from myapp.presentation.views import build_pennant_world_service, build_service

from ..helpers import register_playable_support

START = 2030


class RosterCheckTest(TestCase):
    def setUp(self):
        self.league = orm_models.League.objects.create(name="検査リーグ")
        self.home = orm_models.Team.objects.create(league=self.league, name="十分な球団")
        self.short = orm_models.Team.objects.create(league=self.league, name="足りない球団")
        self.service = build_service()
        register_playable_support(self.service, self.home)

    def _create(self, **overrides):
        arguments = {
            "name": "検査",
            "owner_id": None,
            "source_league_ids": [self.league.id],
            "start_year": START,
            "seed": 1,
        } | overrides
        return build_pennant_world_service().create_world(**arguments)

    def _assert_nothing_left(self):
        self.assertEqual(orm_models.PennantWorld.objects.count(), 0)
        self.assertEqual(orm_models.League.objects.filter(world__isnull=False).count(), 0)
        self.assertEqual(orm_models.Team.objects.filter(league__world__isnull=False).count(), 0)
        self.assertEqual(orm_models.PennantPlayerRatings.objects.count(), 0)

    def test_a_league_with_a_short_roster_cannot_make_a_world(self):
        register_playable_support(self.service, self.short)
        orm_models.PlayerStint.objects.filter(team=self.short, player__position="投手").delete()

        with self.assertRaises(InvalidWorld) as raised:
            self._create()

        self.assertIn("足りない球団", str(raised.exception))
        self.assertIn("投手", str(raised.exception))
        self._assert_nothing_left()

    def test_a_team_without_players_cannot_make_a_world(self):
        with self.assertRaises(InvalidWorld) as raised:
            self._create()

        self.assertIn("足りない球団", str(raised.exception))
        self._assert_nothing_left()

    def test_the_schedule_version_is_checked_too(self):
        with self.assertRaises(InvalidWorld):
            build_pennant_world_service().create_world_with_schedule(
                name="検査",
                owner_id=None,
                source_league_ids=[self.league.id],
                start_year=START,
                seed=1,
            )

        self._assert_nothing_left()

    def test_retired_players_do_not_count(self):
        """数えるのは現在在籍の選手だけ（世界に写されるのと同じ範囲）。"""
        register_playable_support(self.service, self.short)
        pitcher = orm_models.Player.objects.filter(stints__team=self.short, position="投手").first()
        assert pitcher is not None
        self.service.retire_player(self.short.id, pitcher.id)

        with self.assertRaises(InvalidWorld):
            self._create()

        self._assert_nothing_left()

    def test_every_team_with_a_playable_roster_makes_a_world(self):
        register_playable_support(self.service, self.short)

        created = self._create()

        self.assertEqual(created.team_count, 2)
        self.assertEqual(orm_models.PennantWorld.objects.count(), 1)

    def test_too_many_foreigners_for_the_game_limit_cannot_make_a_world(self):
        """野手が9人ちょうどで出場枠を超える外国人がいると、自動のスタメンを組めず進行が止まる。"""
        register_playable_support(self.service, self.short)
        orm_models.League.objects.filter(id=self.league.id).update(foreign_player_game_limit=2)
        batters = orm_models.Player.objects.filter(stints__team=self.short).exclude(position="投手")
        orm_models.Player.objects.filter(id__in=list(batters.values_list("id", flat=True)[:3])).update(
            is_foreign_player=True
        )

        with self.assertRaises(InvalidWorld) as raised:
            self._create()

        self.assertIn("足りない球団", str(raised.exception))
        self.assertIn("出場は1試合2人まで", str(raised.exception))
        self._assert_nothing_left()

    def test_the_same_roster_passes_when_the_league_has_no_game_limit(self):
        register_playable_support(self.service, self.short)
        batters = orm_models.Player.objects.filter(stints__team=self.short).exclude(position="投手")
        orm_models.Player.objects.filter(id__in=list(batters.values_list("id", flat=True)[:3])).update(
            is_foreign_player=True
        )

        self._create()

        self.assertEqual(orm_models.PennantWorld.objects.count(), 1)

    def test_the_strictest_game_limit_of_the_chosen_leagues_applies_to_every_team(self):
        """出場枠の無いリーグの球団も、一緒に選んだリーグの厳しい枠で見る（交流戦はどのリーグとも当たる）。"""
        register_playable_support(self.service, self.short)
        strict = orm_models.League.objects.create(name="厳しいリーグ", foreign_player_game_limit=2)
        strict_team = orm_models.Team.objects.create(league=strict, name="厳しい球団")
        register_playable_support(self.service, strict_team)
        batters = orm_models.Player.objects.filter(stints__team=self.short).exclude(position="投手")
        orm_models.Player.objects.filter(id__in=list(batters.values_list("id", flat=True)[:3])).update(
            is_foreign_player=True
        )

        self._create()  # 枠の無いリーグだけなら作れる
        orm_models.PennantWorld.objects.all().delete()
        with self.assertRaises(InvalidWorld) as raised:
            self._create(source_league_ids=[self.league.id, strict.id])

        self.assertIn("足りない球団", str(raised.exception))
        self.assertIn("出場は1試合2人まで", str(raised.exception))
