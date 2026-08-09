"""リポジトリの Django ORM 実装。

ORM モデルとドメインオブジェクトの相互変換（マッピング）もここで行う。
ドメイン層はこのモジュールを知らない。

選手の通算成績はテーブルに持たず、試合の明細を合計して求める。
合計は SQL の集計で行い、そこから作った BattingLine / PitchingLine に
打率や防御率の計算をさせる。式をドメインの一箇所に保つため。
"""

from __future__ import annotations

from django.db import transaction
from django.db.models import Sum

from ..domain.entities import Game, GameBatting, GamePitching, League, Player, Team
from ..domain.exceptions import GameNotFound, LeagueNotFound, TeamNotFound
from ..domain.value_objects import (
    BattingLine,
    InningsPitched,
    JerseyNumber,
    PitchingLine,
    Position,
    Season,
)
from . import orm_models

_BATTING_FIELDS = (
    'at_bats', 'singles', 'doubles', 'triples', 'home_runs',
    'runs_batted_in', 'walks', 'hit_by_pitch', 'sacrifice_flies',
)

_PITCHING_COUNTS = (
    'wins', 'losses', 'saves', 'earned_runs',
    'strikeouts', 'hits_allowed', 'walks_allowed',
)


class DjangoTeamRepository:
    """TeamRepository の Django ORM 実装。"""

    def find_by_id(self, team_id: int) -> Team:
        try:
            row = (
                orm_models.Team.objects
                .select_related('league')
                .prefetch_related('players')
                .get(id=team_id)
            )
        except orm_models.Team.DoesNotExist:
            raise TeamNotFound(f"チームが見つかりません（id={team_id}）。") from None

        return self._to_domain(row, with_roster=True)

    def find_all(self) -> list[Team]:
        rows = (
            orm_models.Team.objects
            .select_related('league')
            .order_by('display_order', 'name')
        )
        return [self._to_domain(row, with_roster=False) for row in rows]

    def find_all_with_roster(self) -> list[Team]:
        rows = (
            orm_models.Team.objects
            .select_related('league')
            .prefetch_related('players')
            .order_by('display_order', 'name')
        )
        return [self._to_domain(row, with_roster=True) for row in rows]

    def exists_with_name(self, league_id: int, name: str) -> bool:
        return orm_models.Team.objects.filter(league_id=league_id, name=name).exists()

    @transaction.atomic
    def save(self, team: Team) -> Team:
        """チームとロスターを永続化する。

        成績は試合側に持つため、ここでは書かない。
        """
        team_row, _ = orm_models.Team.objects.update_or_create(
            id=team.id,
            defaults={
                'league_id': team.league_id,
                'name': team.name,
                'city': team.city,
                'display_order': team.display_order,
            },
        )
        team.id = team_row.id

        for player in team.players:
            row, _ = orm_models.Player.objects.update_or_create(
                id=player.id,
                defaults={
                    'team': team_row,
                    'name': player.name,
                    'number': player.number.value,
                    'position': player.position.value,
                    'is_active': player.is_active,
                },
            )
            player.id = row.id

        return team

    # --- 内部処理 ---

    def _to_domain(self, row: orm_models.Team, *, with_roster: bool) -> Team:
        players = []
        if with_roster:
            player_rows = list(row.players.all())
            batting = _batting_totals([p.id for p in player_rows])
            pitching = _pitching_totals([p.id for p in player_rows])
            players = [
                Player(
                    id=p.id,
                    name=p.name,
                    number=JerseyNumber(p.number),
                    position=Position.from_label(p.position),
                    is_active=p.is_active,
                    batting=batting.get(p.id, BattingLine()),
                    pitching=pitching.get(p.id, PitchingLine()),
                )
                for p in player_rows
            ]

        return Team(
            id=row.id,
            league_id=row.league_id,
            name=row.name,
            city=row.city,
            display_order=row.display_order,
            players=players,
        )


def _batting_totals(player_ids: list[int]) -> dict[int, BattingLine]:
    """選手ごとの通算打撃成績を SQL の集計で求める。"""
    if not player_ids:
        return {}
    rows = (
        orm_models.GameBattingLine.objects
        .filter(player_id__in=player_ids)
        .values('player_id')
        .annotate(**{f: Sum(f) for f in _BATTING_FIELDS})
    )
    return {
        r['player_id']: BattingLine(**{f: r[f] or 0 for f in _BATTING_FIELDS})
        for r in rows
    }


