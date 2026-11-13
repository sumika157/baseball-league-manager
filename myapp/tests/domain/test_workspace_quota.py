"""ワークスペースの上限（`WorkspaceQuota`）と、世界の見込みの試合数・使用量の業務ルール（DB 不要・Django 非依存）。"""

import unittest

from myapp.domain.exceptions import InvalidWorkspace, WorkspaceLimitReached
from myapp.domain.pennant.schedule import ScheduleRules
from myapp.domain.pennant.world import MAX_SEASONS_PER_WORLD, WorldUsage, world_game_reserve
from myapp.domain.workspace import WorkspaceQuota


def quota(*, owned: int = 3, worlds: int = 5, games: int = 40000) -> WorkspaceQuota:
    return WorkspaceQuota(max_owned_workspaces=owned, max_pennant_worlds=worlds, max_pennant_games=games)


class WorkspaceQuotaTest(unittest.TestCase):
    def test_up_to_the_limit_can_be_created(self):
        """上限3なら、0〜2個持っている人はもう1つ作れ、3個目まで作れる。4個目は作れない。"""
        limits = quota(owned=3)
        for owned in (0, 1, 2):
            with self.subTest(owned=owned):
                self.assertTrue(limits.can_create_workspace(owned))
                limits.ensure_can_create_workspace(owned)
        for owned in (3, 4):
            with self.subTest(owned=owned):
                self.assertFalse(limits.can_create_workspace(owned))
                with self.assertRaisesRegex(WorkspaceLimitReached, "1人3つまで"):
                    limits.ensure_can_create_workspace(owned)

    def test_the_message_follows_the_limit(self):
        self.assertEqual(quota(owned=5).limit_message, "ワークスペースは1人5つまで作れます。")

    def test_zero_means_nobody_can_create(self):
        self.assertFalse(quota(owned=0).can_create_workspace(0))

    def test_every_limit_must_be_a_non_negative_int(self):
        for field in ("owned", "worlds", "games"):
            for value in (-1, True, 3.0, "3"):
                with self.subTest(field=field, value=value), self.assertRaises(InvalidWorkspace):
                    quota(**{field: value})  # type: ignore[arg-type]


class WorldGameReserveTest(unittest.TestCase):
    """見込みは「球団数 × 1球団の試合数 ÷ 2（切り上げ）× シーズン数」。期待値も定数から組み立てる。"""

    def test_the_reserve_is_derived_from_the_schedule_and_the_season_count(self):
        per_team = ScheduleRules().games_per_team
        for teams in (2, 6, 12, 48):
            with self.subTest(teams=teams):
                self.assertEqual(world_game_reserve(teams), teams * per_team // 2 * MAX_SEASONS_PER_WORLD)

    def test_an_odd_number_of_teams_rounds_up(self):
        per_team = ScheduleRules().games_per_team
        self.assertEqual(world_game_reserve(5), (5 * per_team + 1) // 2 * MAX_SEASONS_PER_WORLD)

    def test_the_npb_sized_world_reserves_8580_games(self):
        """計画の表の値（12球団 × 143試合 ÷ 2 × 10シーズン）。定数を変えたらここで気づく。"""
        self.assertEqual(world_game_reserve(12), 8580)

    def test_zero_teams_reserve_nothing_and_bad_counts_are_refused(self):
        self.assertEqual(world_game_reserve(0), 0)
        for value in (-1, True, 2.0):
            with self.subTest(value=value), self.assertRaises(ValueError):
                world_game_reserve(value)  # type: ignore[arg-type]


class WorldUsageTest(unittest.TestCase):
    def test_usage_counts_worlds_and_sums_their_reserves(self):
        usage = WorldUsage.of([6, 12, 0])

        self.assertEqual(usage.world_count, 3, "球団の無い世界も1つと数える")
        self.assertEqual(usage.reserved_games, world_game_reserve(6) + world_game_reserve(12))

    def test_no_worlds_is_empty(self):
        self.assertEqual(WorldUsage.of([]), WorldUsage(world_count=0, reserved_games=0))


class PennantQuotaTest(unittest.TestCase):
    """世界の数と試合の枠の境界（上限ちょうど・+1・0）。"""

    def test_worlds_can_be_added_below_the_limit_only(self):
        limits = quota(worlds=2)
        limits.ensure_can_add_world(WorldUsage(world_count=1, reserved_games=0))
        for count in (2, 3):
            with self.subTest(count=count):
                usage = WorldUsage(world_count=count, reserved_games=0)
                self.assertFalse(limits.can_add_world(usage))
                with self.assertRaisesRegex(WorkspaceLimitReached, "このワークスペースの世界は2つまで"):
                    limits.ensure_can_add_world(usage)

    def test_zero_worlds_allowed_means_none_can_be_made(self):
        self.assertFalse(quota(worlds=0).can_add_world(WorldUsage(world_count=0, reserved_games=0)))

    def test_a_reserve_that_exactly_fills_the_remainder_fits(self):
        reserve = world_game_reserve(6)
        usage = WorldUsage(world_count=1, reserved_games=1000)
        exact = quota(games=1000 + reserve)
        exact.ensure_fits_games(usage, 6)
        self.assertEqual(exact.remaining_games(usage), reserve)

        short = quota(games=1000 + reserve - 1)
        self.assertFalse(short.fits_games(usage, 6))
        with self.assertRaises(WorkspaceLimitReached) as raised:
            short.ensure_fits_games(usage, 6)
        self.assertEqual(
            str(raised.exception),
            f"この世界には{MAX_SEASONS_PER_WORLD}シーズンで約{reserve:,}試合の枠が要りますが、"
            f"このワークスペースの残りは{reserve - 1:,}試合です。"
            "元にするリーグを減らすか、不要な世界を削除してから作ってください。",
        )

    def test_the_remainder_never_goes_below_zero(self):
        """既存の世界が上限を超えていても（上限を下げた後など）、残りは0で、0球団でなければ収まらない。"""
        usage = WorldUsage(world_count=1, reserved_games=50000)
        limits = quota(games=40000)

        self.assertEqual(limits.remaining_games(usage), 0)
        self.assertTrue(limits.fits_games(usage, 0))
        self.assertFalse(limits.fits_games(usage, 2))

    def test_the_blocker_explains_why_the_form_is_hidden(self):
        limits = quota(worlds=2, games=world_game_reserve(12))
        empty = WorldUsage(world_count=0, reserved_games=0)

        self.assertIsNone(limits.world_creation_blocker(empty, [6, 12]))
        self.assertIsNone(limits.world_creation_blocker(empty, []), "候補が無いときはフォームの側で案内する")
        self.assertEqual(
            limits.world_creation_blocker(WorldUsage(world_count=2, reserved_games=0), [6]),
            limits.world_limit_message,
            "世界の数が上限なら、試合の枠より先にその理由",
        )
        one_npb = WorldUsage(world_count=1, reserved_games=world_game_reserve(12))
        blocker = limits.world_creation_blocker(one_npb, [6, 12])
        self.assertIsNotNone(blocker)
        self.assertIn("収まるリーグがありません", blocker or "")
        self.assertIsNone(
            limits.world_creation_blocker(WorldUsage(world_count=1, reserved_games=0), [48, 6]),
            "どれか1つでも収まれば、フォームを出す",
        )
