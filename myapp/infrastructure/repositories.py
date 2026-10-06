"""リポジトリの Django ORM 実装。

ORM モデルとドメインオブジェクトの相互変換（マッピング）もここで行う。
ドメイン層はこのモジュールを知らない。

選手の通算成績はテーブルに持たず、試合の明細を合計して求める。
合計は SQL の集計で行い、そこから作った BattingLine / PitchingLine に
打率や防御率の計算をさせる。式をドメインの一箇所に保つため。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from django.db import models, transaction
from django.db.models import Count, Max, Min, Prefetch, Q, QuerySet, Sum

from ..domain.entities import (
    Captaincy,
    FieldingError,
    Game,
    GameBatting,
    GameFielding,
    GamePitching,
    League,
    PlateAppearance,
    Player,
    RunnerAdvance,
    RunnerSubstitution,
    Stint,
    Team,
)
from ..domain.exceptions import (
    GameNotFound,
    InvalidGame,
    InvalidPosition,
    InvalidRatings,
    InvalidSchedule,
    InvalidWorld,
    LeagueNotFound,
    PlayerNotFound,
    TeamNotFound,
    WorldNotFound,
)
from ..domain.pennant.club_plan import ClubPlan, LineupChoice
from ..domain.pennant.ratings import PlayerRatings
from ..domain.pennant.schedule import Fixture
from ..domain.pennant.world import World, WorldScope
from ..domain.services import ensure_lines_match_plate_appearances
from ..domain.simulation.ratings import BatterRatings, GrowthType, PitcherRatings
from ..domain.value_objects import (
    AdvanceReason,
    Base,
    BattingLine,
    ErrorKind,
    FieldingLine,
    FieldingPosition,
    Handedness,
    InningsPitched,
    JerseyNumber,
    LineScore,
    PitchingLine,
    PlateAppearanceResult,
    Position,
    Profile,
    Season,
)
from . import orm_models
from .scoping import (
    club_plans_in,
    fixtures_in,
    games_in,
    leagues_in,
    players_in,
    ratings_in,
    stints_in,
    teams_in,
    world_condition,
)

_BATTING_FIELDS = (
    "at_bats",
    "singles",
    "doubles",
    "triples",
    "home_runs",
    "runs_batted_in",
    "walks",
    "hit_by_pitch",
    "sacrifice_flies",
    "runs",
    "strikeouts",
    "sacrifice_bunts",
    "intentional_walks",
    "stolen_bases",
    "caught_stealing",
    "double_plays",
)

_FIELDING_FIELDS = (
    "putouts",
    "assists",
    "errors",
    "double_plays_turned",
)

_PITCHING_COUNTS = (
    "wins",
    "losses",
    "saves",
    "runs_allowed",
    "earned_runs",
    "strikeouts",
    "hits_allowed",
    "walks_allowed",
    "home_runs_allowed",
    "hit_by_pitch_allowed",
    "holds",
)

# 先発登板数と救援勝利は行に持たず、登板順から導く。同じ事実を2つ持たないため
# （登板順1なら先発、2以上での勝利は救援勝利）。
_DERIVED_PITCHING_COUNTS = ("starts", "relief_wins")


@dataclass(frozen=True)
class _RosterData:
    """ロスターを組み立てるのに要る、選手単位の材料をまとめたもの。

    在籍・履歴・通算成績をチームごとに引くと、**チーム数ぶんのクエリ**になる
    （48チームで約290クエリ・応答4.5秒だった）。選手idをまとめて渡して
    一度に読み、各チームはそこから自分の選手を取り出す。
    """

    players: dict[int, orm_models.Player]
    batting: dict[int, BattingLine]
    pitching: dict[int, PitchingLine]
    careers: dict[int, list[Stint]]
    captaincies: dict[int, list[Captaincy]]

    @classmethod
    def for_players(cls, rows: Iterable[orm_models.Player]) -> _RosterData:
        players = {row.id: row for row in rows}
        player_ids = list(players)
        return cls(
            players=players,
            batting=batting_totals(player_ids),
            pitching=pitching_totals(player_ids),
            careers=_careers_of(player_ids),
            captaincies=_captaincies_of(player_ids),
        )


class DjangoTeamRepository:
    """TeamRepository の Django ORM 実装。

    **範囲（`WorldScope`）を必須で受け取り**、読み出しはすべて SQL でその世界のチームに
    絞る。範囲の外の id は「見つからない」になる。
    """

    def __init__(self, scope: WorldScope) -> None:
        self._scope = scope

    def find_by_id(self, team_id: int) -> Team:
        try:
            row = (
                teams_in(self._scope)
                .select_related("league")
                .prefetch_related(self._stints_prefetch())
                .get(id=team_id)
            )
        except orm_models.Team.DoesNotExist:
            raise TeamNotFound(f"チームが見つかりません（id={team_id}）。") from None

        return self._to_domain(row, roster=_RosterData.for_players(s.player for s in row.stints.all()))

    def roster_signature(self) -> tuple[int, int, int]:
        """範囲の在籍の目印（在籍の行数・現在在籍の人数・最大の id）。クエリ1本。

        選手の加入・退団・新しい選手の登録があれば変わる。外で読んだ名簿が、書く前に変わっていないかの確認に使う。
        """
        values = stints_in(self._scope).aggregate(
            total=Count("id"), active=Count("id", filter=Q(to_year__isnull=True)), newest=Max("id")
        )
        return (values["total"], values["active"], values["newest"] or 0)

    def find_all(self) -> list[Team]:
        rows = teams_in(self._scope).select_related("league").order_by("display_order", "name")
        return [self._to_domain(row) for row in rows]

    def find_all_with_roster(self) -> list[Team]:
        return self._with_rosters(self._roster_rows())

    def find_by_league_with_roster(self, league_id: int) -> list[Team]:
        """そのリーグのチームだけを、ロスター込みで読む。**絞り込みは SQL 側で行う**。

        全チームを読んでから Python で絞ると、使わないリーグの選手の通算成績まで
        組み立てることになる（6チームのリーグ平均のために48チーム読んでいた）。
        """
        return self._with_rosters(self._roster_rows().filter(league_id=league_id))

    def _roster_rows(self) -> QuerySet[orm_models.Team]:
        return (
            teams_in(self._scope)
            .select_related("league")
            .prefetch_related(self._stints_prefetch())
            .order_by("display_order", "name")
        )

    def _with_rosters(self, rows: QuerySet[orm_models.Team]) -> list[Team]:
        """渡したチーム全部のロスターを、選手をまとめて1度に読んで組み立てる。"""
        team_rows = list(rows)
        # チームごとに引くとチーム数ぶんのクエリになる（_RosterData の説明を参照）
        roster = _RosterData.for_players(s.player for row in team_rows for s in row.stints.all())
        return [self._to_domain(row, roster=roster) for row in team_rows]

    @staticmethod
    def _stints_prefetch() -> Prefetch:
        """在籍と選手を1クエリのJOINで取得する。

        単純に prefetch_related('stints__player') とすると、選手側の取得が
        「id = 1 OR id = 2 OR ...」という選手数ぶんのOR連結クエリになり、
        全チーム分をまとめて読む find_all_with_roster() では選手数が
        1000人を超えたあたりでSQLiteの式木の深さ上限に達してエラーになる。
        select_related で同じクエリのJOINにまとめることで回避する。
        """
        return Prefetch("stints", queryset=orm_models.PlayerStint.objects.select_related("player"))

    @transaction.atomic
    def save(self, team: Team) -> Team:
        """チームとロスターを永続化する。

        成績は試合側に持つため、ここでは書かない。
        範囲の外のリーグ・チームには書けない（別の世界の球団を書き換えない）。
        """
        if team.league_id is None or not leagues_in(self._scope).filter(id=team.league_id).exists():
            raise LeagueNotFound(f"リーグが見つかりません（id={team.league_id}）。")
        if team.id is not None and not teams_in(self._scope).filter(id=team.id).exists():
            raise TeamNotFound(f"チームが見つかりません（id={team.id}）。")

        # id=None（未保存）なら update_or_create が新規作成に落ちる。この使い方は
        # 型スタブで表現できないため、このファイルの id 検索は ignore で明示する
        team_row, _ = orm_models.Team.objects.update_or_create(  # type: ignore[misc]
            id=team.id,
            defaults={
                "league_id": team.league_id,
                "name": team.name,
                "home_stadium_id": team.home_stadium_id,
                "display_order": team.display_order,
            },
        )
        team.id = team_row.id

        for player in team.players:
            row, _ = orm_models.Player.objects.update_or_create(  # type: ignore[misc]
                id=player.id,
                defaults={
                    "name": player.name,
                    "position": player.position.value,
                    **_profile_defaults(player.profile),
                },
            )
            player.id = row.id

            # 在籍が所属と背番号の出典。選手側には持たせない
            for stint in player.career:
                stint_row, _ = orm_models.PlayerStint.objects.update_or_create(  # type: ignore[misc]
                    id=stint.id,
                    defaults={
                        "player": row,
                        "team_id": stint.team_id,
                        "number": stint.number.value,
                        "from_year": stint.from_year,
                        "to_year": stint.to_year,
                    },
                )
                stint.id = stint_row.id

            for captaincy in player.captaincies:
                captaincy_row, _ = orm_models.Captaincy.objects.update_or_create(  # type: ignore[misc]
                    id=captaincy.id,
                    defaults={
                        "player": row,
                        "team_id": captaincy.team_id,
                        "from_year": captaincy.from_year,
                        "to_year": captaincy.to_year,
                    },
                )
                captaincy.id = captaincy_row.id

        return team

    # --- 内部処理 ---

    def _to_domain(self, row: orm_models.Team, *, roster: _RosterData | None = None) -> Team:
        """ORM のチーム行をドメインのチームにする。roster を渡すとロスターも組み立てる。"""
        players = []
        if roster is not None:
            # このチームに在籍したことのある選手を、在籍の情報つきで組み立てる。
            # 同じチームへの再加入で在籍が複数ある選手は1人にまとめる
            team_player_ids = dict.fromkeys(stint.player_id for stint in row.stints.all())

            for player_id in team_player_ids:
                p = roster.players[player_id]
                career = roster.careers.get(player_id, [])
                here = next((s for s in career if s.team_id == row.id and s.is_current), None)
                # 現在このチームに居ないなら、最後にこのチームに居たときの背番号
                last_here = next((s for s in career if s.team_id == row.id), None)
                stint = here or last_here
                if stint is None:
                    continue
                players.append(
                    Player(
                        id=player_id,
                        name=p.name,
                        number=stint.number,
                        position=Position.from_label(p.position),
                        is_active=here is not None,
                        profile=profile_of(p),
                        batting=roster.batting.get(player_id, BattingLine()),
                        pitching=roster.pitching.get(player_id, PitchingLine()),
                        career=career,
                        captaincies=roster.captaincies.get(player_id, []),
                    )
                )
            players.sort(key=lambda p: p.number.value)

        return Team(
            id=row.id,
            league_id=row.league_id,
            name=row.name,
            home_stadium_id=row.home_stadium_id,
            display_order=row.display_order,
            players=players,
        )


def _careers_of(player_ids: list[int]) -> dict[int, list[Stint]]:
    """選手ごとの在籍履歴。新しい順に並べる。"""
    if not player_ids:
        return {}

    careers: dict[int, list[Stint]] = {}
    rows = (
        orm_models.PlayerStint.objects.filter(player_id__in=player_ids)
        .select_related("team")
        .order_by("-from_year", "-id")
    )
    for row in rows:
        careers.setdefault(row.player_id, []).append(
            Stint(
                id=row.id,
                team_id=row.team_id,
                team_name=row.team.name,
                number=JerseyNumber(row.number),
                from_year=row.from_year,
                to_year=row.to_year,
            )
        )
    return careers


def _captaincies_of(player_ids: list[int]) -> dict[int, list[Captaincy]]:
    """選手ごとの主将在任歴。新しい順に並べる。_careers_of と同じ形。"""
    if not player_ids:
        return {}

    captaincies: dict[int, list[Captaincy]] = {}
    rows = (
        orm_models.Captaincy.objects.filter(player_id__in=player_ids)
        .select_related("team")
        .order_by("-from_year", "-id")
    )
    for row in rows:
        captaincies.setdefault(row.player_id, []).append(
            Captaincy(
                id=row.id,
                team_id=row.team_id,
                team_name=row.team.name,
                from_year=row.from_year,
                to_year=row.to_year,
            )
        )
    return captaincies


def _profile_defaults(profile: Profile) -> dict:
    return {
        "birth_date": profile.birth_date,
        "throws": profile.throws.value if profile.throws else "",
        "bats": profile.bats.value if profile.bats else "",
        "height_cm": profile.height_cm,
        "weight_kg": profile.weight_kg,
        "birthplace": profile.birthplace,
        "debut_year": profile.debut_year,
        "high_school": profile.high_school,
        "university": profile.university,
        "corporate_team": profile.corporate_team,
        "nationality": profile.nationality,
        "name_kana": profile.name_kana,
        "back_name": profile.back_name,
        "is_foreign_player": profile.is_foreign_player,
    }


def profile_of(row: orm_models.Player) -> Profile:
    return Profile(
        birth_date=row.birth_date,
        throws=Handedness.from_label(row.throws),
        bats=Handedness.from_label(row.bats),
        height_cm=row.height_cm,
        weight_kg=row.weight_kg,
        birthplace=row.birthplace,
        debut_year=row.debut_year,
        high_school=row.high_school,
        university=row.university,
        corporate_team=row.corporate_team,
        nationality=row.nationality,
        name_kana=row.name_kana,
        back_name=row.back_name,
        is_foreign_player=row.is_foreign_player,
    )


def batting_totals(player_ids: list[int] | None = None, *, games: Q | None = None) -> dict[int, BattingLine]:
    """選手ごとの打撃成績を SQL の集計で求める。

    既定は通算。`games` に試合の条件（`game__year=2026` など、明細の行から見た条件）を
    渡すと、その試合だけの成績になる（タイトルのようにシーズンで区切る用途）。
    `player_ids` が None なら全選手（IN 句に何千件も並べずに済む）。空なら何も読まない。
    """
    if player_ids is not None and not player_ids:
        return {}
    rows = orm_models.GameBattingLine.objects.all()
    if player_ids is not None:
        rows = rows.filter(player_id__in=player_ids)
    if games is not None:
        rows = rows.filter(games)
    totals = rows.values("player_id").annotate(**{f: Sum(f) for f in _BATTING_FIELDS})
    # values() の行から可変のキーで取り出すため、TypedDict の字面キー検査は効かない
    return {r["player_id"]: BattingLine(**{f: r[f] or 0 for f in _BATTING_FIELDS}) for r in totals}  # type: ignore[literal-required]


def pitching_totals(player_ids: list[int] | None = None, *, games: Q | None = None) -> dict[int, PitchingLine]:
    """選手ごとの投球成績を求める。引数の意味は `batting_totals` と同じ。

    投球回だけは 5.2 が「5回と2/3」を意味する特殊な表記のため、単純な合計では
    正しくない（5.2 + 5.2 は 10.4 ではなく 11.1）。表記ごとに集計した行を取り出して
    InningsPitched に足し合わせさせる（換算は InningsPitched だけが持つ）。

    登板1回ごとに読むと数万行を Python で足すことになる。現れる表記は数十通りなので、
    SQL で（選手, 表記）ごとに集計してから足す。先発登板数と救援勝利は登板順から導く
    項目なので、同じ集計の中で条件つきに数える。
    """
    if player_ids is not None and not player_ids:
        return {}
    rows = orm_models.GamePitchingLine.objects.all()
    if player_ids is not None:
        rows = rows.filter(player_id__in=player_ids)
    if games is not None:
        rows = rows.filter(games)

    grouped = (
        rows.values("player_id", "innings_pitched")
        .annotate(
            lines=Count("id"),
            starts=Count("id", filter=Q(appearance_order__lte=1)),
            relief_wins=Sum("wins", filter=Q(appearance_order__gt=1)),
            **{f: Sum(f) for f in _PITCHING_COUNTS},
        )
        .order_by()
    )

    innings: dict[int, InningsPitched] = {}
    sums: dict[int, dict[str, int]] = {}
    for r in grouped:
        player_id = r["player_id"]
        # 同じ表記の行が r["lines"] 件ぶんまとまっている
        innings[player_id] = innings.get(player_id, InningsPitched.zero()) + InningsPitched.from_notation(
            r["innings_pitched"]
        ).times(r["lines"])
        entry = sums.setdefault(player_id, dict.fromkeys((*_PITCHING_COUNTS, *_DERIVED_PITCHING_COUNTS), 0))
        for name in entry:
            entry[name] += r[name] or 0  # type: ignore[literal-required]

    # entry は項目名 → 値の辞書を展開するため、引数ごとの型検査は効かない
    return {player_id: PitchingLine(innings=innings[player_id], **entry) for player_id, entry in sums.items()}  # type: ignore[arg-type]


def _to_fielded_by(value: str) -> tuple[FieldingPosition, ...]:
    """「遊-二-一」の1列から打球の処理経路に戻す。空欄は経路なし。"""
    positions = (FieldingPosition.from_label(label) for label in value.split(orm_models.FIELDED_BY_SEPARATOR))
    return tuple(position for position in positions if position is not None)


def _from_fielded_by(positions: tuple[FieldingPosition, ...]) -> str:
    """打球の処理経路を1列にまとめる。読み書きで同じ形に戻ることが条件。"""
    return orm_models.FIELDED_BY_SEPARATOR.join(position.value for position in positions)


def _required_position(label: str) -> FieldingPosition:
    """守備位置を必須として読む。失策には必ず守備者の位置が付く。"""
    position = FieldingPosition.from_label(label)
    if position is None:
        raise InvalidPosition("失策には守備位置が必要です。")
    return position


def _batting_values(entry: GameBatting) -> dict[str, Any]:
    """打撃成績1行の列の値（試合と選手の指定を除く）。`save()` と `add_all()` が同じ写し方を使う。"""
    values: dict[str, Any] = {f: getattr(entry.line, f) for f in _BATTING_FIELDS}
    values.update(
        {
            "batting_order": entry.batting_order,
            "slot_sequence": entry.slot_sequence,
            "fielding_position": (entry.fielding_position.value if entry.fielding_position else ""),
            "team_id": entry.team_id,
            "entered_sequence": entry.entered_sequence,
        }
    )
    return values


def _pitching_values(entry: GamePitching) -> dict[str, Any]:
    """投球成績1行の列の値（試合と選手の指定を除く）。"""
    values: dict[str, Any] = {f: getattr(entry.line, f) for f in _PITCHING_COUNTS}
    values["innings_pitched"] = float(entry.line.innings.to_notation())
    values["appearance_order"] = entry.appearance_order
    values["entered_inning"] = entry.entered_inning
    return values


@dataclass
class _PlateRows:
    """1試合の打席と、打席に属する進塁・代走・失策の行（まだ保存しない）。

    試合の行や打席の行の主キーが決まる前に組み立てておくため、親の id（`game_id` など）は
    書く直前に入れる。打席ごとの子の行は `rows` と同じ並びで持つ。
    """

    entries: list[PlateAppearance]
    rows: list[orm_models.GamePlateAppearance]
    advances: list[list[orm_models.GameRunnerAdvance]]
    substitutions: list[list[orm_models.GameRunnerSubstitution]]
    errors: list[list[orm_models.GameFieldingError]]

    @classmethod
    def build(cls, game: Game) -> _PlateRows:
        entries = list(game.plate_appearances_in_order())
        return cls(
            entries=entries,
            rows=[
                orm_models.GamePlateAppearance(
                    sequence=entry.sequence,
                    inning=entry.inning,
                    is_bottom=entry.is_bottom,
                    batter_id=entry.batter_id,
                    pitcher_id=entry.pitcher_id,
                    batting_order=entry.batting_order,
                    slot_sequence=entry.slot_sequence,
                    result=entry.result.value,
                    fielded_by=_from_fielded_by(entry.fielded_by),
                )
                for entry in entries
            ],
            advances=[
                [
                    orm_models.GameRunnerAdvance(
                        runner_id=advance.runner_id,
                        from_base=advance.from_base.value,
                        to_base=advance.to_base.value,
                        reason=advance.reason.value,
                        error_index=advance.error_index,
                    )
                    for advance in entry.advances
                ]
                for entry in entries
            ],
            substitutions=[
                [
                    orm_models.GameRunnerSubstitution(
                        base=substitution.base.value,
                        leaving_runner_id=substitution.leaving_runner_id,
                        entering_runner_id=substitution.entering_runner_id,
                    )
                    for substitution in entry.substitutions
                ]
                for entry in entries
            ],
            errors=[
                [
                    orm_models.GameFieldingError(
                        player_id=error.player_id, position=error.position.value, kind=error.kind.value
                    )
                    for error in entry.errors
                ]
                for entry in entries
            ],
        )


@dataclass
class _NewGame:
    """新しい試合1つぶんの行（まだ保存しない）。`add_all` の書く前の仕事で組み立てる。"""

    game: Game
    row: orm_models.Game
    batting: list[orm_models.GameBattingLine]
    pitching: list[orm_models.GamePitchingLine]
    fielding: list[orm_models.GameFieldingLine]
    innings: list[orm_models.GameInningScore]
    plate: _PlateRows

    @classmethod
    def build(cls, game: Game) -> _NewGame:
        return cls(
            game=game,
            row=orm_models.Game(
                year=game.season.year,
                played_on=game.played_on,
                home_team_id=game.home_team_id,
                away_team_id=game.away_team_id,
                home_score=game.home_score,
                away_score=game.away_score,
            ),
            batting=[orm_models.GameBattingLine(player_id=e.player_id, **_batting_values(e)) for e in game.batting],
            pitching=[
                orm_models.GamePitchingLine(player_id=e.player_id, **_pitching_values(e)) for e in game.pitching
            ],
            fielding=[
                orm_models.GameFieldingLine(player_id=e.player_id, **{f: getattr(e.line, f) for f in _FIELDING_FIELDS})
                for e in game.fielding
            ],
            innings=[
                orm_models.GameInningScore(inning=inning, is_home=is_home, runs=values[inning - 1])
                for inning in range(1, game.line_score.innings + 1)
                for is_home, values in ((False, game.line_score.away), (True, game.line_score.home))
                if inning <= len(values)
            ],
            plate=_PlateRows.build(game),
        )


def _bind_game(rows: Iterable[Any], game_id: int | None) -> None:
    """組み立て済みの行に、保存で決まった試合の主キーを入れる。"""
    for row in rows:
        row.game_id = game_id


def _bind_plates(grouped: Sequence[Sequence[Any]], plate_rows: Sequence[Any]) -> list[Any]:
    """打席ごとの子の行（進塁・代走・失策）に、保存で決まった打席の主キーを入れて、1つの並びにする。"""
    children: list[Any] = []
    for plate_row, rows in zip(plate_rows, grouped, strict=True):
        for child in rows:
            child.plate_appearance_id = plate_row.pk
        children.extend(rows)
    return children


# 1回の一括書き込みにまとめる試合数。メモリと、1つの SQL に載せる行数を抑える
_ADD_ALL_CHUNK = 100


class DjangoGameRepository:
    """GameRepository の Django ORM 実装。試合（Game 集約）の永続化。

    範囲（`WorldScope`）を必須で受け取り、読み出しはすべて SQL でその世界の試合に絞る。
    """

    def __init__(self, scope: WorldScope) -> None:
        self._scope = scope

    def find_by_id(self, game_id: int) -> Game:
        try:
            row = (
                self._with_details()
                .prefetch_related(self._plate_appearance_prefetch(), "fielding_lines")
                .get(id=game_id)
            )
        except orm_models.Game.DoesNotExist:
            raise GameNotFound(f"試合が見つかりません（id={game_id}）。") from None
        return self._to_domain(row, with_plate_appearances=True)

    def find_all(self, year: int | None = None) -> list[Game]:
        rows = self._with_details()
        if year is not None:
            rows = rows.filter(year=year)
        return [self._to_domain(row) for row in rows]

    def find_by_team(self, team_id: int, year: int | None = None) -> list[Game]:
        rows = self._with_details().filter(Q(home_team_id=team_id) | Q(away_team_id=team_id))
        if year is not None:
            rows = rows.filter(year=year)
        return [self._to_domain(row) for row in rows]

    @staticmethod
    def _plate_appearance_prefetch() -> Prefetch:
        """打席を進塁・代走・失策ごとまとめて読む。

        多段の prefetch を素で書くと、SQLite では関連が1000件を超えた時点で
        OR 連結クエリになって落ちる（既知の罠）。打席を軸に1段ずつまとめて読む。
        """
        return Prefetch(
            "plate_appearances",
            queryset=orm_models.GamePlateAppearance.objects.prefetch_related("advances", "substitutions", "errors"),
        )

    def find_between_teams(self, team_ids: set[int], year: int | None = None) -> list[Game]:
        """渡したチームどうしの試合だけ。**絞り込みは SQL 側で行う**。

        全試合を読んでから Python で捨てると、使わない試合の打撃・投球の明細まで
        組み立てることになる（リーグ6チームぶんを得るために全48チームぶんの
        明細を読んでいて、4.5秒かかっていた）。
        """
        rows = self._with_details().filter(home_team_id__in=team_ids, away_team_id__in=team_ids)
        if year is not None:
            rows = rows.filter(year=year)
        return [self._to_domain(row) for row in rows]

    def _with_details(self) -> QuerySet[orm_models.Game]:
        """打撃・投球・イニングスコアの明細つきで、範囲の試合を読む。集約として扱うときに使う。

        **打席は含めない。** 1試合で約280行あり、まとめて読む用途（リーグ集計など）で
        付けると数十万行を組み立てることになる。打席が要るのは1試合を編集するときだけで、
        そこは `find_by_id` が読む。
        """
        return games_in(self._scope).prefetch_related("batting_lines", "pitching_lines", "inning_scores")

    @transaction.atomic
    def save(self, game: Game) -> Game:
        # 打撃・投球の明細は打席から導ける値だが、通算成績の集計のために保存もしている。
        # 同じ事実を2か所に持つので、食い違ったまま保存されないよう集約に照合させる
        if game.read_only:
            raise InvalidGame("参照専用に読んだ試合は保存できません（読んでいない明細が消えます）。")
        ensure_lines_match_plate_appearances(game)
        self._ensure_in_scope(game)
        row, _ = orm_models.Game.objects.update_or_create(  # type: ignore[misc]
            id=game.id,
            defaults={
                "year": game.season.year,
                "played_on": game.played_on,
                "home_team_id": game.home_team_id,
                "away_team_id": game.away_team_id,
                "home_score": game.home_score,
                "away_score": game.away_score,
            },
        )
        game.id = row.id

        for entry in game.batting:
            line_row, _ = orm_models.GameBattingLine.objects.update_or_create(
                game=row, player_id=entry.player_id, defaults=_batting_values(entry)
            )
            entry.id = line_row.id

        for pitching_entry in game.pitching:
            pitching_row, _ = orm_models.GamePitchingLine.objects.update_or_create(
                game=row, player_id=pitching_entry.player_id, defaults=_pitching_values(pitching_entry)
            )
            pitching_entry.id = pitching_row.id

        self._save_line_score(row, game)
        self._save_plate_appearances(row, game)
        self._save_fielding(row, game)

        # 集約から外された成績は削除する。上書きだけだと、いったん入力した
        # 選手を「出場していない」に戻せない
        orm_models.GameBattingLine.objects.filter(game=row).exclude(
            player_id__in=[e.player_id for e in game.batting]
        ).delete()
        orm_models.GamePitchingLine.objects.filter(game=row).exclude(
            player_id__in=[e.player_id for e in game.pitching]
        ).delete()

        return game

    def add_all(self, games: Sequence[Game]) -> None:
        """新しい試合を種類ごとの一括書き込みで保存する。検査は `save()` と同じで、書く前に全試合ぶん済ませる。"""
        with transaction.atomic():
            self.prepare_add_all(games)()

    def prepare_add_all(self, games: Sequence[Game]) -> Callable[[], None]:
        """`add_all` の、書く前の仕事（検査と行の組み立て）だけを済ませ、書き込みの関数を返す。

        検査と組み立ては CPU の仕事で、DB に触れない（範囲の確認の読み取りだけ）。呼び手が書き込み
        トランザクションの外で済ませ、返った関数だけをトランザクションの中で呼べば、書き込みロックを
        持つのは SQL を流す間だけになる。返す関数を呼ばなければ何も書かれない。
        """
        for game in games:
            if game.id is not None:
                raise InvalidGame("保存済みの試合は add_all では書けません（更新は save を使います）。")
            # `save()` と同じ照合。食い違ったまま保存されないよう、集約に確かめさせる
            ensure_lines_match_plate_appearances(game)
        self._ensure_teams_in_scope({team_id for game in games for team_id in (game.home_team_id, game.away_team_id)})

        prepared = [_NewGame.build(game) for game in games]

        def write() -> None:
            for start in range(0, len(prepared), _ADD_ALL_CHUNK):
                self._insert_new(prepared[start : start + _ADD_ALL_CHUNK])

        return write

    @classmethod
    def _insert_new(cls, items: Sequence[_NewGame]) -> None:
        """組み立て済みの試合を、試合の行から明細・イニングスコア・打席まで種類ごとに bulk_create する。"""
        rows = orm_models.Game.objects.bulk_create([item.row for item in items])
        # 主キーを返さない DB（SQLite 3.35 未満）では、行と集約を対応づけられない
        if any(row.pk is None for row in rows):
            raise RuntimeError("bulk_create が主キーを返さない DB には対応していません。")
        batting_rows: list[orm_models.GameBattingLine] = []
        pitching_rows: list[orm_models.GamePitchingLine] = []
        fielding_rows: list[orm_models.GameFieldingLine] = []
        inning_rows: list[orm_models.GameInningScore] = []
        for item, row in zip(items, rows, strict=True):
            item.game.id = row.pk
            for lines in (item.batting, item.pitching, item.fielding, item.innings):
                _bind_game(lines, row.pk)
            batting_rows.extend(item.batting)
            pitching_rows.extend(item.pitching)
            fielding_rows.extend(item.fielding)
            inning_rows.extend(item.innings)
        orm_models.GameBattingLine.objects.bulk_create(batting_rows)
        orm_models.GamePitchingLine.objects.bulk_create(pitching_rows)
        orm_models.GameFieldingLine.objects.bulk_create(fielding_rows)
        orm_models.GameInningScore.objects.bulk_create(inning_rows)
        cls._create_plate_appearances([(item.plate, row) for item, row in zip(items, rows, strict=True)])

    def _ensure_teams_in_scope(self, team_ids: set[int]) -> None:
        if teams_in(self._scope).filter(id__in=team_ids).count() != len(team_ids):
            raise TeamNotFound("試合のチームが見つかりません。")

    def _ensure_in_scope(self, game: Game) -> None:
        """範囲の外の試合・チームには書けない（別の世界の試合を書き換えない）。"""
        self._ensure_teams_in_scope({game.home_team_id, game.away_team_id})
        if game.id is not None and not games_in(self._scope).filter(id=game.id).exists():
            raise GameNotFound(f"試合が見つかりません（id={game.id}）。")

    @staticmethod
    def _save_fielding(row: orm_models.Game, game: Game) -> None:
        """守備成績を保存する。**打席を省いて読んだ集約では何もしない。**

        守備成績は打席と一緒に読む（`find_by_id`）。省いて読んだ集約の `fielding` は空だが、
        それは「守備が無い」ではない。空として扱うと、一覧のために読んだ試合を保存しただけで
        守備成績が全部消える（打席と同じ罠）。
        """
        if not game.plate_appearances_loaded:
            return

        for entry in game.fielding:
            fielding_row, _ = orm_models.GameFieldingLine.objects.update_or_create(
                game=row,
                player_id=entry.player_id,
                defaults={f: getattr(entry.line, f) for f in _FIELDING_FIELDS},
            )
            entry.id = fielding_row.id
        orm_models.GameFieldingLine.objects.filter(game=row).exclude(
            player_id__in=[entry.player_id for entry in game.fielding]
        ).delete()

    @staticmethod
    def _save_line_score(row: orm_models.Game, game: Game) -> None:
        """イニングスコアを保存する。回数が減った場合は余った行を消す。"""
        score = game.line_score
        for inning in range(1, score.innings + 1):
            for is_home in (False, True):
                values = score.home if is_home else score.away
                if inning > len(values):
                    continue
                orm_models.GameInningScore.objects.update_or_create(
                    game=row,
                    inning=inning,
                    is_home=is_home,
                    defaults={"runs": values[inning - 1]},
                )
        orm_models.GameInningScore.objects.filter(game=row, inning__gt=score.innings).delete()

    @classmethod
    def _save_plate_appearances(cls, row: orm_models.Game, game: Game) -> None:
        """打席の記録を保存する。

        **打席を省いて読んだ集約では何もしない。** 省略を「記録が無い」と解釈すると、
        一覧のために読んだ試合を保存しただけで記録済みの打席が全部消える
        （既知の罠と同じ形。エラーにならないので気づけない）。

        記録があるときは、その試合の打席をいったん消してから入れ直す。進塁・代走・失策は
        打席の中の位置で識別する（`error_index` が並び順を指す）ため、1行ずつ突き合わせても
        安定した対応が付けられない。1試合あたり6クエリで済み、行ごとの更新より速い。
        """
        if not game.plate_appearances_loaded:
            return

        orm_models.GamePlateAppearance.objects.filter(game=row).delete()
        cls._create_plate_appearances([(_PlateRows.build(game), row)])

    @staticmethod
    def _create_plate_appearances(items: Sequence[tuple[_PlateRows, orm_models.Game]]) -> None:
        """打席・進塁・代走・失策を、試合をまたいで種類ごとに bulk_create する。

        (組み立て済みの打席の行, 保存済みの試合の行) の組を渡す。保存した打席の id は集約の打席に入る。
        """
        entries: list[PlateAppearance] = []
        plate_rows: list[orm_models.GamePlateAppearance] = []
        advance_rows: list[list[orm_models.GameRunnerAdvance]] = []
        substitution_rows: list[list[orm_models.GameRunnerSubstitution]] = []
        error_rows: list[list[orm_models.GameFieldingError]] = []
        for plate, row in items:
            for plate_row in plate.rows:
                plate_row.game_id = row.pk
            entries.extend(plate.entries)
            plate_rows.extend(plate.rows)
            advance_rows.extend(plate.advances)
            substitution_rows.extend(plate.substitutions)
            error_rows.extend(plate.errors)
        if not plate_rows:
            return

        saved = orm_models.GamePlateAppearance.objects.bulk_create(plate_rows)
        if any(r.pk is None for r in saved):
            # bulk_create が主キーを返すかは DB に依存するため、返さないときは読み直して対応づける
            by_key = {
                (r.game_id, r.sequence): r
                for r in orm_models.GamePlateAppearance.objects.filter(game_id__in={row.pk for _, row in items})
            }
            saved = [by_key[(r.game_id, r.sequence)] for r in plate_rows]
        for entry, plate_row in zip(entries, saved, strict=True):
            entry.id = plate_row.pk

        orm_models.GameRunnerAdvance.objects.bulk_create(_bind_plates(advance_rows, saved))
        orm_models.GameRunnerSubstitution.objects.bulk_create(_bind_plates(substitution_rows, saved))
        orm_models.GameFieldingError.objects.bulk_create(_bind_plates(error_rows, saved))

    @staticmethod
    def _to_plate_appearances(row: orm_models.Game) -> list[PlateAppearance]:
        """行から打席の記録を組み立てる。進塁・代走・失策は打席の中の並びを保つ。"""
        return [
            PlateAppearance(
                id=pa.id,
                sequence=pa.sequence,
                inning=pa.inning,
                is_bottom=pa.is_bottom,
                batter_id=pa.batter_id,
                pitcher_id=pa.pitcher_id,
                batting_order=pa.batting_order,
                slot_sequence=pa.slot_sequence,
                result=PlateAppearanceResult.from_label(pa.result),
                fielded_by=_to_fielded_by(pa.fielded_by),
                advances=[
                    RunnerAdvance(
                        runner_id=advance.runner_id,
                        from_base=Base(advance.from_base),
                        to_base=Base(advance.to_base),
                        reason=AdvanceReason.from_label(advance.reason),
                        error_index=advance.error_index,
                    )
                    for advance in pa.advances.all()
                ],
                substitutions=[
                    RunnerSubstitution(
                        base=Base(substitution.base),
                        leaving_runner_id=substitution.leaving_runner_id,
                        entering_runner_id=substitution.entering_runner_id,
                    )
                    for substitution in pa.substitutions.all()
                ],
                errors=[
                    FieldingError(
                        player_id=error.player_id,
                        position=_required_position(error.position),
                        kind=ErrorKind.from_label(error.kind),
                    )
                    for error in pa.errors.all()
                ],
            )
            for pa in row.plate_appearances.all()
        ]

    @staticmethod
    def _to_fielding(row: orm_models.Game) -> list[GameFielding]:
        return [
            GameFielding(
                id=f.id,
                player_id=f.player_id,
                line=FieldingLine(**{name: getattr(f, name) for name in _FIELDING_FIELDS}),
            )
            for f in row.fielding_lines.all()
        ]

    @staticmethod
    def _to_line_score(row: orm_models.Game) -> LineScore:
        """行から回ごとの得点を組み立てる。抜けている回は 0 で埋める。"""
        halves: dict[bool, dict[int, int]] = {False: {}, True: {}}
        for entry in row.inning_scores.all():
            halves[entry.is_home][entry.inning] = entry.runs

        def to_tuple(values: dict[int, int]) -> tuple[int, ...]:
            if not values:
                return ()
            return tuple(values.get(i, 0) for i in range(1, max(values) + 1))

        return LineScore(away=to_tuple(halves[False]), home=to_tuple(halves[True]))

    @classmethod
    def _to_domain(cls, row: orm_models.Game, *, with_plate_appearances: bool = False) -> Game:
        """行から集約を組み立てる。

        打席を読んだかどうかは集約に持たせる。読んでいない集約をそのまま保存しても
        記録済みの打席が消えないようにするため（`_save_plate_appearances` を参照）。
        """
        game = Game(
            id=row.id,
            season=Season(row.year),
            played_on=row.played_on,
            home_team_id=row.home_team_id,
            away_team_id=row.away_team_id,
            home_score=row.home_score,
            away_score=row.away_score,
            line_score=cls._to_line_score(row),
            plate_appearances=cls._to_plate_appearances(row) if with_plate_appearances else [],
            fielding=cls._to_fielding(row) if with_plate_appearances else [],
            plate_appearances_loaded=with_plate_appearances,
        )
        game.batting = [game_batting_of(b) for b in row.batting_lines.all()]
        game.pitching = [game_pitching_of(p) for p in row.pitching_lines.all()]
        return game


def game_batting_of(row: orm_models.GameBattingLine) -> GameBatting:
    """打撃の明細の行を、試合の中の1人ぶんの打撃にする。"""
    return GameBatting(
        id=row.id,
        player_id=row.player_id,
        line=BattingLine(**{f: getattr(row, f) for f in _BATTING_FIELDS}),
        team_id=row.team_id,
        entered_sequence=row.entered_sequence,
        batting_order=row.batting_order,
        slot_sequence=row.slot_sequence,
        fielding_position=FieldingPosition.from_label(row.fielding_position),
    )


def game_pitching_of(row: orm_models.GamePitchingLine) -> GamePitching:
    """投球の明細の行を、試合の中の1人ぶんの投球にする。"""
    return GamePitching(
        id=row.id,
        player_id=row.player_id,
        line=PitchingLine(
            innings=InningsPitched.from_notation(row.innings_pitched),
            **{f: getattr(row, f) for f in _PITCHING_COUNTS},
            # 1試合の行なので、先発なら1、救援での勝利ならその勝利数
            starts=1 if row.appearance_order <= 1 else 0,
            relief_wins=row.wins if row.appearance_order > 1 else 0,
        ),
        appearance_order=row.appearance_order,
        entered_inning=row.entered_inning,
    )


class DjangoLeagueRepository:
    """LeagueRepository の Django ORM 実装。範囲（`WorldScope`）を必須で受け取る。"""

    def __init__(self, scope: WorldScope) -> None:
        self._scope = scope

    def find_by_id(self, league_id: int) -> League:
        try:
            row = leagues_in(self._scope).get(id=league_id)
        except orm_models.League.DoesNotExist:
            raise LeagueNotFound(f"リーグが見つかりません（id={league_id}）。") from None
        return self._to_domain(row)

    def find_all(self) -> list[League]:
        # 管理画面で手動設定した表示順を既定にする。順位表・ダッシュボードの
        # タブ・チーム一覧の並びが、この順に揃う
        return [self._to_domain(row) for row in leagues_in(self._scope).order_by("display_order", "name")]

    def save(self, league: League) -> League:
        """リーグを保存する。新しいリーグは範囲の世界に属する。範囲の外のリーグには書けない。"""
        if league.id is not None and not leagues_in(self._scope).filter(id=league.id).exists():
            raise LeagueNotFound(f"リーグが見つかりません（id={league.id}）。")
        row, _ = orm_models.League.objects.update_or_create(  # type: ignore[misc]
            id=league.id,
            defaults={
                "name": league.name,
                "world_id": self._scope.world_id,
                "display_order": league.display_order,
                "foreign_player_roster_limit": league.foreign_player_roster_limit,
                "foreign_player_game_limit": league.foreign_player_game_limit,
            },
        )
        league.id = row.id
        return league

    @staticmethod
    def _to_domain(row: orm_models.League) -> League:
        return League(
            id=row.id,
            name=row.name,
            foreign_player_roster_limit=row.foreign_player_roster_limit,
            foreign_player_game_limit=row.foreign_player_game_limit,
            display_order=row.display_order,
        )


# 世界に属する行を消す順（子から親へ）。(モデル, そのモデルから League へ至る道)。
# 世界の削除は Django の CASCADE の collector に任せない。打席などは1シーズンで数十万行
# あり、collector は消す行を Python に集めてしまうため。ここに挙げたモデルと、選手・球団・
# リーグ・世界は、`tests/integration/test_world_isolation.py` の分類表が「世界に属する」と
# したものと一致することを検査している（足したモデルを消し忘れると、そのテストが落ちる）。
_GAME_ROUTE = "home_team__league"
_WORLD_ROWS_CHILD_FIRST: tuple[tuple[type[models.Model], str], ...] = (
    # 能力は選手の在籍をたどって世界が決まるので、在籍を消す前に消す
    # 編成は選手を指すので、選手を消す前に消す（行から先に）
    (orm_models.PennantClubPlanEntry, "plan__team__league"),
    (orm_models.PennantClubPlan, "team__league"),
    (orm_models.PennantPlayerRatings, "player__stints__team__league"),
    (orm_models.PennantFixture, "home_team__league"),
    (orm_models.GameRunnerAdvance, f"plate_appearance__game__{_GAME_ROUTE}"),
    (orm_models.GameRunnerSubstitution, f"plate_appearance__game__{_GAME_ROUTE}"),
    (orm_models.GameFieldingError, f"plate_appearance__game__{_GAME_ROUTE}"),
    (orm_models.GamePlateAppearance, f"game__{_GAME_ROUTE}"),
    (orm_models.GameFieldingLine, f"game__{_GAME_ROUTE}"),
    (orm_models.GameBattingLine, f"game__{_GAME_ROUTE}"),
    (orm_models.GamePitchingLine, f"game__{_GAME_ROUTE}"),
    (orm_models.GameInningScore, f"game__{_GAME_ROUTE}"),
    (orm_models.Game, _GAME_ROUTE),
    (orm_models.Captaincy, "team__league"),
    (orm_models.PlayerStint, "team__league"),
)
_PLAYER_DELETE_CHUNK = 500


_RATING_COLUMNS = {
    BatterRatings: tuple(BatterRatings.LABELS),
    PitcherRatings: tuple(PitcherRatings.LABELS),
}
_RATINGS_CHUNK = 500
_FIXTURE_CHUNK = 500


class DjangoRatingsRepository:
    """RatingsRepository の Django ORM 実装。範囲（`WorldScope`）を必須で受け取る。

    能力はペナント専用で、実データの範囲には書けない。
    """

    def __init__(self, scope: WorldScope) -> None:
        self._scope = scope

    @transaction.atomic
    def add_all(self, ratings: Sequence[PlayerRatings]) -> None:
        if self._scope.is_real:
            raise InvalidWorld("能力はペナントの世界にだけ保存できます。")
        player_ids = {item.player_id for item in ratings}
        positions = dict(players_in(self._scope).filter(id__in=player_ids).values_list("id", "position"))
        if missing := sorted(player_ids - positions.keys()):
            raise PlayerNotFound(f"この世界に居ない選手の能力は保存できません（id={missing[0]}）。")
        for item in ratings:
            # 登録位置が投手の選手に投手の能力、それ以外に野手の能力。取り違えると、行はチェック制約を
            # 通ってしまい（どちらの組も埋まっていれば通る）、シミュレーションで別の項目として読まれる
            if Position.from_label(positions[item.player_id]).is_pitcher != item.is_pitcher:
                kind = "投手" if item.is_pitcher else "野手"
                raise InvalidRatings(f"登録位置と能力の種類が合いません（選手 id={item.player_id} に{kind}の能力）。")
        keys = [(item.player_id, item.year) for item in ratings]
        if len(set(keys)) != len(keys):
            raise InvalidRatings("同じ選手・同じ年の能力が重複しています。")
        years = {item.year for item in ratings}
        if saved := ratings_in(self._scope).filter(player_id__in=player_ids, year__in=years).first():
            raise InvalidRatings(f"すでに能力があります（選手 id={saved.player_id}・{saved.year}年）。")
        orm_models.PennantPlayerRatings.objects.bulk_create(
            [self._to_row(item) for item in ratings], batch_size=_RATINGS_CHUNK
        )

    def find_by_year(self, year: int) -> list[PlayerRatings]:
        rows = ratings_in(self._scope).filter(year=year).order_by("player_id")
        return [self._to_domain(row) for row in rows]

    def find_by_player(self, player_id: int) -> list[PlayerRatings]:
        rows = ratings_in(self._scope).filter(player_id=player_id).order_by("year")
        return [self._to_domain(row) for row in rows]

    def find_by_players(self, player_ids: Sequence[int], year: int) -> list[PlayerRatings]:
        rows = ratings_in(self._scope).filter(player_id__in=list(player_ids), year=year).order_by("player_id")
        return [self._to_domain(row) for row in rows]

    @staticmethod
    def _to_row(item: PlayerRatings) -> orm_models.PennantPlayerRatings:
        values = {name: getattr(item.ratings, name) for name in _RATING_COLUMNS[type(item.ratings)]}
        return orm_models.PennantPlayerRatings(
            player_id=item.player_id, year=item.year, growth=item.ratings.growth.value, **values
        )

    @staticmethod
    def _to_domain(row: orm_models.PennantPlayerRatings) -> PlayerRatings:
        growth = GrowthType(row.growth)
        ratings: BatterRatings | PitcherRatings
        if row.contact is not None:
            ratings = BatterRatings(**{name: getattr(row, name) for name in BatterRatings.LABELS}, growth=growth)
        else:
            ratings = PitcherRatings(**{name: getattr(row, name) for name in PitcherRatings.LABELS}, growth=growth)
        return PlayerRatings(player_id=row.player_id, year=row.year, ratings=ratings)


class DjangoFixtureRepository:
    """FixtureRepository の Django ORM 実装。範囲（`WorldScope`）を必須で受け取る。

    日程はペナント専用で、実データの範囲には書けない。
    """

    def __init__(self, scope: WorldScope) -> None:
        self._scope = scope

    @transaction.atomic
    def add_all(self, fixtures: Sequence[Fixture]) -> None:
        if self._scope.is_real:
            raise InvalidWorld("日程はペナントの世界にだけ保存できます。")
        team_ids = {team_id for f in fixtures for team_id in (f.home_team_id, f.visitor_team_id)}
        if teams_in(self._scope).filter(id__in=team_ids).count() != len(team_ids):
            raise TeamNotFound("日程の球団がこの世界に見つかりません。")
        keys = {self._key(f) for f in fixtures}
        if len(keys) != len(fixtures):
            raise InvalidSchedule("同じ日の同じ対戦が重複しています。")
        saved = fixtures_in(self._scope).filter(date__in={f.date for f in fixtures})
        if any(
            (day, home, visitor) in keys
            for day, home, visitor in saved.values_list("date", "home_team_id", "visitor_team_id")
        ):
            raise InvalidSchedule("同じ日の同じ対戦がすでに保存されています。")
        orm_models.PennantFixture.objects.bulk_create(
            [
                orm_models.PennantFixture(date=f.date, home_team_id=f.home_team_id, visitor_team_id=f.visitor_team_id)
                for f in fixtures
            ],
            batch_size=_FIXTURE_CHUNK,
        )

    def find_all(self) -> list[Fixture]:
        rows = (
            fixtures_in(self._scope)
            .order_by("date", "home_team_id")
            .values_list("date", "home_team_id", "visitor_team_id")
        )
        return [Fixture(date=day, home_team_id=home, visitor_team_id=visitor) for day, home, visitor in rows]

    def first_date(self, team_id: int | None = None) -> date | None:
        fixtures = fixtures_in(self._scope)
        if team_id is not None:
            fixtures = fixtures.filter(Q(home_team_id=team_id) | Q(visitor_team_id=team_id))
        return fixtures.aggregate(first=Min("date"))["first"]

    @transaction.atomic
    def remove(self, fixtures: Sequence[Fixture]) -> None:
        by_date: dict[date, list[Fixture]] = {}
        for fixture in fixtures:
            by_date.setdefault(fixture.date, []).append(fixture)
        removed = 0
        for day, items in by_date.items():
            # 1日に組まれる試合は多くても数十なので、式が大きくなりすぎない
            condition = Q()
            for fixture in items:
                condition |= Q(home_team_id=fixture.home_team_id, visitor_team_id=fixture.visitor_team_id)
            deleted, _ = fixtures_in(self._scope).filter(date=day).filter(condition).delete()
            removed += deleted
        if removed != len(fixtures):
            # 例外で、この呼び出しの削除はすべて取り消される
            raise InvalidSchedule("すでに消化された対戦が含まれています。")

    @staticmethod
    def _key(fixture: Fixture) -> tuple[date, int, int]:
        return (fixture.date, fixture.home_team_id, fixture.visitor_team_id)


class DjangoClubPlanRepository:
    """ClubPlanRepository の Django ORM 実装。範囲（`WorldScope`）を必須で受け取る。

    編成はペナント専用で、実データの範囲には書けない。他の世界の球団の編成は、読めも書けもしない。
    """

    def __init__(self, scope: WorldScope) -> None:
        self._scope = scope

    def find_all(self) -> list[ClubPlan]:
        rows = club_plans_in(self._scope).prefetch_related("entries").order_by("team_id")
        return [self._to_domain(row) for row in rows]

    def find_by_team(self, team_id: int) -> ClubPlan:
        if not teams_in(self._scope).filter(id=team_id).exists():
            raise TeamNotFound(f"球団が見つかりません（id={team_id}）。")
        row = club_plans_in(self._scope).prefetch_related("entries").filter(team_id=team_id).first()
        return ClubPlan(team_id=team_id) if row is None else self._to_domain(row)

    @transaction.atomic
    def save(self, plan: ClubPlan) -> ClubPlan:
        if self._scope.is_real:
            raise InvalidWorld("編成はペナントの世界にだけ保存できます。")
        if not teams_in(self._scope).filter(id=plan.team_id).exists():
            raise TeamNotFound(f"球団が見つかりません（id={plan.team_id}）。")
        player_ids = {c.player_id for c in plan.lineup or ()}
        player_ids |= set(plan.active_ids or ()) | set(plan.rotation or ())
        if plan.closer_id is not None:
            player_ids.add(plan.closer_id)
        known = set(players_in(self._scope).filter(id__in=player_ids).values_list("id", flat=True))
        if missing := sorted(player_ids - known):
            raise PlayerNotFound(f"この世界に居ない選手は編成に入れられません（id={missing[0]}）。")

        if plan.is_empty:
            club_plans_in(self._scope).filter(team_id=plan.team_id).delete()
            return plan
        row, _ = orm_models.PennantClubPlan.objects.update_or_create(  # type: ignore[misc]
            team_id=plan.team_id, defaults={"closer_id": plan.closer_id}
        )
        row.entries.all().delete()
        entries = orm_models.PennantClubPlanEntry
        rows = [
            entries(plan=row, section=entries.ACTIVE, order=order, player_id=player_id)
            for order, player_id in enumerate(plan.active_ids or (), start=1)
        ]
        rows += [
            entries(
                plan=row,
                section=entries.LINEUP,
                order=order,
                player_id=choice.player_id,
                fielding_position=choice.position.value,
            )
            for order, choice in enumerate(plan.lineup or (), start=1)
        ]
        rows += [
            entries(plan=row, section=entries.ROTATION, order=order, player_id=player_id)
            for order, player_id in enumerate(plan.rotation or (), start=1)
        ]
        entries.objects.bulk_create(rows)
        return plan

    @staticmethod
    def _to_domain(row: orm_models.PennantClubPlan) -> ClubPlan:
        entries = sorted(row.entries.all(), key=lambda e: (e.section, e.order))
        section = orm_models.PennantClubPlanEntry
        active = tuple(e.player_id for e in entries if e.section == section.ACTIVE)
        lineup = tuple(
            LineupChoice(e.player_id, FieldingPosition(e.fielding_position))
            for e in entries
            if e.section == section.LINEUP
        )
        rotation = tuple(e.player_id for e in entries if e.section == section.ROTATION)
        # 区画の行が1つも無ければ自動
        return ClubPlan(
            team_id=row.team_id,
            active_ids=active or None,
            lineup=lineup or None,
            rotation=rotation or None,
            closer_id=row.closer_id,
        )


class DjangoWorldRepository:
    """WorldRepository の Django ORM 実装。世界の台帳で、範囲は持たない。"""

    def find_by_id(self, world_id: int) -> World:
        try:
            row = orm_models.PennantWorld.objects.get(id=world_id)
        except orm_models.PennantWorld.DoesNotExist:
            raise WorldNotFound(f"世界が見つかりません（id={world_id}）。") from None
        return self._to_domain(row)

    def find_all(self) -> list[World]:
        return [self._to_domain(row) for row in orm_models.PennantWorld.objects.all()]

    def count_by_owner(self, owner_id: int) -> int:
        return orm_models.PennantWorld.objects.filter(owner_id=owner_id).count()

    def save(self, world: World) -> World:
        row, _ = orm_models.PennantWorld.objects.update_or_create(  # type: ignore[misc]
            id=world.id,
            defaults={
                "name": world.name,
                "owner_id": world.owner_id,
                "seed": world.seed,
                "managed_team_id": world.managed_team_id,
                "start_year": world.start_year,
            },
        )
        world.id = row.id
        return world

    @transaction.atomic
    def delete(self, world_id: int) -> None:
        """世界と、属する行をすべて消す。子のテーブルから、範囲で絞って順に消す。"""
        if not orm_models.PennantWorld.objects.filter(id=world_id).exists():
            raise WorldNotFound(f"世界が見つかりません（id={world_id}）。")
        scope = WorldScope.pennant(world_id)

        # 選手は在籍をたどって世界が決まるので、在籍を消す前に控えておく
        player_ids = list(players_in(scope).values_list("id", flat=True))

        # 孫から順に消し終えているので、CASCADE や PROTECT の検査に頼らず行だけを消せる
        # （_raw_delete は collector を通さない。Django の非公開 API だが、数十万行を
        # Python に集めずに済ませる手段が他に無い）
        for model, route in _WORLD_ROWS_CHILD_FIRST:
            manager = model._default_manager
            manager.filter(world_condition(route, scope))._raw_delete(manager.db)
        for start in range(0, len(player_ids), _PLAYER_DELETE_CHUNK):
            chunk = player_ids[start : start + _PLAYER_DELETE_CHUNK]
            orm_models.Player.objects.filter(id__in=chunk)._raw_delete(orm_models.Player.objects.db)

        # 球団・リーグ・世界は数が少ないので、通常の削除でよい（担当者や受け持ちの参照も外れる）
        teams_in(scope).delete()
        leagues_in(scope).delete()
        orm_models.PennantWorld.objects.filter(id=world_id).delete()

    @staticmethod
    def _to_domain(row: orm_models.PennantWorld) -> World:
        return World(
            id=row.id,
            name=row.name,
            seed=row.seed,
            start_year=row.start_year,
            owner_id=row.owner_id,
            managed_team_id=row.managed_team_id,
        )
