"""分析のテストで使う組み立て（チーム・試合・投手）。`test_analysis`・`test_analysis_splits` が使う。"""

from datetime import date

from myapp.domain.entities import Game, Player, Team
from myapp.domain.value_objects import (
    BattingLine,
    GameStage,
    InningsPitched,
    JerseyNumber,
    PitchingLine,
    Position,
    Season,
)

TIGERS, DRAGONS, SWALLOWS = 1, 2, 3


def _team(team_id, name, players=None) -> Team:
    return Team(id=team_id, name=name, league_id=1, players=players or [])


def _game(
    home,
    away,
    home_score,
    away_score,
    *,
    day=1,
    month=4,
    season=2026,
    batting=None,
    pitching=None,
) -> Game:
    game = Game(
        season=Season(season),
        played_on=date(season, month, day),
        home_team_id=home,
        away_team_id=away,
        stage=GameStage.REGULAR,
        home_score=home_score,
        away_score=away_score,
    )
    for player_id, line in (batting or {}).items():
        game.record_batting(player_id, line, team_id=game.home_team_id)
    for player_id, line in (pitching or {}).items():
        game.record_pitching(player_id, line)
    if not game.is_recorded:
        # 明細が1行でもあれば記録済み。中身は問わず、集計に数えられる試合にする
        game.record_batting(0, BattingLine(), team_id=game.home_team_id)
    return game


def _pitcher(player_id, notation="9.0", **line) -> Player:
    return Player(
        id=player_id,
        name=f"投手{player_id}",
        number=JerseyNumber(player_id),
        position=Position.PITCHER,
        pitching=PitchingLine(innings=InningsPitched.from_notation(notation), **line),
    )
