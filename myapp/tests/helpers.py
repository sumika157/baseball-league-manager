"""結合テストの下ごしらえ。

成績は試合の記録から集計されるため、テストでも「試合を1件作って成績を入れる」
という手順を踏む。その定型をここにまとめる。
"""

from datetime import date

from django.contrib.auth.models import User

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


def inning_payload(away=(), home=(), *, total=12) -> dict:
    """試合編集フォームのイニングスコアぶんの POST データ。

    勝敗・セーブ・ホールドはイニングスコアから導出されるため、画面には
    常にこの欄がある。空のまま送れば「経過を記録しない」扱いになる。
    """
    payload = {
        "innings-TOTAL_FORMS": str(total),
        "innings-INITIAL_FORMS": str(total),
        "innings-MIN_NUM_FORMS": "0",
        "innings-MAX_NUM_FORMS": "1000",
    }
    for index in range(total):
        payload[f"innings-{index}-inning"] = str(index + 1)
        if index < len(away):
            payload[f"innings-{index}-away"] = str(away[index])
        if index < len(home):
            payload[f"innings-{index}-home"] = str(home[index])
    return payload


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
