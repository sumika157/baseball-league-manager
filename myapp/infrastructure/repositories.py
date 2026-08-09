"""リポジトリの Django ORM 実装。

ORM モデルとドメインオブジェクトの相互変換（マッピング）もここで行う。
ドメイン層はこのモジュールを知らない。
"""

from __future__ import annotations

from django.db import transaction

from ..domain.entities import League, Player, Team
from ..domain.exceptions import TeamNotFound
from ..domain.value_objects import (
    BattingLine,
    InningsPitched,
    JerseyNumber,
    PitchingLine,
    Position,
)
from . import orm_models


class DjangoTeamRepository:
    """TeamRepository の Django ORM 実装。"""

    def find_by_id(self, team_id: int) -> Team:
        try:
            row = (
                orm_models.Team.objects
                .select_related('league')
                .prefetch_related('players__stats', 'players__pitcher_stats')
                .get(id=team_id)
            )
        except orm_models.Team.DoesNotExist:
            raise TeamNotFound(f"チームが見つかりません（id={team_id}）。") from None

        return self._to_domain(row, with_roster=True)

    def find_all(self) -> list[Team]:
        rows = orm_models.Team.objects.select_related('league').order_by('name')
        return [self._to_domain(row, with_roster=False) for row in rows]

    def find_all_with_roster(self) -> list[Team]:
        rows = (
            orm_models.Team.objects
            .select_related('league')
            .prefetch_related('players__stats', 'players__pitcher_stats')
            .order_by('name')
        )
        return [self._to_domain(row, with_roster=True) for row in rows]

    def exists_with_name(self, league_id: int, name: str) -> bool:
        return orm_models.Team.objects.filter(league_id=league_id, name=name).exists()

    @transaction.atomic
    def save(self, team: Team) -> Team:
        """集約（チーム＋ロスター＋成績）をまとめて永続化する。

        選手の削除は現在ユースケースに無いため扱わない。
        """
        team_row, _ = orm_models.Team.objects.update_or_create(
            id=team.id,
            defaults={
                'league_id': team.league_id,
                'name': team.name,
                'city': team.city,
            },
        )
        team.id = team_row.id

        for player in team.players:
            self._save_player(team_row, player)

        return team

    # --- 内部処理 ---

    def _save_player(self, team_row: orm_models.Team, player: Player) -> None:
        player_row, _ = orm_models.Player.objects.update_or_create(
            id=player.id,
            defaults={
                'team': team_row,
                'name': player.name,
                'number': player.number.value,
                'position': player.position.value,
                'is_active': player.is_active,
            },
        )
        player.id = player_row.id

        batting = player.batting
        orm_models.PlayerStats.objects.update_or_create(
            player=player_row,
            defaults={
                'at_bats': batting.at_bats,
                'singles': batting.singles,
                'doubles': batting.doubles,
                'triples': batting.triples,
                'home_runs': batting.home_runs,
                'runs_batted_in': batting.runs_batted_in,
                'walks': batting.walks,
                'hit_by_pitch': batting.hit_by_pitch,
                'sacrifice_flies': batting.sacrifice_flies,
            },
        )

        pitching = player.pitching
        orm_models.PitcherStats.objects.update_or_create(
            player=player_row,
            defaults={
                'innings_pitched': float(pitching.innings.to_notation()),
                'wins': pitching.wins,
                'losses': pitching.losses,
                'saves': pitching.saves,
                'earned_runs': pitching.earned_runs,
                'strikeouts': pitching.strikeouts,
                'hits_allowed': pitching.hits_allowed,
                'walks_allowed': pitching.walks_allowed,
            },
        )

    def _to_domain(self, row: orm_models.Team, *, with_roster: bool) -> Team:
        players = (
            [self._player_to_domain(p) for p in row.players.all()]
            if with_roster
            else []
        )
        return Team(
            id=row.id,
            league_id=row.league_id,
            name=row.name,
            city=row.city,
            players=players,
        )

    def _player_to_domain(self, row: orm_models.Player) -> Player:
        return Player(
            id=row.id,
            name=row.name,
            number=JerseyNumber(row.number),
            position=Position.from_label(row.position),
            is_active=row.is_active,
            batting=self._batting_to_domain(getattr(row, 'stats', None)),
            pitching=self._pitching_to_domain(getattr(row, 'pitcher_stats', None)),
        )

    @staticmethod
    def _batting_to_domain(row) -> BattingLine:
        if row is None:
            return BattingLine()
        return BattingLine(
            at_bats=row.at_bats,
            singles=row.singles,
            doubles=row.doubles,
            triples=row.triples,
            home_runs=row.home_runs,
            runs_batted_in=row.runs_batted_in,
            walks=row.walks,
            hit_by_pitch=row.hit_by_pitch,
            sacrifice_flies=row.sacrifice_flies,
        )

    @staticmethod
    def _pitching_to_domain(row) -> PitchingLine:
        if row is None:
            return PitchingLine()
        return PitchingLine(
            innings=InningsPitched.from_notation(row.innings_pitched),
            wins=row.wins,
            losses=row.losses,
            saves=row.saves,
            earned_runs=row.earned_runs,
            strikeouts=row.strikeouts,
            hits_allowed=row.hits_allowed,
            walks_allowed=row.walks_allowed,
        )


class DjangoLeagueRepository:
    """LeagueRepository の Django ORM 実装。"""

    def find_by_id(self, league_id: int) -> League:
        row = orm_models.League.objects.get(id=league_id)
        return League(id=row.id, name=row.name)

    def find_all(self) -> list[League]:
        return [
            League(id=row.id, name=row.name)
            for row in orm_models.League.objects.order_by('name')
        ]
