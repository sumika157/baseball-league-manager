"""結合テストの下ごしらえ。

成績は試合の記録から集計されるため、テストでも「試合を1件作って成績を入れる」
という手順を踏む。その定型をここにまとめる。
"""

import itertools
import json
from datetime import date

from django.contrib.auth.models import User
from django.urls import reverse

from myapp.domain.entities import Game, PlateAppearance, RunnerAdvance
from myapp.domain.pennant.world import WorldScope
from myapp.domain.simulation.manager import MIN_ACTIVE_PITCHERS
from myapp.domain.value_objects import (
    AdvanceReason,
    Base,
    BattingLine,
    JerseyNumber,
    PitchingLine,
    PlateAppearanceResult,
    Position,
    Season,
)
from myapp.domain.workspace import KEY_LENGTH, Visibility
from myapp.infrastructure import orm_models
from myapp.infrastructure.queries import DjangoWorldSummaryQuery
from myapp.infrastructure.repositories import (
    DjangoGameRepository,
    DjangoLeagueRepository,
    DjangoTeamRepository,
    DjangoWorldRepository,
)

# テストも画面と同じ組み立て（presentation/views.py）を使い、ここから再輸出する。
# テスト専用の組み立てを別に持つと、依存が食い違ってもテストでは気づけない
from myapp.presentation.views import build_recording_service as build_recording_service
from myapp.presentation.views import build_roster_service as build_roster_service
from myapp.presentation.views import build_service as build_service
from myapp.presentation.views import build_team_analysis_service as build_team_analysis_service

LINEUP_SIZE = 9

# 既定のワークスペース（バックフィルが作る「移したデータ」の入れ物に当たる）の key。10文字の英小文字と数字
DEFAULT_WORKSPACE_KEY = "defaultws0"
_workspace_serial = itertools.count(1)


def make_workspace(owner=None, name="テストワークスペース", *, visibility=Visibility.PUBLIC, key=None):
    """ワークスペースを1つ作る。owner を省くと運営者のユーザーも作る。

    key を省くと呼ぶたびに別の key（ドメインの形: 10文字の英小文字と数字）になる。
    公開範囲はドメインに既定が無いので、ここで明示する（既定は公開。バックフィルと同じ）。
    """
    if owner is None:
        owner = User.objects.create_user(username=f"operator{next(_workspace_serial)}", password="x")
    if key is None:
        key = f"ws{next(_workspace_serial):0{KEY_LENGTH - 2}d}"
    return orm_models.Workspace.objects.create(name=name, owner=owner, key=key, visibility=visibility.value)


def default_workspace() -> orm_models.Workspace:
    """テストの既定のワークスペース（公開）。無ければ作る。`BaseCase` は `setUpTestData` で先に作っておく。"""
    existing = orm_models.Workspace.objects.filter(key=DEFAULT_WORKSPACE_KEY).first()
    if existing is not None:
        return existing
    owner, _ = User.objects.get_or_create(username="default-operator")
    return make_workspace(owner, "既定のワークスペース", key=DEFAULT_WORKSPACE_KEY)


def real_scope(workspace: orm_models.Workspace | None = None) -> WorldScope:
    """実データの範囲。テストが組み立て口やリポジトリに渡す範囲はここで作る。

    workspace を省くと既定のワークスペース（`BaseCase` が最初に作るので id が最小＝今の URL が解決するもの）。
    """
    return WorldScope.real((workspace or default_workspace()).pk)


def world_ledger(workspace: orm_models.Workspace | None = None) -> DjangoWorldRepository:
    """世界の台帳（ワークスペースで絞る）。workspace を省くと既定のワークスペース。"""
    return DjangoWorldRepository((workspace or default_workspace()).pk)


def world_summary_query(workspace: orm_models.Workspace | None = None) -> DjangoWorldSummaryQuery:
    """世界の見出しの台帳（ワークスペースで絞る）。workspace を省くと既定のワークスペース。"""
    return DjangoWorldSummaryQuery((workspace or default_workspace()).pk)


def world_scope(world_id: int) -> WorldScope:
    """ペナントの世界ひとつの範囲。ワークスペースは世界の行から取る（無い世界の id なら既定のワークスペース）。"""
    workspace_id = orm_models.PennantWorld.objects.filter(id=world_id).values_list("workspace_id", flat=True).first()
    return WorldScope.pennant(workspace_id if workspace_id is not None else default_workspace().pk, world_id)


def make_league(name, *, workspace=None, **fields) -> orm_models.League:
    """リーグを作る。実データのリーグは workspace に属す（省けば既定のワークスペース）。

    ペナントのリーグ（world / world_id を渡す）は世界経由で属すので workspace を持たない
    （workspace と world はどちらか一方だけ、という制約に合わせる）。
    """
    if fields.get("world") is not None or fields.get("world_id") is not None:
        assert workspace is None, "ペナントのリーグはワークスペースを持たない（世界経由で属す）"
        return orm_models.League.objects.create(name=name, **fields)
    return orm_models.League.objects.create(name=name, workspace=workspace or default_workspace(), **fields)


def make_stadium(name, *, workspace=None, **fields) -> orm_models.Stadium:
    """球場を作る。省けば既定のワークスペースに属す。"""
    return orm_models.Stadium.objects.create(name=name, workspace=workspace or default_workspace(), **fields)


def make_pennant_world(name, *, workspace=None, **fields) -> orm_models.PennantWorld:
    """ペナントの世界を直接作る。省けば既定のワークスペースに属す。"""
    return orm_models.PennantWorld.objects.create(name=name, workspace=workspace or default_workspace(), **fields)


