"""結合テストの下ごしらえ。

成績は試合の記録から集計されるため、テストでも「試合を1件作って成績を入れる」
という手順を踏む。その定型をここにまとめる。
"""

import json
from datetime import date

from django.contrib.auth.models import User
from django.urls import reverse

from myapp.domain.entities import Game, PlateAppearance, RunnerAdvance
from myapp.domain.value_objects import (
    AdvanceReason,
    Base,
    BattingLine,
    PitchingLine,
    PlateAppearanceResult,
    Season,
)
from myapp.infrastructure.repositories import DjangoGameRepository

# テストも画面と同じ組み立て（presentation/views.py）を使い、ここから再輸出する。
# テスト専用の組み立てを別に持つと、依存が食い違ってもテストでは気づけない
from myapp.presentation.views import build_recording_service as build_recording_service
from myapp.presentation.views import build_service as build_service

LINEUP_SIZE = 9


def register_lineup(service, team, *, prefix, first_number=1) -> list[int]:
    """打順9人ぶんの選手を登録して id を返す。

    打順は1〜9を巡回する決まりなので、1試合を通して記録するには9人必要になる。
    """
    return [
        service.register_player(team.id, f"{prefix}{order}", first_number + order - 1, "内野手").id
        for order in range(1, LINEUP_SIZE + 1)
    ]


def lineup_rows(team, player_ids, *, fielding_position="指") -> list[dict]:
    """スコアブック保存 API に送る打順の行。"""
    return [
        {
            "team_id": team.id,
            "player_id": player_id,
            "batting_order": order,
            "slot_sequence": 0,
            "fielding_position": fielding_position,
        }
        for order, player_id in enumerate(player_ids, start=1)
    ]


def build_scorebook(*, away, home, away_batters, home_batters, away_pitchers, home_pitchers) -> list[dict]:
    """回ごとの得点から、成立する打席の並びを組み立てる。

    **得点は本塁打、アウトは三振**で表す。走者が塁に残らないので塁の再生が単純になり、
    「何回に何点入ったか」と「誰がいつ投げたか」だけを指定すれば済む。
    勝敗・セーブ・ホールドの判定や登板順の導出を結合テストで確かめるときに使う。

    away / home は回ごとの得点のリスト。**home が away より短ければ、その回の裏は
    行われなかったことになる**（ホームがリードして9回裏を戦わない試合）。
    *_pitchers は {回: 投手id}。その回から登板する。
    """
    entries: list[dict] = []
    orders = {False: 0, True: 0}

    for index in range(max(len(away), len(home))):
        inning = index + 1
        for is_bottom in (False, True):
            runs_by_inning = home if is_bottom else away
            if inning > len(runs_by_inning):
                continue
            # 表はホームが守り、裏はビジターが守る（攻撃側の反対）
            pitcher_id = _pitcher_for(away_pitchers if is_bottom else home_pitchers, inning)
            batters = home_batters if is_bottom else away_batters
            results = ["本塁打"] * runs_by_inning[index] + ["空振り三振"] * 3
            for result in results:
                orders[is_bottom] = orders[is_bottom] % LINEUP_SIZE + 1
                order = orders[is_bottom]
                entries.append(
                    _scorebook_entry(
                        sequence=len(entries) + 1,
                        inning=inning,
                        is_bottom=is_bottom,
                        batter_id=batters[order - 1],
                        pitcher_id=pitcher_id,
                        order=order,
                        result=result,
                    )
                )
    return entries


def _pitcher_for(pitchers: dict, inning: int) -> int:
    """その回に投げている投手。指定された回のうち、最も遅く始まったもの。"""
    started = [start for start in pitchers if start <= inning]
    return pitchers[max(started)]


def _scorebook_entry(*, sequence, inning, is_bottom, batter_id, pitcher_id, order, result) -> dict:
    to_base = 4 if result == "本塁打" else -1
    reason = "打撃" if result == "本塁打" else "アウト"
    return {
        "sequence": sequence,
        "inning": inning,
        "is_bottom": is_bottom,
        "batter_id": batter_id,
        "pitcher_id": pitcher_id,
        "batting_order": order,
        "slot_sequence": 0,
        "result": result,
        "fielded_by": "",
        "advances": [
            {"runner_id": batter_id, "from_base": 0, "to_base": to_base, "reason": reason, "error_index": None}
        ],
        "errors": [],
    }


def to_plate_appearances(rows) -> list[PlateAppearance]:
    """`build_scorebook` の行をドメインの打席に直す。

    API を通さずサービスを直接呼ぶテスト用。API 経由なら `presentation/forms.py`
    が同じ変換をする。
    """
    return [
        PlateAppearance(
            sequence=row["sequence"],
            inning=row["inning"],
            is_bottom=row["is_bottom"],
            batter_id=row["batter_id"],
            pitcher_id=row["pitcher_id"],
            batting_order=row["batting_order"],
            slot_sequence=row["slot_sequence"],
            result=PlateAppearanceResult.from_label(row["result"]),
            advances=[
                RunnerAdvance(
                    runner_id=advance["runner_id"],
                    from_base=Base(advance["from_base"]),
                    to_base=Base(advance["to_base"]),
                    reason=AdvanceReason.from_label(advance["reason"]),
                )
                for advance in row["advances"]
            ],
        )
        for row in rows
    ]


def post_game_scorebook(client, game_id, payload):
    """スコアブックの保存 API（api_game_scorebook）に JSON で POST する。"""
    return client.post(
        reverse("api_game_scorebook", args=[game_id]),
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
