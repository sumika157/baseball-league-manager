"""スコアブック（打席の記録）の保存。

試合の経過を打席単位で受け取り、**打撃・投球・守備・イニングスコア・得点はすべてそこから
導いて**保存する。組み立ては domain の `assemble_game()` が行い、ここは入力の読み替え（出場した回）と
他の集約を読む検査（外国人の出場枠）と保存だけを受け持つ。打撃・投球・守備の明細を直接書き換える入口は無い。
明細が打席と食い違った集約は、保存のときに集約が弾く
（`ensure_lines_match_plate_appearances`）。

保存済みの記録（打席、または打席を記録する前の古い試合の明細）を、打席が空のまま
保存して消してしまわないよう、`_ensure_not_wiping_recorded_game` で弾く。

手入力として残るのは、試合日・対戦カード・ラインアップ・打席ごとの結果と走者の動きだけ。
得点・イニングスコア・登板順・登板した回・勝敗・セーブ・ホールドは導出する。
"""

from __future__ import annotations

from datetime import date

from ..domain import services as domain_services
from ..domain.entities import Game, PlateAppearance
from ..domain.exceptions import DomainError, InvalidGame
from ..domain.repositories import GameRepository, LeagueRepository, TeamRepository
from ..domain.value_objects import FieldingPosition, GameHeader, LineupEntry, Season, ensure_quota_not_exceeded
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
        self._ensure_not_wiping_recorded_game(current, plate_appearances)

        header = GameHeader(
            id=current.id,
            season=Season(year),
            played_on=played_on,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
        )
        entries = [
            LineupEntry(
                team_id=slot.team_id,
                player_id=slot.player_id,
                batting_order=slot.batting_order,
                slot_sequence=slot.slot_sequence,
                fielding_position=slot.fielding_position,
                entered_sequence=self._entered_sequence(plate_appearances, slot),
            )
            for slot in lineup
        ]

        # 得点・イニングスコア・打撃・投球・守備・勝敗は、ドメインが打席から導く
        game = domain_services.assemble_game(header, entries, plate_appearances)
        self._ensure_foreign_player_game_quota(
            game, domain_services.team_of_players(header, entries, game.plate_appearances)
        )
        return self._games.save(game)

    @staticmethod
    def _ensure_not_wiping_recorded_game(current: Game, plate_appearances: list[PlateAppearance]) -> None:
        """記録のある試合を、打席が空のまま保存して消さないようにする。

        - 打席のある試合: 空の打席で上書きすると、打席も明細も得点も全部消える。
          不具合のあるクライアントが空の配列を送っただけで黙って全消去になるのを防ぐ。
        - 打席の無い古い試合（明細と得点だけが保存されている）: 打席から導き直せないので、
          空の打席で上書きすると明細も得点も0に置き換わって元に戻せない。

        未記録の試合（`Game.is_recorded` が偽。打席も明細も無い）を空で保存するのは通す。
        記録済みかどうかの判定は `Game.is_recorded` だけを使い、ここで別の条件を書かない
        （得点だけが入った試合は未記録として集計から外れるので、ここでも未記録として扱う）。
        """
        if plate_appearances:
            return
        if current.plate_appearances:
            raise InvalidGame("打席がすべて取り除かれています。記録を全部消す場合は、試合ごと削除してください。")
        if current.is_recorded:
            raise InvalidGame(
                "この試合は打席の記録がない古い形式で保存されています。"
                "打席を1つ以上入力してから保存してください（空のまま保存すると成績が消えます）。"
            )

    def _entered_sequence(self, plate_appearances: list[PlateAppearance], slot: LineupSlot) -> int | None:
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
                for entry in plate_appearances
                if any(sub.entering_runner_id == slot.player_id for sub in entry.substitutions)
            ]
            return min(found) if found else None
        if position is FieldingPosition.PINCH_HITTER:
            found = [entry.sequence for entry in plate_appearances if entry.batter_id == slot.player_id]
            return min(found) if found else None
        if position is FieldingPosition.PITCHER:
            found = [entry.sequence for entry in plate_appearances if entry.pitcher_id == slot.player_id]
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
            for entry in plate_appearances
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
