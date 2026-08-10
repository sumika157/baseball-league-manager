"""結合テストの下ごしらえ。

成績は試合の記録から集計されるため、テストでも「試合を1件作って成績を入れる」
という手順を踏む。その定型をここにまとめる。
"""

import json
from datetime import date

from django.contrib.auth.models import User
from django.urls import reverse

from myapp.application.services import TeamApplicationService
from myapp.domain.entities import Game
from myapp.domain.value_objects import BattingLine, PitchingLine, Season
from myapp.infrastructure.queries import DjangoGameListQuery, DjangoTeamListQuery
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
        game_list_query=DjangoGameListQuery(),
    )


def api_inning_rows(away=(), home=(), *, total=12) -> list[dict]:
    """試合編集 API（JSON）のイニングスコアぶんの行データ。

    away / home に値が無い回は None（未入力）で送る。イニングスコアから
    導出できるものは入力させない方針のため、テストでも欄の意味は変えない。
    """
    return [
        {
            "inning": index + 1,
            "away": away[index] if index < len(away) else None,
            "home": home[index] if index < len(home) else None,
        }
        for index in range(total)
    ]


def post_game_update(client, game_id, payload):
    """試合編集の保存 API（api_game_update）に JSON で POST する。"""
    return client.post(
        reverse("api_game_update", args=[game_id]),
        data=json.dumps(payload),
        content_type="application/json",
    )


def login_as_manager(client, *teams, username="manager") -> User:
    """渡したチームの担当者としてログインする。

    書き込みはログインだけでは通らず、対象チームの担当者であることが要る。
    ログインさせるテストの大半はこの形になる。
    """
    user = User.objects.create_user(username=username, password="x")
    for team in teams:
        team.managers.add(user)
    client.force_login(user)
    return user


def play_game(
    home_team,
    away_team,
    *,
    home_score=1,
    away_score=0,
    year=2026,
    month=4,
    day=1,
    batting=None,
    pitching=None,
) -> Game:
    """試合を1件作って保存する。

    batting / pitching は {選手id: ライン} の辞書。
    月別成績のように試合日をずらしたい場合は month も指定する。
    """
    game = Game(
        season=Season(year),
        played_on=date(year, month, day),
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
    return play_game(home_team, away_team, year=year, day=day, batting={player_id: line})


def give_pitching(home_team, away_team, player_id, line: PitchingLine, *, year=2026, day=1):
    """ある選手に投球成績を持たせるためだけの試合を作る。"""
    return play_game(home_team, away_team, year=year, day=day, pitching={player_id: line})
