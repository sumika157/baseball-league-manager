"""集約・成績の値を画面向けの行（DTO）に詰め替える関数。

複数の画面のユースケースが同じ行を作るため、サービスのクラスから出してある。
業務ルールは持たず、値オブジェクトの指標を DTO のフィールドに写すだけ。
"""

from __future__ import annotations

from datetime import date

from ..domain.entities import Game, Player
from ..domain.value_objects import PitchingLine, TeamRecord
from .dto import BatterRow, GameRow, PitcherRow
from .league_context import EMPTY_LEAGUE_CONTEXT, LeagueContext


def compact_record_label(record: TeamRecord) -> str:
    """勝敗を「3-1-1」（勝-敗-分）の1行で表す。

    対戦成績はチーム数ぶんの列が並ぶため、「3勝1敗1分」では表が横に伸びる。
    引分が0でも省かない。列によって桁数が変わると読み違えるため。
    """
    return f"{record.wins}-{record.losses}-{record.ties}"


def decision_label(line: PitchingLine) -> str:
    """その試合で投手に付いた記録。ボックススコアの「勝」「S」の印。

    1試合では勝利・敗戦・セーブは1つずつしか付かないので、先に見つかった
    ものを返す。ホールドは勝敗と両立しない（勝利投手にホールドは付かない）。
    """
    for count, label in (
        (line.wins, "勝"),
        (line.losses, "敗"),
        (line.saves, "Ｓ"),
        (line.holds, "Ｈ"),
    ):
        if count:
            return label
    return ""


def to_game_row(game: Game, names: dict[int, str]) -> GameRow:
    assert game.id is not None, "一覧に載る試合は保存済み"
    return GameRow(
        id=game.id,
        year=game.season.year,
        played_on=game.played_on,
        home_team_id=game.home_team_id,
        home_team_name=names.get(game.home_team_id, ""),
        away_team_id=game.away_team_id,
        away_team_name=names.get(game.away_team_id, ""),
        home_score=game.home_score,
        away_score=game.away_score,
        # 未記録の試合は 0-0 でも引分ではないので、勝者も付けない
        winner_team_id=game.winner_team_id if game.is_recorded else None,
        is_recorded=game.is_recorded,
    )


def to_batter_row(
    player: Player,
    *,
    is_captain: bool = False,
    is_developmental: bool = False,
    played: int = 0,
    league_context: LeagueContext = EMPTY_LEAGUE_CONTEXT,
    today: date,
) -> BatterRow:
    """`played` は出場試合数（成績の値オブジェクトに無いので別に渡す）。"""
    assert player.id is not None, "一覧に載る選手は保存済み"
    line = player.batting
    profile = player.profile
    return BatterRow(
        id=player.id,
        name=player.name,
        number=player.number.value,
        position=player.position.label,
        at_bats=line.at_bats,
        hits=line.hits,
        doubles=line.doubles,
        triples=line.triples,
        home_runs=line.home_runs,
        runs_batted_in=line.runs_batted_in,
        batting_average=line.batting_average,
        on_base_percentage=line.on_base_percentage,
        ops=line.ops,
        games=played,
        isolated_power=line.isolated_power,
        walks=line.walks,
        sacrifice_flies=line.sacrifice_flies,
        slugging_percentage=line.slugging_percentage,
        ops_plus=line.ops_plus(league_context.average_ops),
        is_captain=is_captain,
        is_developmental=is_developmental,
        is_foreign_player=profile.is_foreign_player,
        throws_bats=profile.throws_bats,
        height_cm=profile.height_cm,
        weight_kg=profile.weight_kg,
        age=profile.age_or_none(today),
    )


def to_pitcher_row(
    player: Player,
    *,
    is_captain: bool = False,
    is_developmental: bool = False,
    played: int = 0,
    league_context: LeagueContext = EMPTY_LEAGUE_CONTEXT,
    today: date,
) -> PitcherRow:
    """`played` は登板数（成績の値オブジェクトに無いので別に渡す）。"""
    assert player.id is not None, "一覧に載る選手は保存済み"
    line = player.pitching
    profile = player.profile
    return PitcherRow(
        id=player.id,
        name=player.name,
        number=player.number.value,
        position=player.position.label,
        innings_pitched=str(line.innings),
        wins=line.wins,
        losses=line.losses,
        strikeouts=line.strikeouts,
        earned_run_average=line.earned_run_average,
        whip=line.whip,
        strikeouts_per_nine=line.strikeouts_per_nine,
        appearances=played,
        walks_per_nine=line.walks_per_nine,
        fip=line.fip(league_context.fip_constant),
        era_plus=line.era_plus(league_context.average_era),
        saves=line.saves,
        holds=line.holds,
        hold_points=line.hold_points,
        starts=line.starts,
        home_runs_allowed=line.home_runs_allowed,
        hit_by_pitch_allowed=line.hit_by_pitch_allowed,
        is_captain=is_captain,
        is_developmental=is_developmental,
        is_foreign_player=profile.is_foreign_player,
        throws_bats=profile.throws_bats,
        height_cm=profile.height_cm,
        weight_kg=profile.weight_kg,
        age=profile.age_or_none(today),
    )
