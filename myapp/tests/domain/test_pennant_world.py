"""世界・世界の範囲・分岐の業務ルール（DB 不要・Django 非依存）。"""

import unittest

from myapp.domain.entities import League, Team
from myapp.domain.exceptions import DuplicateJerseyNumber, InvalidSeason, InvalidWorld
from myapp.domain.pennant.fork import fork_league, fork_roster, fork_team
from myapp.domain.pennant.world import MAX_SEED, World, WorldScope
from myapp.domain.value_objects import JerseyNumber, Position, Profile


class WorldScopeTest(unittest.TestCase):
    def test_real_scope_has_no_world(self):
        scope = WorldScope.real()

        self.assertTrue(scope.is_real)
        self.assertFalse(scope.is_pennant)
        self.assertIsNone(scope.world_id)

    def test_pennant_scope_points_to_a_world(self):
        scope = WorldScope.pennant(3)

        self.assertTrue(scope.is_pennant)
        self.assertFalse(scope.is_real)
        self.assertEqual(scope.world_id, 3)

    def test_scopes_compare_by_value(self):
        self.assertEqual(WorldScope.real(), WorldScope.real())
        self.assertEqual(WorldScope.pennant(3), WorldScope.pennant(3))
        self.assertNotEqual(WorldScope.pennant(3), WorldScope.pennant(4))
        self.assertNotEqual(WorldScope.real(), WorldScope.pennant(3))

    def test_a_scope_is_immutable(self):
        with self.assertRaises(AttributeError):
            WorldScope.real().world_id = 1  # type: ignore[misc]

    def test_a_scope_cannot_be_built_without_saying_which(self):
        """既定値が無い。「指定しなければ実データ」と読める形を作らない。"""
        with self.assertRaises(TypeError):
            WorldScope()  # type: ignore[call-arg]

    def test_pennant_scope_rejects_a_value_that_is_not_a_world_id(self):
        for bad in (0, -1, None, True, "3"):
            with self.subTest(world_id=bad), self.assertRaises(InvalidWorld):
                WorldScope.pennant(bad)  # type: ignore[arg-type]


class WorldTest(unittest.TestCase):
    def test_a_world_keeps_what_was_decided_at_creation(self):
        world = World(name=" 2026年ペナント ", seed=42, start_year=2026, owner_id=5)

        self.assertEqual(world.name, "2026年ペナント")
        self.assertEqual(world.seed, 42)
        self.assertEqual(world.start_year, 2026)
        self.assertEqual(world.owner_id, 5)
        self.assertIsNone(world.managed_team_id)

    def test_the_managed_team_and_owner_may_be_decided_later(self):
        world = World(name="世界", seed=0, start_year=2026)

        self.assertIsNone(world.owner_id)
        self.assertIsNone(world.managed_team_id)

    def test_a_blank_name_is_rejected(self):
        for name in ("", "   "):
            with self.subTest(name=name), self.assertRaises(InvalidWorld):
                World(name=name, seed=1, start_year=2026)

    def test_a_too_long_name_is_rejected(self):
        with self.assertRaises(InvalidWorld):
            World(name="あ" * 101, seed=1, start_year=2026)

    def test_the_seed_must_fit_the_storage(self):
        World(name="世界", seed=0, start_year=2026)
        World(name="世界", seed=MAX_SEED, start_year=2026)
        for seed in (-1, MAX_SEED + 1, True, 1.5):
            with self.subTest(seed=seed), self.assertRaises(InvalidWorld):
                World(name="世界", seed=seed, start_year=2026)  # type: ignore[arg-type]

    def test_the_start_year_must_be_a_season(self):
        with self.assertRaises(InvalidSeason):
            World(name="世界", seed=1, start_year=1800)

    def test_an_unsaved_world_has_no_scope(self):
        with self.assertRaises(InvalidWorld):
            _ = World(name="世界", seed=1, start_year=2026).scope

    def test_a_saved_world_has_its_scope(self):
        self.assertEqual(World(name="世界", seed=1, start_year=2026, id=9).scope, WorldScope.pennant(9))


def _source_team() -> Team:
    """分岐元の球団。現在の選手2人と、退団済みの選手1人（過去の在籍のみ）がいる。"""
    team = Team(name="東京", league_id=1, home_stadium_id=7, id=10, display_order=2)
    first = team.add_player("山田", JerseyNumber(1), Position.INFIELDER, from_year=2018)
    first.profile = Profile(birth_date=None, is_foreign_player=False, birthplace="東京都", debut_year=2018)
    second = team.add_player("スミス", JerseyNumber(99), Position.PITCHER, from_year=2020)
    second.profile = Profile(is_foreign_player=True, nationality="米国")
    gone = team.add_player("引退", JerseyNumber(5), Position.OUTFIELDER, from_year=2010)
    team.retire_player(gone, year=2020)
    team.appoint_captain(first, year=2024)
    return team


