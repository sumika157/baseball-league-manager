"""スコアブック（打席の記録）の保存。

試合の経過を打席単位で受け取り、**打撃・投球・イニングスコア・得点はすべてそこから
導いて**保存する**。打撃・投球・守備の明細を直接書き換える入口は無い。
明細が打席と食い違った集約は、保存のときに集約が弾く
（`ensure_lines_match_plate_appearances`）。

打席を記録する前の古い試合（明細だけが保存されている）を、打席が空のまま保存して
成績を消してしまわないよう、`_ensure_not_wiping_legacy_lines` で弾く。

手入力として残るのは、試合日・対戦カード・ラインアップ・打席ごとの結果と走者の動きだけ。
得点・イニングスコア・登板順・登板した回・勝敗・セーブ・ホールドは導出する。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

from ..domain import services as domain_services
from ..domain.entities import Game, PlateAppearance
from ..domain.exceptions import DomainError, InvalidGame
from ..domain.repositories import GameRepository, LeagueRepository, TeamRepository
from ..domain.value_objects import FieldingPosition, Season, ensure_quota_not_exceeded
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
        self._ensure_not_wiping_legacy_lines(current, plate_appearances)

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
        self._record_fielding(game)
        team_of = self._team_of(game, lineup)
        self._apply_pitching_decisions(game, team_of)
        self._ensure_foreign_player_game_quota(game, team_of)
        return self._games.save(game)

    @staticmethod
    def _ensure_not_wiping_legacy_lines(current: Game, plate_appearances: list[PlateAppearance]) -> None:
        """打席の無い古い試合を、打席が空のまま保存して成績を消さないようにする。

        古い試合は打撃・投球の明細と得点だけが保存されていて、打席から導き直せない。
        空の打席で上書きすると、明細も得点も0に置き換わって元に戻せない。
        """
        if plate_appearances or current.plate_appearances:
            return
        recorded = current.batting or current.pitching or current.home_score or current.away_score
        if recorded:
            raise InvalidGame(
                "この試合は打席の記録がない古い形式で保存されています。"
                "打席を1つ以上入力してから保存してください（空のまま保存すると成績が消えます）。"
            )

    def _record_batting(self, game: Game, lineup: list[LineupSlot]) -> None:
        """打順の枠ごとに打撃成績を打席から数えて載せる。

        打席が回らなかった枠も0の行として残す（守備には就いているため、
        ボックススコアからは消せない）。

        途中出場の行には、いつ入ったか（`entered_sequence`）も載せる。守備位置を選手に引く
        のに要る。守備固めのように打席から導けない出場だけ、入力された半回を使う。
        """
        for slot in lineup:
            game.record_batting(
                slot.player_id,
                domain_services.batting_line_for(game.plate_appearances, slot.player_id),
                team_id=slot.team_id,
                batting_order=slot.batting_order,
                slot_sequence=slot.slot_sequence,
                fielding_position=slot.fielding_position,
                entered_sequence=self._entered_sequence(game, slot),
            )

    def _entered_sequence(self, game: Game, slot: LineupSlot) -> int | None:
        """途中出場の選手が試合に入った最初の打席の通し番号。スタメンは None（試合開始から）。

        **要るかどうかはサーバーが決める**（入力欄の有無でクライアントに決めさせない）。

        - 代打は初めて打席に立った打席、代走は代走を出した打席、投手は初めて投げた打席から導く。
          入力された値は無視する（保存のたびに変わらないように）。該当する打席が無ければ不明（None）。
        - 守備に就く途中出場（守備固めなど）は、入った半回の入力が要る。無ければ弾く。
          入力は「何回・表か裏か・その半回の何人目の打者から」で、その打席の番号に読み替える。
          代打からそのまま守備に就いた選手は、その代打の打席を指せばよい。
        - 守備に就かない行（指名打者・守備位置の未記録）は要らない。
        """
        if slot.slot_sequence == 0:
            return None

        position = slot.fielding_position
        if position is FieldingPosition.PINCH_RUNNER:
            found = [
                entry.sequence
                for entry in game.plate_appearances
                if any(sub.entering_runner_id == slot.player_id for sub in entry.substitutions)
            ]
            return min(found) if found else None
        if position is FieldingPosition.PINCH_HITTER:
            found = [entry.sequence for entry in game.plate_appearances if entry.batter_id == slot.player_id]
            return min(found) if found else None
        if position is FieldingPosition.PITCHER:
            found = [entry.sequence for entry in game.plate_appearances if entry.pitcher_id == slot.player_id]
            return min(found) if found else None
        if position is None or not position.takes_the_field:
            return None

        if slot.entered_inning is None:
            raise InvalidGame(
                f"{self._player_name(slot)}の出場した回を入力してください"
                "（代打からそのまま守備に就いた選手は、その代打の打席の回・表裏・打者番号を入力します）。"
            )
        half = sorted(
            entry.sequence
            for entry in game.plate_appearances
            if entry.inning == slot.entered_inning and entry.is_bottom == slot.entered_is_bottom
        )
        label = "裏" if slot.entered_is_bottom else "表"
        if not half:
            raise InvalidGame(
                f"{slot.entered_inning}回{label}には打席の記録がありません。{self._player_name(slot)}の"
                "出場した半回を直してください。"
            )
        if not 1 <= slot.entered_batter <= len(half):
            raise InvalidGame(
                f"{slot.entered_inning}回{label}の打者は{len(half)}人です。{self._player_name(slot)}の"
                f"出場した打者番号（{slot.entered_batter}）を直してください。"
            )
        return half[slot.entered_batter - 1]

    def _player_name(self, slot: LineupSlot) -> str:
        """エラーメッセージ用の選手名。引けなければ id。"""
        try:
            return self._teams.find_by_id(slot.team_id).find_player(slot.player_id).name
        except DomainError:
            return f"選手id={slot.player_id}"

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
    def _record_fielding(game: Game) -> None:
        """守備成績を打席から導いて載せる。打撃の枠（ラインアップ）を載せた後に呼ぶ。

        守備位置を選手に引くのにラインアップが要るため。守備に就いた選手は、
        守備機会が無くても 0 の行になる（守備の試合数を数えるため）。
        """
        domain_services.record_derived_fielding(game)

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
