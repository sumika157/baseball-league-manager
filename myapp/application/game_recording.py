"""スコアブック（打席の記録）の保存。

試合の経過を打席単位で受け取り、**打撃・投球・イニングスコア・得点はすべてそこから
導いて**保存する。手入力の成績を受け取る `TeamApplicationService.update_game` とは
別の関心事なので、サービスを分けてある。

**同じ試合に両方を使わない。** 打席の記録がある試合は打席が出典で、明細だけを
書き換えようとすると集約が弾く（`ensure_lines_match_plate_appearances`）。

手入力として残るのは、試合日・対戦カード・ラインアップ・打席ごとの結果と走者の動きだけ。
得点・イニングスコア・登板順・登板した回・勝敗・セーブ・ホールドは導出する。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

from ..domain import services as domain_services
from ..domain.entities import Game, PlateAppearance
from ..domain.repositories import GameRepository, LeagueRepository, TeamRepository
from ..domain.value_objects import Season, ensure_quota_not_exceeded
from .dto import LineupSlot


def _saved_id(value: int | None) -> int:
    """保存済みの集約から取り出す id。永続化された後は必ず値がある。"""
    assert value is not None, "保存済みの集約には id がある"
    return value


class GameRecordingService:
    """1試合ぶんのスコアブックを受け取って保存する。"""

    def __init__(
        self,
        *,
        games: GameRepository,
        teams: TeamRepository,
        leagues: LeagueRepository,
    ) -> None:
        self._games = games
        self._teams = teams
        self._leagues = leagues

    def record_scorebook(
        self,
        game_id: int,
        *,
        year: int,
        played_on: date,
        home_team_id: int,
        away_team_id: int,
        lineup: list[LineupSlot],
        plate_appearances: list[PlateAppearance],
    ) -> Game:
        """打席の記録で試合を上書きする。成績は打席から導く。

        得点を引数で受け取らないのは、打席から導ける値だから。受け取ると
        「記録と食い違う得点」を保存できてしまう。
        """
        current = self._games.find_by_id(game_id)

        game = Game(
            id=current.id,
            season=Season(year),
            played_on=played_on,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
            plate_appearances=list(plate_appearances),
        )
        # 得点とイニングスコアは打席から導く。手入力させない
        game.line_score = game.derived_line_score()
        game.home_score = game.line_score.home_total
        game.away_score = game.line_score.away_total
        # スコアブックとして成立しているか（打順の巡回・塁の再生・得点の一致）
        game.ensure_plate_appearances_consistent()

        self._record_batting(game, lineup)
        self._record_pitching(game)
        team_of = self._team_of(game, lineup)
        self._apply_pitching_decisions(game, team_of)
        self._ensure_foreign_player_game_quota(game, team_of)
        return self._games.save(game)

    @staticmethod
    def _record_batting(game: Game, lineup: list[LineupSlot]) -> None:
        """打順の枠ごとに打撃成績を打席から数えて載せる。

        打席が回らなかった枠も0の行として残す（守備には就いているため、
        ボックススコアからは消せない）。
        """
        for slot in lineup:
            game.record_batting(
                slot.player_id,
                domain_services.batting_line_for(game.plate_appearances, slot.player_id),
                batting_order=slot.batting_order,
                slot_sequence=slot.slot_sequence,
                fielding_position=slot.fielding_position,
            )

    @staticmethod
    def _record_pitching(game: Game) -> None:
        """投球成績を打席から数えて載せる。登板順と登板した回も打席から導く。

        誰がいつ投げ始めたかは記録に書いてあるので、入力させない。**登板順は
        チームごとに1から振る**（両チームの投手をまとめて数えると、相手の先発が
        2番手になってしまう）。
        """
        first_seen: dict[int, PlateAppearance] = {}
        for entry in game.plate_appearances_in_order():
            first_seen.setdefault(entry.pitcher_id, entry)

        by_team: dict[int, list[int]] = {}
        for pitcher_id, entry in first_seen.items():
            # 表の攻撃で投げているのはホーム、裏はビジター
            team_id = game.away_team_id if entry.is_bottom else game.home_team_id
            by_team.setdefault(team_id, []).append(pitcher_id)

        for pitchers in by_team.values():
            for order, pitcher_id in enumerate(pitchers, start=1):
                game.record_pitching(
                    pitcher_id,
                    domain_services.pitching_line_for(game.plate_appearances, pitcher_id),
                    appearance_order=order,
                    entered_inning=first_seen[pitcher_id].inning,
                )

    @staticmethod
    def _team_of(game: Game, lineup: list[LineupSlot]) -> dict[int, int]:
        """選手 id → チーム id。

        **スコアブック自身が答えを持っている** — 表の攻撃で打っているのはビジター、
        投げているのはホーム。選手の索引を引き直す必要がない
        （引くと全チームのロスターと通算成績を読むことになる）。
        """
        team_of = {slot.player_id: slot.team_id for slot in lineup}
        for entry in game.plate_appearances:
            batting_team = game.home_team_id if entry.is_bottom else game.away_team_id
            fielding_team = game.away_team_id if entry.is_bottom else game.home_team_id
            team_of.setdefault(entry.batter_id, batting_team)
            team_of[entry.pitcher_id] = fielding_team
        return team_of

    @staticmethod
    def _apply_pitching_decisions(game: Game, team_of: dict[int, int]) -> None:
        """勝敗・セーブ・ホールドをドメインの規則で決め、記録に反映する。

        規則から一意に決まるものを手入力させると、記録どうしが食い違う。
        """
        if game.line_score.is_empty:
            return

        decisions = domain_services.pitching_decisions(game, team_of)
        for outing in game.pitching:
            wins = decisions.wins_for(outing.player_id)
            outing.line = replace(
                outing.line,
                wins=wins,
                losses=decisions.losses_for(outing.player_id),
                saves=decisions.saves_for(outing.player_id),
                holds=decisions.holds_for(outing.player_id),
                starts=1 if outing.appearance_order == 1 else 0,
                relief_wins=wins if outing.appearance_order > 1 else 0,
            )

    def _ensure_foreign_player_game_quota(self, game: Game, team_of: dict[int, int]) -> None:
        """出場した外国人選手がチームごとの上限を超えていないか確認する。

        ホーム・ビジターはそれぞれ独立に判定する（合算しない）。読むのは対戦する
        2チームのロスターだけ（全チームの索引を作ると通算成績まで付いてくる）。
        """
        for team_id in (game.home_team_id, game.away_team_id):
            team = self._teams.find_by_id(team_id)
            foreign_ids = {player.id for player in team.players if player.profile.is_foreign_player}
            count = sum(1 for player_id, owner in team_of.items() if owner == team_id and player_id in foreign_ids)
            limit = self._leagues.find_by_id(_saved_id(team.league_id)).foreign_player_game_limit
            ensure_quota_not_exceeded(
                count,
                limit,
                f"「{team.name}」の外国人選手出場人数（{count}人）が上限（{limit}人）を超えています。",
            )