def _pitching_totals(player_ids: list[int]) -> dict[int, PitchingLine]:
    """選手ごとの通算投球成績を求める。

    投球回だけは 5.2 が「5回と2/3」を意味する特殊な表記のため、単純な合計では
    正しくない（5.2 + 5.2 は 10.4 ではなく 11.1）。明細を取り出して
    InningsPitched に足し合わせさせる。
    """
    if not player_ids:
        return {}

    counts = (
        orm_models.GamePitchingLine.objects
        .filter(player_id__in=player_ids)
        .values('player_id')
        .annotate(**{f: Sum(f) for f in _PITCHING_COUNTS})
    )
    innings: dict[int, InningsPitched] = {}
    for player_id, notation in (
        orm_models.GamePitchingLine.objects
        .filter(player_id__in=player_ids)
        .values_list('player_id', 'innings_pitched')
    ):
        innings[player_id] = innings.get(player_id, InningsPitched.zero()) + \
            InningsPitched.from_notation(notation)

    return {
        r['player_id']: PitchingLine(
            innings=innings.get(r['player_id'], InningsPitched.zero()),
            **{f: r[f] or 0 for f in _PITCHING_COUNTS},
        )
        for r in counts
    }


class DjangoGameRepository:
    """試合（Game 集約）の永続化。"""

    def find_by_id(self, game_id: int) -> Game:
        try:
            row = (
                orm_models.Game.objects
                .prefetch_related('batting_lines', 'pitching_lines')
                .get(id=game_id)
            )
        except orm_models.Game.DoesNotExist:
            raise GameNotFound(f"試合が見つかりません（id={game_id}）。") from None
        return self._to_domain(row)

    def find_all(self, year: int | None = None) -> list[Game]:
        rows = orm_models.Game.objects.prefetch_related('batting_lines', 'pitching_lines')
        if year is not None:
            rows = rows.filter(year=year)
        return [self._to_domain(row) for row in rows]

    def find_by_team(self, team_id: int, year: int | None = None) -> list[Game]:
        from django.db.models import Q

        rows = (
            orm_models.Game.objects
            .filter(Q(home_team_id=team_id) | Q(away_team_id=team_id))
            .prefetch_related('batting_lines', 'pitching_lines')
        )
        if year is not None:
            rows = rows.filter(year=year)
        return [self._to_domain(row) for row in rows]

    @transaction.atomic
    def save(self, game: Game) -> Game:
        row, _ = orm_models.Game.objects.update_or_create(
            id=game.id,
            defaults={
                'year': game.season.year,
                'played_on': game.played_on,
                'home_team_id': game.home_team_id,
                'away_team_id': game.away_team_id,
                'home_score': game.home_score,
                'away_score': game.away_score,
            },
        )
        game.id = row.id

        for entry in game.batting:
            line_row, _ = orm_models.GameBattingLine.objects.update_or_create(
                game=row,
                player_id=entry.player_id,
                defaults={f: getattr(entry.line, f) for f in _BATTING_FIELDS},
            )
            entry.id = line_row.id

        for entry in game.pitching:
            defaults = {f: getattr(entry.line, f) for f in _PITCHING_COUNTS}
            defaults['innings_pitched'] = float(entry.line.innings.to_notation())
            line_row, _ = orm_models.GamePitchingLine.objects.update_or_create(
                game=row, player_id=entry.player_id, defaults=defaults
            )
            entry.id = line_row.id

        return game

    @staticmethod
    def _to_domain(row: orm_models.Game) -> Game:
        game = Game(
            id=row.id,
            season=Season(row.year),
            played_on=row.played_on,
            home_team_id=row.home_team_id,
            away_team_id=row.away_team_id,
            home_score=row.home_score,
            away_score=row.away_score,
        )
        game.batting = [
            GameBatting(
                id=b.id,
                player_id=b.player_id,
                line=BattingLine(**{f: getattr(b, f) for f in _BATTING_FIELDS}),
            )
            for b in row.batting_lines.all()
        ]
        game.pitching = [
            GamePitching(
                id=p.id,
                player_id=p.player_id,
                line=PitchingLine(
                    innings=InningsPitched.from_notation(p.innings_pitched),
                    **{f: getattr(p, f) for f in _PITCHING_COUNTS},
                ),
            )
            for p in row.pitching_lines.all()
        ]
        return game


class DjangoLeagueRepository:
    """LeagueRepository の Django ORM 実装。"""

    def find_by_id(self, league_id: int) -> League:
        try:
            row = orm_models.League.objects.get(id=league_id)
        except orm_models.League.DoesNotExist:
            raise LeagueNotFound(f"リーグが見つかりません（id={league_id}）。") from None
        return League(id=row.id, name=row.name)

    def find_all(self) -> list[League]:
        return [
            League(id=row.id, name=row.name)
            for row in orm_models.League.objects.order_by('name')
        ]
