"""リポジトリの往復。ORM ⇄ ドメインの変換でデータが失われないことを確認する。"""

from myapp.domain.exceptions import (
    DuplicateJerseyNumber,
    InvalidGame,
)
from myapp.domain.value_objects import (
    BattingLine,
    InningsPitched,
    JerseyNumber,
    PitchingLine,
    Position,
)
from myapp.infrastructure import orm_models
from myapp.infrastructure.repositories import (
    DjangoGameRepository,
    DjangoTeamRepository,
)

from ..helpers import (
    give_batting,
    give_pitching,
    play_game,
)
from .base import BaseCase


class RepositoryRoundTripTest(BaseCase):
    """ORM ⇄ ドメインの往復でデータが失われないこと。"""

    def setUp(self):
        super().setUp()
        self.repo = DjangoTeamRepository()

    def test_save_and_reload_a_player(self):
        team = self.repo.find_by_id(self.team.id)
        player = team.add_player("山田", JerseyNumber(10), Position.INFIELDER)
        self.repo.save(team)

        saved = self.repo.find_by_id(self.team.id).find_player(player.id)

        self.assertEqual(saved.name, "山田")
        self.assertEqual(saved.number.value, 10)
        self.assertEqual(saved.position, Position.INFIELDER)

    def test_batting_totals_come_from_games(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        give_batting(self.team, self.rival, player.id, BattingLine(at_bats=4, singles=2), day=1)
        give_batting(self.team, self.rival, player.id, BattingLine(at_bats=3, home_runs=1), day=2)

        saved = self.repo.find_by_id(self.team.id).find_player(player.id)

        self.assertEqual(saved.batting.at_bats, 7)
        self.assertEqual(saved.batting.hits, 3)

    def test_innings_are_added_as_outs_not_decimals(self):
        """5.2 + 5.2 は 10.4 ではなく 11.1。"""
        player = self.service.register_player(self.team.id, "佐藤", 18, "投手")
        line = PitchingLine(innings=InningsPitched.from_notation("5.2"), earned_runs=1)
        give_pitching(self.team, self.rival, player.id, line, day=1)
        give_pitching(self.team, self.rival, player.id, line, day=2)

        saved = self.repo.find_by_id(self.team.id).find_player(player.id)

        self.assertEqual(saved.pitching.innings.outs, 34)
        self.assertEqual(str(saved.pitching.innings), "11.1")
        self.assertEqual(saved.pitching.earned_runs, 2)

    def test_player_without_games_has_empty_stats(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        saved = self.repo.find_by_id(self.team.id).find_player(player.id)

        self.assertEqual(saved.batting.at_bats, 0)
        self.assertEqual(saved.pitching.innings.outs, 0)

    def test_duplicate_number_is_rejected_on_the_aggregate(self):
        team = self.repo.find_by_id(self.team.id)
        team.add_player("山田", JerseyNumber(10), Position.INFIELDER)
        self.repo.save(team)

        reloaded = self.repo.find_by_id(self.team.id)
        with self.assertRaises(DuplicateJerseyNumber):
            reloaded.add_player("田中", JerseyNumber(10), Position.OUTFIELDER)


class GameRepositoryTest(BaseCase):
    def test_round_trip(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        saved = play_game(
            self.team,
            self.rival,
            home_score=5,
            away_score=3,
            batting={player.id: BattingLine(at_bats=4, singles=2, runs_batted_in=1)},
        )

        reloaded = DjangoGameRepository().find_by_id(saved.id)

        self.assertEqual(reloaded.home_score, 5)
        self.assertEqual(reloaded.result_for(self.team.id), "win")
        self.assertEqual(len(reloaded.batting), 1)
        self.assertEqual(reloaded.batting[0].line.hits, 2)

    def test_same_team_is_rejected(self):
        with self.assertRaises(InvalidGame):
            play_game(self.team, self.team)

    def test_filter_by_season(self):
        play_game(self.team, self.rival, year=2025, day=1)
        play_game(self.team, self.rival, year=2026, day=1)

        self.assertEqual(len(DjangoGameRepository().find_all(2026)), 1)
        self.assertEqual(len(DjangoGameRepository().find_all()), 2)

    def test_recording_the_same_player_twice_overwrites(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        game = play_game(self.team, self.rival, batting={player.id: BattingLine(at_bats=4, singles=1)})
        game.record_batting(player.id, BattingLine(at_bats=4, home_runs=2))
        DjangoGameRepository().save(game)

        self.assertEqual(orm_models.GameBattingLine.objects.filter(game_id=game.id).count(), 1)
        reloaded = DjangoGameRepository().find_by_id(game.id)
        self.assertEqual(reloaded.batting[0].line.home_runs, 2)