class ForkTest(unittest.TestCase):
    def setUp(self):
        self.source = _source_team()

    def test_fork_league_copies_the_rules_but_not_the_id(self):
        league = League(name="セ", id=3, foreign_player_roster_limit=4, foreign_player_game_limit=2, display_order=5)

        copy = fork_league(league, display_order=1)

        self.assertIsNone(copy.id)
        self.assertEqual(
            (copy.name, copy.foreign_player_roster_limit, copy.foreign_player_game_limit, copy.display_order),
            ("セ", 4, 2, 1),
        )

    def test_fork_team_copies_the_stadium_and_order_but_starts_empty(self):
        copy = fork_team(self.source, league_id=99)

        self.assertIsNone(copy.id)
        self.assertEqual(
            (copy.name, copy.league_id, copy.home_stadium_id, copy.display_order),
            ("東京", 99, 7, 2),
        )
        self.assertEqual(copy.players, [])

    def test_fork_roster_copies_only_current_players(self):
        target = fork_team(self.source, league_id=99)
        target.id = 20

        fork_roster(self.source, target, start_year=2026)

        self.assertEqual(sorted(p.name for p in target.players), ["スミス", "山田"])

    def test_the_joining_year_is_the_start_year(self):
        target = fork_team(self.source, league_id=99)
        target.id = 20

        fork_roster(self.source, target, start_year=2026)

        for player in target.players:
            stint = target.current_stint(player)
            self.assertIsNotNone(stint)
            assert stint is not None  # mypy 用
            self.assertEqual((stint.from_year, stint.to_year, stint.team_id), (2026, None, 20))
            self.assertEqual(len(player.career), 1, "過去の在籍は写さない")

    def test_number_and_position_and_profile_are_copied(self):
        target = fork_team(self.source, league_id=99)
        target.id = 20

        fork_roster(self.source, target, start_year=2026)

        by_name = {p.name: p for p in target.players}
        self.assertEqual(by_name["山田"].number, JerseyNumber(1))
        self.assertEqual(by_name["山田"].position, Position.INFIELDER)
        self.assertEqual(by_name["山田"].profile.birthplace, "東京都")
        self.assertEqual(by_name["山田"].profile.debut_year, 2018)
        self.assertEqual(by_name["スミス"].number, JerseyNumber(99))
        self.assertTrue(by_name["スミス"].profile.is_foreign_player)

    def test_captaincy_is_not_copied(self):
        target = fork_team(self.source, league_id=99)
        target.id = 20

        fork_roster(self.source, target, start_year=2026)

        self.assertTrue(all(p.captaincies == [] for p in target.players))
        self.assertIsNone(target.current_captain)

    def test_the_copy_is_independent_of_the_source(self):
        target = fork_team(self.source, league_id=99)
        target.id = 20
        fork_roster(self.source, target, start_year=2026)

        target.players[0].rename("別人")

        self.assertIn("山田", [p.name for p in self.source.players])
        self.assertNotIn("別人", [p.name for p in self.source.players])

    def test_the_aggregate_still_guards_the_jersey_number(self):
        """名簿は集約の操作で作るので、背番号の重複は集約が弾く（検査を書き直していない）。"""
        source = _source_team()
        # 分岐元の側が壊れていた場合（同じ背番号が2人）でも、写し先の集約が弾く
        source.players[1].career[0].number = JerseyNumber(1)
        target = fork_team(source, league_id=99)
        target.id = 20

        with self.assertRaises(DuplicateJerseyNumber):
            fork_roster(source, target, start_year=2026)

    def test_the_foreign_player_quota_is_not_checked(self):
        """枠を超えている分岐元も、そのまま写す（スナップショット）。"""
        source = Team(name="多国籍", league_id=1, id=11)
        for number in range(1, 8):
            player = source.add_player(f"外国人{number}", JerseyNumber(number), Position.PITCHER, from_year=2020)
            player.profile = Profile(is_foreign_player=True)
        target = fork_team(source, league_id=99)
        target.id = 21

        fork_roster(source, target, start_year=2026)

        self.assertEqual(target.foreign_player_count, 7)


if __name__ == "__main__":
    unittest.main()
