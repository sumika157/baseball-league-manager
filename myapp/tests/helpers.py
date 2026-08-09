"""結合テストの下ごしらえ。

成績は試合の記録から集計されるため、テストでも「試合を1件作って成績を入れる」
という手順を踏む。その定型をここにまとめる。
"""

from datetime import date

from myapp.application.services import TeamApplicationService
from myapp.domain.entities import Game
from myapp.domain.value_objects import BattingLine, PitchingLine, Season
from myapp.infrastructure.queries import DjangoTeamListQuery
from myapp.infrastructure.repositories import (
    DjangoGameRepository,
    DjangoLeagueRepository,
    DjangoTeamRepository,
)


def build_service() -> TeamApplicationService:
    return TeamApplicationService(
        teams=DjangoTeamRepository(),
        team_list_query=DjangoTeamListQuery(),
        games=DjangoGameRepository(),
        leagues=DjangoLeagueRepository(),
    )


def play_game(
    home_team, away_team, *, home_score=1, away_score=0, year=2026, day=1,
    batting=None, pitching=None,
) -> Game:
    """試合を1件作って保存する。

    batting / pitching は {選手id: ライン} の辞書。
    """
    game = Game(
        season=Season(year),
        played_on=date(year, 4, day),
        home_team_id=home_team.id,
        away_team_id=away_team.id,
        home_score=home_score,
        away_score=away_score,
    )
    for player_id, line in (batting or {}).items():
        game.record_batting(player_id, line)
    for player_id, line in (pitching or {}).items():
        game.record_pitching(player_id, line)

    return DjangoGameRepository().save(game)


def give_batting(home_team, away_team, player_id, line: BattingLine, *, year=2026, day=1):
    """ある選手に打撃成績を持たせるためだけの試合を作る。"""
    return play_game(
        home_team, away_team, year=year, day=day, batting={player_id: line}
    )


def give_pitching(home_team, away_team, player_id, line: PitchingLine, *, year=2026, day=1):
    """ある選手に投球成績を持たせるためだけの試合を作る。"""
    return play_game(
        home_team, away_team, year=year, day=day, pitching={player_id: line}
    )