def register_players(team, rows) -> list[int]:
    """実データのチームに選手をまとめて登録して id を返す。rows は (名前, 背番号, 守備位置の表示名) の並び。

    `TeamApplicationService.register_player` を人数ぶん呼ぶのと同じ結果になる（集約の `add_player` が
    背番号の重複・支配下の上限を検査する）。違いは、集約の読み込みと保存を1回にまとめること。
    1人ずつ登録すると、そのたびにロスター全員（通算成績つき）を読み、保存で全員の行と比べるので、人数の2乗で遅くなる。
    登録そのものを確かめるテストは、これを使わずサービスを呼ぶ。
    """
    scope = real_scope(team.league.workspace)  # チームのワークスペースの実データ
    teams = DjangoTeamRepository(scope)
    aggregate = teams.find_by_id(team.id)
    assert aggregate.league_id is not None
    limits = DjangoLeagueRepository(scope).find_by_id(aggregate.league_id).roster_limits
    players = [
        aggregate.add_player(
            name=name,
            number=JerseyNumber(number),
            position=Position.from_label(position_label),
            limits=limits,
        )
        for name, number, position_label in rows
    ]
    teams.save(aggregate)
    ids = [player.id for player in players]
    assert all(player_id is not None for player_id in ids), "保存で id が付く"
    return [player_id for player_id in ids if player_id is not None]


def register_lineup(service, team, *, prefix, first_number=1) -> list[int]:
    """打順9人ぶんの選手を登録して id を返す。

    打順は1〜9を巡回する決まりなので、1試合を通して記録するには9人必要になる。
    `service` は使わない（呼び出し側の形を変えないため受け取る。登録は `register_players` でまとめて行う）。
    """
    return register_players(
        team, [(f"{prefix}{order}", first_number + order - 1, "内野手") for order in range(1, LINEUP_SIZE + 1)]
    )


# 試合を組める名簿に足りない分を補う選手（捕手1・内野手5・外野手3で野手9人、投手は下限ぶん）
PLAYABLE_SUPPORT = ("捕手", *("内野手",) * 5, *("外野手",) * 3, *("投手",) * MIN_ACTIVE_PITCHERS)


def register_playable_support(service, team, *, first_number=60, prefix="補充") -> None:
    """いまの選手に足して、試合を組める名簿にする（`PLAYABLE_SUPPORT` を登録する）。

    世界の作成は、試合を組めない名簿の球団を弾く。世界を作るテストが、名簿の中身とは関係なく
    世界を作れるようにする。背番号は `first_number` から連番（既存の選手と重ならない番号を渡す）。
    `service` は使わない（`register_lineup` と同じ理由）。
    """
    register_players(
        team,
        [
            (f"{prefix}{offset + 1}", first_number + offset, position)
            for offset, position in enumerate(PLAYABLE_SUPPORT)
        ],
    )


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


# 保存 API が必ず受け取る5つのキー（開始・終了・翌日・中断・観客数）の「未記録」。キーの欠落は 400 になる
GAME_DAY_UNRECORDED = {
    "start_time": "",
    "end_time": "",
    "ends_next_day": False,
    "interruption_minutes": "",
    "attendance": "",
}


def post_game_scorebook(client, game_id, payload):
    """スコアブックの保存 API（api_game_scorebook）に、payload をそのまま JSON で POST する（補わない）。"""
    return client.post(
        reverse("api_game_scorebook", args=[game_id]),
        data=json.dumps(payload),
        content_type="application/json",
    )


def post_game_scorebook_day_unrecorded(client, game_id, payload):
    """`post_game_scorebook` に、開始・終了・翌日・中断・観客数の5キーを「未記録」で補って POST する。

    試合の時刻を試さないテストが毎回5つ書かずに済むように。**補っていることが呼び出しの名前に出る**
    （キーの欠落そのものを試すテストは `post_game_scorebook` を使う）。payload に5キーがあればそちらが優先。
    """
    return post_game_scorebook(client, game_id, {**GAME_DAY_UNRECORDED, **payload})


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
    recorded=True,
    scope: WorldScope | None = None,
) -> Game:
    """試合を1件作って保存する。

    batting / pitching は {選手id: ライン} の辞書。
    月別成績のように試合日をずらしたい場合は month も指定する。

    **既定は記録済みの試合**。明細を渡さないときは、どのロスターにも属さない選手の
    空の打撃行を1つ足す（明細が1行でもあれば記録済み。順位・勝敗・試合数に数えられる）。
    登録しただけの未記録の試合を作るときは recorded=False。
    scope を渡すと、その世界の試合として保存する（ペナントの世界のチームを渡すときに要る。既定は実データ）。
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
        game.record_batting(player_id, line, team_id=_team_of(player_id, home_team, away_team))
    for player_id, line in (pitching or {}).items():
        game.record_pitching(player_id, line)
    if recorded and not game.is_recorded:
        placeholder = orm_models.Player.objects.create(name="記録用ダミー")
        game.record_batting(placeholder.id, BattingLine(), team_id=home_team.id)

    return DjangoGameRepository(scope or real_scope()).save(game)


def _team_of(player_id, home_team, away_team) -> int:
    """選手がどちらのチームに在籍しているか。成績だけを持たせるテスト用の試合に要る。"""
    stints = orm_models.PlayerStint.objects.filter(player_id=player_id)
    return home_team.id if stints.filter(team_id=home_team.id).exists() else away_team.id


def give_batting(home_team, away_team, player_id, line: BattingLine, *, year=2026, day=1):
    """ある選手に打撃成績を持たせるためだけの試合を作る。"""
    return play_game(home_team, away_team, year=year, day=day, batting={player_id: line})


def give_pitching(home_team, away_team, player_id, line: PitchingLine, *, year=2026, day=1):
    """ある選手に投球成績を持たせるためだけの試合を作る。"""
    return play_game(home_team, away_team, year=year, day=day, pitching={player_id: line})
