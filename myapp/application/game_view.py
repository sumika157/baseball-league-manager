"""試合の参照（一覧・絞り込みの選択肢・詳細）。

試合の一覧は集約を組み立てず、リードモデル（`GameListQuery`）から直接 DTO を受け取る。
試合詳細だけは、ボックススコアと打席の記録（スコアブック）を作るために `Game` 集約を読む。
どちらも読むだけで、更新は `GameRecordingService` が受け持つ。
"""

from __future__ import annotations

from ..domain.entities import Game
from ..domain.repositories import GameRepository, TeamRepository
from ..domain.value_objects import FieldingPosition, attendance_label
from .dto import (
    GameDetail,
    GameFieldingRow,
    GameLineScore,
    GamePlayerRow,
    GameTeamBox,
    Listing,
    PlayerIndexEntry,
)
from .ids import saved_id, team_names
from .queries import GameListQuery
from .scorebook_view import build_line_score, build_scorebook_grids
from .stat_rows import decision_label, to_game_row


class GameViewService:
    """試合の一覧と詳細を読むユースケース。"""

    def __init__(
        self,
        *,
        games: GameRepository,
        teams: TeamRepository,
        game_list_query: GameListQuery,
    ) -> None:
        # 省略可能にすると、一部だけ渡した半端なサービスが作れてしまい、呼ぶ経路によって
        # 「None に find_by_id は無い」で落ちる。そのため全部を必須にしている。
        # 試合詳細は集約を読む（ボックススコア・スコアブックの材料）
        self._games = games
        # 選手の索引とチーム名を引く
        self._teams = teams
        # 一覧は集約を組み立てないリードモデルを使う
        self._game_list_query = game_list_query

    def list_games(
        self,
        *,
        year: int | None = None,
        team_id: int | None = None,
        month: int | None = None,
        league_id: int | None = None,
    ) -> Listing:
        """試合の一覧。新しい順。

        参照だけなので集約は組み立てず、リードモデルから直接 DTO を受け取る。
        """
        rows = self._game_list_query.list_rows(year=year, team_id=team_id, month=month, league_id=league_id)
        return Listing(rows=rows, sort="date", descending=True)

    def list_game_seasons(self) -> list[int]:
        return self._game_list_query.list_seasons()

    def list_game_months(
        self, *, year: int | None = None, team_id: int | None = None, league_id: int | None = None
    ) -> list[int]:
        """その絞り込みで試合がある月。一覧の切り替えに使う。"""
        return self._game_list_query.list_months(year=year, team_id=team_id, league_id=league_id)

    def latest_game_year(self) -> int | None:
        """試合一覧を開いたときに最初に見せるシーズン。

        月の既定は「その範囲で試合がある月の最後」で、選んだ月に試合が無い
        ときの落とし先と同じ規則なので、画面側の1か所にまとめてある。
        """
        return self._game_list_query.latest_year()

    def get_game_detail(self, game_id: int) -> GameDetail:
        """試合詳細。ボックススコアの形（チームごと・打順の順）で返す。"""
        game = self._games.find_by_id(game_id)
        names = team_names(self._teams)
        players = self._player_index()

        batting = []
        for entry in game.batting_in_order():
            info = players.get(entry.player_id)
            if info is None:
                continue
            line = entry.line
            batting.append(
                GamePlayerRow(
                    player_id=entry.player_id,
                    player_name=info.name,
                    number=info.number,
                    team_id=info.team_id,
                    team_name=names.get(info.team_id, ""),
                    at_bats=line.at_bats,
                    hits=line.hits,
                    home_runs=line.home_runs,
                    runs_batted_in=line.runs_batted_in,
                    walks=line.walks,
                    batting_average=line.batting_average,
                    doubles=line.doubles,
                    triples=line.triples,
                    hit_by_pitch=line.hit_by_pitch,
                    sacrifice_flies=line.sacrifice_flies,
                    runs=line.runs,
                    sacrifice_bunts=line.sacrifice_bunts,
                    stolen_bases=line.stolen_bases,
                    double_plays=line.double_plays,
                    strikeouts_batting=line.strikeouts,
                    career_batting_average=info.career_batting_average,
                    batting_order=entry.batting_order,
                    slot_sequence=entry.slot_sequence,
                    position_label=entry.position_label,
                )
            )

        # 打撃と別の名前にしておく（同じ名前を使い回すと、打撃の型に固定される）
        pitching = []
        for outing in game.pitching_in_order():
            info = players.get(outing.player_id)
            if info is None:
                continue
            pitched = outing.line
            pitching.append(
                GamePlayerRow(
                    player_id=outing.player_id,
                    player_name=info.name,
                    number=info.number,
                    team_id=info.team_id,
                    team_name=names.get(info.team_id, ""),
                    innings_pitched=str(pitched.innings),
                    runs_allowed=pitched.runs_allowed,
                    earned_runs=pitched.earned_runs,
                    strikeouts=pitched.strikeouts,
                    hits_allowed=pitched.hits_allowed,
                    walks_allowed=pitched.walks_allowed,
                    hit_by_pitch_allowed=pitched.hit_by_pitch_allowed,
                    home_runs_allowed=pitched.home_runs_allowed,
                    earned_run_average=pitched.earned_run_average,
                    career_earned_run_average=info.career_earned_run_average,
                    appearance_order=outing.appearance_order,
                    decision=decision_label(pitched),
                )
            )

        fielding = self._to_fielding_rows(game, players)
        times = game.times

        return GameDetail(
            game=to_game_row(game, names),
            batting=batting,
            pitching=pitching,
            line_score=self._to_line_score(game, batting),
            away_box=self._to_team_box(game.away_team_id, names, batting, pitching, fielding, game),
            home_box=self._to_team_box(game.home_team_id, names, batting, pitching, fielding, game),
            scorebook=build_scorebook_grids(
                game,
                names,
                lambda player_id: players[player_id].name if player_id in players else f"選手{player_id}",
            ),
            start_label=times.start_text if times else "",
            end_label=times.end_label if times else "",
            duration_label=times.duration_label if times else "",
            interruption_label=times.interruption_label if times else "",
            attendance_label=attendance_label(game.attendance) if game.attendance is not None else "",
        )

    @staticmethod
    def _to_fielding_rows(game: Game, players: dict[int, PlayerIndexEntry]) -> list[GameFieldingRow]:
        """守備成績の行。打順の順に並べ、続けて打順にいない投手（指名打者制）を登板順に並べる。

        位置はラインアップの守備位置。打順にいない投手は「投」。守備成績は打席から導いた値で、
        打席の記録が無い試合では行が無い。
        """
        by_player = {entry.player_id: entry.line for entry in game.fielding}
        lineup = {entry.player_id: entry for entry in game.batting}
        pitched = {outing.player_id for outing in game.pitching}

        ordered = [entry.player_id for entry in game.batting_in_order() if entry.player_id in by_player]
        ordered += [outing.player_id for outing in game.pitching_in_order() if outing.player_id not in ordered]
        # 打席にも登板にも現れない守備者（失策だけが記録された選手など）は最後に
        ordered += sorted(set(by_player) - set(ordered))

        rows = []
        for player_id in ordered:
            info = players.get(player_id)
            if info is None or player_id not in by_player:
                continue
            batting = lineup.get(player_id)
            position = batting.fielding_position if batting else None
            if position is not None and position.takes_the_field:
                label = position.label
            else:
                label = FieldingPosition.PITCHER.label if player_id in pitched else ""
            line = by_player[player_id]
            rows.append(
                GameFieldingRow(
                    player_id=player_id,
                    player_name=info.name,
                    number=info.number,
                    team_id=info.team_id,
                    position_label=label,
                    putouts=line.putouts,
                    assists=line.assists,
                    errors=line.errors,
                )
            )
        return rows

    @staticmethod
    def _to_team_box(
        team_id: int,
        names: dict[int, str],
        batting: list[GamePlayerRow],
        pitching: list[GamePlayerRow],
        fielding: list[GameFieldingRow],
        game: Game,
    ) -> GameTeamBox | None:
        """1チームぶんのボックススコア。並びは既に打順・登板順になっている。"""
        rows = [row for row in batting if row.team_id == team_id]
        staff = [row for row in pitching if row.team_id == team_id]
        if not rows and not staff:
            return None
        return GameTeamBox(
            team_id=team_id,
            team_name=names.get(team_id, ""),
            score=(game.home_score if team_id == game.home_team_id else game.away_score),
            batting=rows,
            pitching=staff,
            fielding=[row for row in fielding if row.team_id == team_id],
        )

    @staticmethod
    def _to_line_score(game: Game, batting: list[GamePlayerRow]) -> GameLineScore | None:
        """スコアボード。回ごとの得点が記録されていなければ出さない。"""
        return build_line_score(game, lambda team_id: sum(row.hits for row in batting if row.team_id == team_id))

    def _player_index(self) -> dict[int, PlayerIndexEntry]:
        """選手 id から名前・背番号・所属チーム・通算の率を引ける索引。"""
        index: dict[int, PlayerIndexEntry] = {}
        for team in self._teams.find_all_with_roster():
            for player in team.players:
                index[saved_id(player.id)] = PlayerIndexEntry(
                    name=player.name,
                    number=player.number.value,
                    team_id=saved_id(team.id),
                    # ボックススコアに並べる参考値。1試合の率は読めないため通算を出す
                    career_batting_average=player.batting.batting_average,
                    career_earned_run_average=player.pitching.earned_run_average,
                )
        return index
