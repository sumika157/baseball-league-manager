"""リポジトリの Django ORM 実装。

ORM モデルとドメインオブジェクトの相互変換（マッピング）もここで行う。
ドメイン層はこのモジュールを知らない。

選手の通算成績はテーブルに持たず、試合の明細を合計して求める。
合計は SQL の集計で行い、そこから作った BattingLine / PitchingLine に
打率や防御率の計算をさせる。式をドメインの一箇所に保つため。
"""

from __future__ import annotations

from django.db import transaction
from django.db.models import Prefetch, Sum

from ..domain.entities import (
    Captaincy,
    Game,
    GameBatting,
    GamePitching,
    League,
    Player,
    Stint,
    Team,
)
from ..domain.exceptions import (
    GameNotFound,
    LeagueNotFound,
    TeamNotFound,
)
from ..domain.value_objects import (
    BattingLine,
    FieldingPosition,
    Handedness,
    InningsPitched,
    JerseyNumber,
    LineScore,
    PitchingLine,
    Position,
    Profile,
    Season,
)
from . import orm_models

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
)

_PITCHING_COUNTS = (
    "wins",
    "losses",
    "saves",
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


class DjangoTeamRepository:
    """TeamRepository の Django ORM 実装。"""

    def find_by_id(self, team_id: int) -> Team:
        try:
            row = (
                orm_models.Team.objects.select_related("league")
                .prefetch_related(self._stints_prefetch())
                .get(id=team_id)
            )
        except orm_models.Team.DoesNotExist:
            raise TeamNotFound(f"チームが見つかりません（id={team_id}）。") from None

        return self._to_domain(row, with_roster=True)

    def find_all(self) -> list[Team]:
        rows = orm_models.Team.objects.select_related("league").order_by("display_order", "name")
        return [self._to_domain(row, with_roster=False) for row in rows]

    def find_all_with_roster(self) -> list[Team]:
        rows = (
            orm_models.Team.objects.select_related("league")
            .prefetch_related(self._stints_prefetch())
            .order_by("display_order", "name")
        )
        return [self._to_domain(row, with_roster=True) for row in rows]

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
        """
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

    def _to_domain(self, row: orm_models.Team, *, with_roster: bool) -> Team:
        players = []
        if with_roster:
            # このチームに在籍したことのある選手を、在籍の情報つきで組み立てる
            stint_rows = list(
                orm_models.PlayerStint.objects.filter(team=row).select_related("player").order_by("number")
            )
            player_rows = {s.player.id: s.player for s in stint_rows}
            batting = _batting_totals(list(player_rows))
            pitching = _pitching_totals(list(player_rows))

            careers = _careers_of(list(player_rows))
            captaincies = _captaincies_of(list(player_rows))

            for player_id, p in player_rows.items():
                career = careers.get(player_id, [])
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
                        profile=_profile_of(p),
                        batting=batting.get(player_id, BattingLine()),
                        pitching=pitching.get(player_id, PitchingLine()),
                        career=career,
                        captaincies=captaincies.get(player_id, []),
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


def _profile_of(row) -> Profile:
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


def _batting_totals(player_ids: list[int]) -> dict[int, BattingLine]:
    """選手ごとの通算打撃成績を SQL の集計で求める。"""
    if not player_ids:
        return {}
    rows = (
        orm_models.GameBattingLine.objects.filter(player_id__in=player_ids)
        .values("player_id")
        .annotate(**{f: Sum(f) for f in _BATTING_FIELDS})
    )
    # values() の行から可変のキーで取り出すため、TypedDict の字面キー検査は効かない
    return {r["player_id"]: BattingLine(**{f: r[f] or 0 for f in _BATTING_FIELDS}) for r in rows}  # type: ignore[literal-required]


def _pitching_totals(player_ids: list[int]) -> dict[int, PitchingLine]:
    """選手ごとの通算投球成績を求める。

    投球回だけは 5.2 が「5回と2/3」を意味する特殊な表記のため、単純な合計では
    正しくない（5.2 + 5.2 は 10.4 ではなく 11.1）。明細を取り出して
    InningsPitched に足し合わせさせる。
    """
    if not player_ids:
        return {}

    counts = (
        orm_models.GamePitchingLine.objects.filter(player_id__in=player_ids)
        .values("player_id")
        .annotate(**{f: Sum(f) for f in _PITCHING_COUNTS})
    )
    innings: dict[int, InningsPitched] = {}
    # 先発登板数と救援勝利は登板順から導く。SQL の集計では表しにくいので
    # 明細を1度読んで数える（投球回の合計も同じ明細から取る）
    derived: dict[int, dict[str, int]] = {}
    for player_id, notation, order, wins in orm_models.GamePitchingLine.objects.filter(
        player_id__in=player_ids
    ).values_list("player_id", "innings_pitched", "appearance_order", "wins"):
        innings[player_id] = innings.get(player_id, InningsPitched.zero()) + InningsPitched.from_notation(notation)
        entry = derived.setdefault(player_id, {"starts": 0, "relief_wins": 0})
        if order <= 1:
            entry["starts"] += 1
        else:
            entry["relief_wins"] += wins

    return {
        r["player_id"]: PitchingLine(
            innings=innings.get(r["player_id"], InningsPitched.zero()),
            **{f: r[f] or 0 for f in _PITCHING_COUNTS},  # type: ignore[literal-required]
            **derived.get(r["player_id"], {}),
        )
        for r in counts
    }


class DjangoGameRepository:
    """試合（Game 集約）の永続化。"""

    def find_by_id(self, game_id: int) -> Game:
        try:
            row = orm_models.Game.objects.prefetch_related("batting_lines", "pitching_lines", "inning_scores").get(
                id=game_id
            )
        except orm_models.Game.DoesNotExist:
            raise GameNotFound(f"試合が見つかりません（id={game_id}）。") from None
        return self._to_domain(row)

    def find_all(self, year: int | None = None) -> list[Game]:
        rows = orm_models.Game.objects.prefetch_related("batting_lines", "pitching_lines", "inning_scores")
        if year is not None:
            rows = rows.filter(year=year)
        return [self._to_domain(row) for row in rows]

    def find_by_team(self, team_id: int, year: int | None = None) -> list[Game]:
        from django.db.models import Q

        rows = orm_models.Game.objects.filter(Q(home_team_id=team_id) | Q(away_team_id=team_id)).prefetch_related(
            "batting_lines", "pitching_lines", "inning_scores"
        )
        if year is not None:
            rows = rows.filter(year=year)
        return [self._to_domain(row) for row in rows]

    @transaction.atomic
    def save(self, game: Game) -> Game:
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
            defaults = {f: getattr(entry.line, f) for f in _BATTING_FIELDS}
            defaults.update(
                {
                    "batting_order": entry.batting_order,
                    "slot_sequence": entry.slot_sequence,
                    "fielding_position": (entry.fielding_position.value if entry.fielding_position else ""),
                }
            )
            line_row, _ = orm_models.GameBattingLine.objects.update_or_create(
                game=row, player_id=entry.player_id, defaults=defaults
            )
            entry.id = line_row.id

        for pitching_entry in game.pitching:
            defaults = {f: getattr(pitching_entry.line, f) for f in _PITCHING_COUNTS}
            defaults["innings_pitched"] = float(pitching_entry.line.innings.to_notation())
            defaults["appearance_order"] = pitching_entry.appearance_order
            defaults["entered_inning"] = pitching_entry.entered_inning
            pitching_row, _ = orm_models.GamePitchingLine.objects.update_or_create(
                game=row, player_id=pitching_entry.player_id, defaults=defaults
            )
            pitching_entry.id = pitching_row.id

        self._save_line_score(row, game)

        # 集約から外された成績は削除する。上書きだけだと、いったん入力した
        # 選手を「出場していない」に戻せない
        orm_models.GameBattingLine.objects.filter(game=row).exclude(
            player_id__in=[e.player_id for e in game.batting]
        ).delete()
        orm_models.GamePitchingLine.objects.filter(game=row).exclude(
            player_id__in=[e.player_id for e in game.pitching]
        ).delete()

        return game

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
    def _to_domain(cls, row: orm_models.Game) -> Game:
        game = Game(
            id=row.id,
            season=Season(row.year),
            played_on=row.played_on,
            home_team_id=row.home_team_id,
            away_team_id=row.away_team_id,
            home_score=row.home_score,
            away_score=row.away_score,
            line_score=cls._to_line_score(row),
        )
        game.batting = [
            GameBatting(
                id=b.id,
                player_id=b.player_id,
                line=BattingLine(**{f: getattr(b, f) for f in _BATTING_FIELDS}),
                batting_order=b.batting_order,
                slot_sequence=b.slot_sequence,
                fielding_position=FieldingPosition.from_label(b.fielding_position),
            )
            for b in row.batting_lines.all()
        ]
        game.pitching = [
            GamePitching(
                id=p.id,
                player_id=p.player_id,
                line=PitchingLine(
                    innings=InningsPitched.from_notation(p.innings_pitched),
                    **{f: getattr(p, f) for f in _PITCHING_COUNTS},
                    # 1試合の行なので、先発なら1、救援での勝利ならその勝利数
                    starts=1 if p.appearance_order <= 1 else 0,
                    relief_wins=p.wins if p.appearance_order > 1 else 0,
                ),
                appearance_order=p.appearance_order,
                entered_inning=p.entered_inning,
            )
            for p in row.pitching_lines.all()
        ]
        return game


class DjangoLeagueRepository:
    """LeagueRepository の Django ORM 実装。"""

    def find_by_id(self, league_id: int) -> League:
        try:
            row = orm_models.League.objects.get(id=league_id)
        except orm_models.League.DoesNotExist:
            raise LeagueNotFound(f"リーグが見つかりません（id={league_id}）。") from None
        return self._to_domain(row)

    def find_all(self) -> list[League]:
        # 管理画面で手動設定した表示順を既定にする。順位表・ダッシュボードの
        # タブ・チーム一覧の並びが、この順に揃う
        return [self._to_domain(row) for row in orm_models.League.objects.order_by("display_order", "name")]

    @staticmethod
    def _to_domain(row: orm_models.League) -> League:
        return League(
            id=row.id,
            name=row.name,
            foreign_player_roster_limit=row.foreign_player_roster_limit,
            foreign_player_game_limit=row.foreign_player_game_limit,
        )
