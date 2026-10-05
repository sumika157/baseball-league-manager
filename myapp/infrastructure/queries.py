"""参照専用のクエリ（リードモデル）。

一覧表示のように「集約の不変条件を扱わず、値を読むだけ」の処理は、
集約を組み立てずに直接 DTO を作った方が素直で速い。
更新はリポジトリ経由（集約単位）、参照はこちら、と役割を分ける。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import fields
from datetime import date, timedelta
from itertools import groupby
from operator import itemgetter
from typing import Any

from django.contrib.auth.models import AnonymousUser, User
from django.db.models import BooleanField, Case, Count, Exists, Max, Min, OuterRef, Q, QuerySet, Sum, Value, When

from ..application.dto import (
    ActivePlayerStats,
    FieldingRow,
    GameNote,
    GameRow,
    LastStart,
    PeriodBatting,
    PeriodPitching,
    PitchingOuting,
    PlayerFielding,
    PlayerSearchRow,
    SimulationContext,
    SimulationPlayer,
    SimulationTeam,
    TeamSummary,
    WorldSummary,
)
from ..domain.entities import Game, winning_team_id
from ..domain.exceptions import WorldNotFound
from ..domain.pennant.retirement import PlayingTime
from ..domain.pennant.world import WorldScope
from ..domain.simulation.manager import RECENT_PITCHING_DAYS
from ..domain.value_objects import BattingLine, FieldingLine, PitchingLine, Position, Profile, Season
from . import orm_models
from .repositories import batting_totals, game_batting_of, game_pitching_of, pitching_totals, profile_of
from .scoping import (
    fixtures_in_worlds,
    games_in,
    games_in_worlds,
    leagues_in_worlds,
    period_covering,
    players_in,
    stints_in,
    teams_in,
    world_condition,
)


def recorded_games_filter() -> Q:
    """記録済みの試合の条件。`Game.is_recorded`（domain/entities.py）を SQL で書いたもの。

    打席も打撃・投球の明細も無い試合が「未記録」。その否定がこの条件。意味の出典は
    `Game.is_recorded` で、ここは SQL 側の写し（食い違わないことを
    `tests/integration/test_unrecorded_games.py` が突き合わせる）。
    """
    return (
        Q(Exists(orm_models.GameBattingLine.objects.filter(game_id=OuterRef("pk"))))
        | Q(Exists(orm_models.GamePitchingLine.objects.filter(game_id=OuterRef("pk"))))
        | Q(Exists(orm_models.GamePlateAppearance.objects.filter(game_id=OuterRef("pk"))))
    )


def _with_recorded(rows: QuerySet[orm_models.Game]) -> QuerySet[orm_models.Game]:
    """各試合に、記録済みかどうか（`recorded`）を付ける。"""
    return rows.annotate(
        recorded=Case(
            When(recorded_games_filter(), then=Value(True)), default=Value(False), output_field=BooleanField()
        )
    )


class DjangoPlayerFieldingQuery:
    """PlayerFieldingQuery の Django ORM 実装。守備成績を SQL の集計で読む。

    選手ページが要るのは通算と年度別の合計だけなので、試合（集約）も打席も組み立てない。
    守備率は合計した実数から `FieldingLine` に計算させる（式の出典を1つに保つ）。
    """

    # 項目は値オブジェクトから引く。ここに並べると、項目を足したときに集計だけが古くなる
    _SUMS = {f.name: Sum(f.name) for f in fields(FieldingLine)}

    def __init__(self, scope: WorldScope) -> None:
        self._scope = scope

    def for_player(self, player_id: int, team_id: int) -> PlayerFielding | None:
        # 選手ごとに行が別なので範囲外の行は混ざらないが、他のクエリと同じく範囲で絞る
        rows = orm_models.GameFieldingLine.objects.filter(
            world_condition("game__home_team__league", self._scope), player_id=player_id
        )
        total = rows.aggregate(games=Count("id"), **self._SUMS)
        if not total["games"]:
            return None

        # 年度別はこのチームでの成績（打撃・投球の年度別と同じ）。通算は移籍前も含む
        yearly = (
            rows.filter(Q(game__home_team_id=team_id) | Q(game__away_team_id=team_id))
            .values("game__year")
            .annotate(games=Count("id"), **self._SUMS)
            .order_by("game__year")
        )
        return PlayerFielding(
            career=self._row("通算", total),
            years=[self._row(f"{entry['game__year']}年", entry) for entry in yearly],
        )

    @staticmethod
    def _row(label: str, sums: Mapping[str, Any]) -> FieldingRow:
        line = FieldingLine(**{f.name: sums[f.name] or 0 for f in fields(FieldingLine)})
        return FieldingRow(
            label=label,
            games=sums["games"],
            total_chances=line.total_chances,
            **{f.name: getattr(line, f.name) for f in fields(FieldingLine)},
            fielding_percentage=line.fielding_percentage,
        )


class DjangoFieldingTotalsQuery:
    """FieldingTotalsQuery の Django ORM 実装。選手ごとの通算守備成績を、1回の SQL の集計で読む。"""

    _SUMS = DjangoPlayerFieldingQuery._SUMS

    def __init__(self, scope: WorldScope) -> None:
        self._scope = scope

    def totals_for(self, player_ids: Sequence[int]) -> dict[int, FieldingLine]:
        if not player_ids:
            return {}
        # 範囲だけで絞って選手ごとに集計し、欲しい選手は集計後に選ぶ。選手の id の一覧（1,600人）を
        # SQL に渡して範囲の JOIN と併せると、実行計画が崩れて 0.05 秒が 0.8 秒になった（実測）。
        # 集計後の行は選手1人につき1行なので、Python で選んでも組み立てる量は変わらない
        wanted = set(player_ids)
        rows = (
            orm_models.GameFieldingLine.objects.filter(world_condition("game__home_team__league", self._scope))
            .values("player_id")
            .annotate(**self._SUMS)
        )
        totals: dict[int, FieldingLine] = {}
        for row in rows:
            # values() の行から可変のキーで取り出すため、TypedDict の字面キー検査は効かない
            sums: Mapping[str, Any] = row
            if sums["player_id"] in wanted:
                totals[sums["player_id"]] = FieldingLine(**{f.name: sums[f.name] or 0 for f in fields(FieldingLine)})
        return totals


class DjangoSimulationContextQuery:
    """SimulationContextQuery の Django ORM 実装。球団・選手・直近の登板を、集約を組み立てずに読む。

    読むのは**範囲の現在の状態だけ**で、クエリは5本（球団・選手・最後の試合日・直近の先発・直近の登板）。
    打席や明細の全件は読まない。
    """

    def __init__(self, scope: WorldScope) -> None:
        self._scope = scope

    def last_played_on(self) -> date | None:
        return games_in(self._scope).aggregate(last=Max("played_on"))["last"]

    def load(self, *, before: date) -> SimulationContext:
        lines = orm_models.GamePitchingLine.objects.filter(
            world_condition("game__home_team__league", self._scope), game__played_on__lt=before
        )
        last_starts = tuple(
            LastStart(pitcher_id=pitcher_id, played_on=last)
            for pitcher_id, last in lines.filter(appearance_order__lte=1)
            .values_list("player_id")
            .annotate(last=Max("game__played_on"))
            .order_by("player_id")
        )

        recent = lines.filter(game__played_on__gte=before - timedelta(days=RECENT_PITCHING_DAYS)).order_by(
            "game_id", "appearance_order", "player_id"
        )
        recent_outings = tuple(
            PitchingOuting(played_on=rows[0][1], pitcher_ids=tuple(row[2] for row in rows))
            for _, group in groupby(recent.values_list("game_id", "game__played_on", "player_id"), key=itemgetter(0))
            for rows in [list(group)]
        )
        return SimulationContext(teams=self.teams(), last_starts=last_starts, recent_outings=recent_outings)

    def teams(self) -> tuple[SimulationTeam, ...]:
        players: dict[int, list[SimulationPlayer]] = {}
        stints = (
            stints_in(self._scope)
            .filter(to_year__isnull=True)
            .order_by("team_id", "number", "player_id")
            .values_list("team_id", "player_id", "player__name", "player__position", "player__is_foreign_player")
        )
        for team_id, player_id, name, position, is_foreign in stints:
            players.setdefault(team_id, []).append(
                SimulationPlayer(
                    player_id=player_id, name=name, position=Position.from_label(position), is_foreign=is_foreign
                )
            )

        return tuple(
            SimulationTeam(
                team_id=team_id,
                name=name,
                foreign_roster_limit=roster_limit,
                foreign_game_limit=game_limit,
                players=tuple(players.get(team_id, ())),
            )
            for team_id, name, roster_limit, game_limit in teams_in(self._scope)
            .order_by("id")
            .values_list("id", "name", "league__foreign_player_roster_limit", "league__foreign_player_game_limit")
        )


class DjangoPlayerStatsQuery:
    """PlayerStatsQuery の Django ORM 実装。

    ランキング・タイトル・選手の一覧に要るのは、選手の名前・守備位置・所属と成績だけ。
    チーム集約を全部組み立てると経歴・主将歴・プロフィールまで作ることになり、
    その Python 側の時間が応答の大半を占めていた。成績は SQL で集計し、選手ごとに1つの
    DTO にする（集計の式は `repositories.batting_totals` / `pitching_totals` が出典）。

    **在籍は年と期間で引く**（`from_year <= Y かつ (to_year が空 または Y <= to_year)`）。`to_year` が空の
    在籍だけを引くと、引退した選手が過去の年のタイトルや成績から消える。成績もその年の試合だけを数え、
    リーグをまたぐ交流戦も含める。一覧は選手の通算を全シーズンぶん積み直さず、必要な年だけ読む。

    在籍も成績も世界の範囲（`WorldScope`）で絞る。選手は世界ごとに別の行なので、範囲の外の
    明細は数えても結果は変わらないが、集計する行を範囲の中に限って読む量を抑える。
    """

    def __init__(self, scope: WorldScope) -> None:
        self._scope = scope

    def list_career(self) -> list[ActivePlayerStats]:
        return self.list_roster(year=None)

    def list_season(self, league_id: int, year: int) -> list[ActivePlayerStats]:
        return self.list_roster(year=year, league_id=league_id)

    def list_roster(
        self,
        *,
        year: int | None,
        team_id: int | None = None,
        league_id: int | None = None,
        with_profile: bool = False,
    ) -> list[ActivePlayerStats]:
        """プロトコルの `list_roster`。`with_profile` を立てると年齢などの材料も読む（タイトルには要らない）。"""
        stints = self._stints(year)
        if team_id is not None:
            stints = stints.filter(team_id=team_id)
        if league_id is not None:
            stints = stints.filter(team__league_id=league_id)
        rows = list(
            stints.select_related("player", "team").order_by("team__display_order", "team__name", "number", "id")
        )
        if year is not None:
            rows = self._one_per_player(rows)

        games = Q() if year is None else Q(game__year=year)
        if team_id is None and league_id is None:
            # 世界の全選手。IN 句に何千件も並べず、範囲の試合で絞る
            games &= world_condition("game__home_team__league", self._scope)
            batting = batting_totals(games=games)
            pitching = pitching_totals(games=games)
        else:
            player_ids = [row.player_id for row in rows]
            batting = batting_totals(player_ids, games=games or None)
            pitching = pitching_totals(player_ids, games=games or None)

        captains = self._captains(team_id, year) if team_id is not None else frozenset()
        return [
            ActivePlayerStats(
                player_id=row.player_id,
                name=row.player.name,
                number=row.number,
                position=Position.from_label(row.player.position),
                team_id=row.team_id,
                team_name=row.team.name,
                league_id=row.team.league_id,
                batting=batting.get(row.player_id, BattingLine()),
                pitching=pitching.get(row.player_id, PitchingLine()),
                profile=profile_of(row.player) if with_profile else Profile(),
                is_captain=row.player_id in captains,
            )
            for row in rows
        ]

    def _stints(self, year: int | None) -> QuerySet[orm_models.PlayerStint]:
        """範囲の中の在籍。`year` が None なら在籍中（退団年が空）、年を渡せばその年に在籍していた期間。"""
        return stints_in(self._scope).filter(period_covering(year))

    @staticmethod
    def _one_per_player(rows: list[orm_models.PlayerStint]) -> list[orm_models.PlayerStint]:
        """同じ年に在籍が2つある選手（年の途中で移った選手）は、その年の終わりにいた方の1行にする。

        成績は選手ごとにその年の全試合を数えるので、2行のまま返すと同じ成績が2度並ぶ。
        退団していない在籍（`to_year` が空）、次に新しい在籍の順に選ぶ。
        """
        best: dict[int, orm_models.PlayerStint] = {}
        for row in rows:
            current = best.get(row.player_id)
            if current is None or _recency(row) > _recency(current):
                best[row.player_id] = row
        return [row for row in rows if best[row.player_id] is row]

    def _captains(self, team_id: int, year: int | None) -> frozenset[int]:
        """その球団でその範囲に主将だった選手の id。"""
        rows = orm_models.Captaincy.objects.filter(period_covering(year), team_id=team_id)
        return frozenset(rows.values_list("player_id", flat=True))


def _recency(stint: orm_models.PlayerStint) -> tuple[bool, int, int]:
    """同じ年の在籍のうち、どちらが新しいか（退団していない、開始年、id の順に新しい方が大きい）。"""
    return (stint.to_year is None, stint.from_year, stint.id)


class DjangoPlayerSearchQuery:
    """選手を名前で探す。

    チーム数が増えると、所属を知らないと選手にたどり着けないため。
    集約を組み立てず、一覧に必要な値だけを読む。
    """

    LIMIT = 50

    def __init__(self, scope: WorldScope) -> None:
        self._scope = scope

    def search(self, keyword: str) -> list[PlayerSearchRow]:
        keyword = (keyword or "").strip()
        if not keyword:
            return []

        # 世界ごとに選手の行が別で、複製した選手は元の選手と同じ名前を持つ。
        # 名前だけで引くと2人ずつ出るので、範囲の選手に絞る
        rows = (
            players_in(self._scope)
            .filter(name__icontains=keyword)
            .prefetch_related("stints__team__league")
            .order_by("name")[: self.LIMIT]
        )

        results = []
        for row in rows:
            stints = list(row.stints.all())
            current = next((s for s in stints if s.to_year is None), None)
            latest = current or (max(stints, key=lambda s: s.from_year) if stints else None)
            results.append(
                PlayerSearchRow(
                    id=row.id,
                    name=row.name,
                    position=row.position,
                    team_id=latest.team_id if latest else None,
                    team_name=latest.team.name if latest else "",
                    league_name=latest.team.league.name if latest else "",
                    number=latest.number if latest else None,
                    is_active=current is not None,
                )
            )
        return results


class DjangoTeamPermissionQuery:
    """チーム担当者の権限判定。

    ログインすれば誰でも全チームを編集できた状態をやめ、管理ユーザー
    （is_staff）以外は自分が担当するチームが関わる範囲だけ編集できるようにする。
    「担当者かどうか」は Team.managers という事実だけを見て決まるので、
    ドメインの業務ルールではなく、この参照専用クエリに置く。

    **範囲（`WorldScope`）の外のチームは、管理ユーザーでも編集できない。** ペナントの球団は
    実データの側から書けず、ペナントの書き込みは世界のオーナーで判定する（その判定は
    ペナントの画面を作る段階で足す。それまでペナントの範囲は常に False）。
    """

    def __init__(self, scope: WorldScope) -> None:
        self._scope = scope

    def can_manage(self, user: User | AnonymousUser, team_id: int) -> bool:
        """指定チームを編集できるか。"""
        return self.can_manage_any(user, (team_id,))

    def can_manage_any(self, user: User | AnonymousUser, team_ids: Iterable[int]) -> bool:
        """渡したチームのうち、少なくとも1つを編集できるか。

        試合は2チームにまたがるため、どちらか一方の担当者であれば編集できる。
        """
        if not user.is_authenticated:
            return False
        if self._scope.is_pennant:
            return False

        in_scope = teams_in(self._scope).filter(id__in=list(team_ids))
        if user.is_staff or user.is_superuser:
            return in_scope.exists()
        return in_scope.filter(managers=user).exists()


class DjangoGameListQuery:
    """GameListQuery の Django ORM 実装。試合一覧に必要な値だけを取得する。

    一覧に要るのは日付・チーム名・スコアだけなので、集約（Game）を組み立てない。
    集約経由だと1試合ごとに打撃・投球・イニングスコアの明細まで読むため、
    件数が増えると一覧が開かなくなる。

    範囲（`WorldScope`）を必須で受け取り、読み出しはすべて SQL でその世界の試合に絞る。
    """

    def __init__(self, scope: WorldScope) -> None:
        self._scope = scope

    def _rows(
        self,
        *,
        year: int | None = None,
        team_id: int | None = None,
        month: int | None = None,
        league_id: int | None = None,
        after: date | None = None,
        through: date | None = None,
    ) -> QuerySet[orm_models.Game]:
        """絞り込みは SQL 側で行う。取得後に Python で捨てると件数ぶん無駄になる。"""
        rows = games_in(self._scope).select_related("home_team", "away_team")
        if year is not None:
            rows = rows.filter(year=year)
        if league_id is not None:
            # どちらかのチームが所属していれば、そのリーグの日程に含める
            # （リーグをまたぐ対戦も、両リーグの日程に現れる）
            rows = rows.filter(Q(home_team__league_id=league_id) | Q(away_team__league_id=league_id))
        if team_id is not None:
            rows = rows.filter(Q(home_team_id=team_id) | Q(away_team_id=team_id))
        if month is not None:
            rows = rows.filter(played_on__month=month)
        if after is not None:
            rows = rows.filter(played_on__gt=after)
        if through is not None:
            rows = rows.filter(played_on__lte=through)
        return rows

    def list_rows(
        self,
        *,
        year: int | None = None,
        team_id: int | None = None,
        month: int | None = None,
        league_id: int | None = None,
        after: date | None = None,
        through: date | None = None,
    ) -> list[GameRow]:
        rows = _with_recorded(
            self._rows(
                year=year, team_id=team_id, month=month, league_id=league_id, after=after, through=through
            ).order_by("-played_on", "-id")
        )
        return [
            GameRow(
                id=row.id,
                year=row.year,
                played_on=row.played_on,
                home_team_id=row.home_team_id,
                home_team_name=row.home_team.name,
                away_team_id=row.away_team_id,
                away_team_name=row.away_team.name,
                home_score=row.home_score,
                away_score=row.away_score,
                # 勝敗の判定はドメインの関数が唯一の出典。結果の文言は GameRow が持つ。
                # 未記録の試合は 0-0 でも引分ではないので、勝者も付けない
                winner_team_id=(
                    winning_team_id(row.home_team_id, row.away_team_id, row.home_score, row.away_score)
                    if row.recorded  # type: ignore[attr-defined]  # annotate(recorded) で足した属性
                    else None
                ),
                is_recorded=row.recorded,  # type: ignore[attr-defined]  # annotate(recorded) で足した属性
            )
            for row in rows
        ]

    def list_for_standings(self, *, year: int | None = None) -> list[Game]:
        """順位表の計算に渡す試合。**明細は読まない**。

        順位は得点と対戦カードだけで決まるので、打撃・投球・イニングスコアは
        要らない。リポジトリの find_all() は集約として明細まで揃えるため、
        順位表のためだけに呼ぶと件数ぶん無駄になる（3480試合で3.5秒かかった）。
        戻り値は成績を持たない Game なので、順位・勝敗の集計にだけ使う。

        **未記録の試合も返す**（直近の試合の表示に要る）。ただし明細を読まないので
        `Game.is_recorded` は自力で判定できない。記録済みかどうかは SQL で調べて
        `recorded_hint` に持たせる。順位・勝敗の集計はドメインサービスが未記録を数えない。
        """
        rows = _with_recorded(games_in(self._scope))
        if year is not None:
            rows = rows.filter(year=year)
        return [
            Game(
                recorded_hint=row.recorded,  # type: ignore[attr-defined]  # annotate(recorded) で足した属性
                read_only=True,
                id=row.id,
                season=Season(row.year),
                played_on=row.played_on,
                home_team_id=row.home_team_id,
                away_team_id=row.away_team_id,
                home_score=row.home_score,
                away_score=row.away_score,
            )
            for row in rows
        ]

    def list_for_player(self, team_id: int, player_id: int) -> list[Game]:
        """そのチームの試合のうち、選手が出場した試合（打撃か投球に記録がある試合）。試合日の古い順。

        **その選手の打撃・投球の明細だけ**を持つ（相手や他の選手の明細・イニングスコアは読まない）。
        選手ページの年度別・月別の成績と試合ごとの記録はこの選手の明細だけで足りる。チームの試合を
        明細ごと全シーズンぶん組み立てると、シーズンが増えるほど遅くなる。参照専用で、保存しない。
        """
        in_team = Q(game__home_team_id=team_id) | Q(game__away_team_id=team_id)
        in_scope = world_condition("game__home_team__league", self._scope)
        batting = orm_models.GameBattingLine.objects.filter(in_scope, in_team, player_id=player_id).select_related(
            "game"
        )
        pitching = orm_models.GamePitchingLine.objects.filter(in_scope, in_team, player_id=player_id).select_related(
            "game"
        )

        games: dict[int, Game] = {}

        def game_of(row: orm_models.Game) -> Game:
            if row.id not in games:
                games[row.id] = Game(
                    recorded_hint=True,  # 明細がある試合は記録済み
                    read_only=True,
                    id=row.id,
                    season=Season(row.year),
                    played_on=row.played_on,
                    home_team_id=row.home_team_id,
                    away_team_id=row.away_team_id,
                    home_score=row.home_score,
                    away_score=row.away_score,
                )
            return games[row.id]

        for batting_row in batting:
            game_of(batting_row.game).batting.append(game_batting_of(batting_row))
        for pitching_row in pitching:
            game_of(pitching_row.game).pitching.append(game_pitching_of(pitching_row))
        return sorted(games.values(), key=lambda game: (game.played_on, game.id or 0))

    def list_seasons(self, *, league_id: int | None = None, recorded_only: bool = False) -> list[int]:
        """試合のある年を新しい順に。"""
        rows = games_in(self._scope)
        if league_id is not None:
            rows = rows.filter(Q(home_team__league_id=league_id) | Q(away_team__league_id=league_id))
        if recorded_only:
            rows = rows.filter(recorded_games_filter())
        # order_by() で既定の並び（試合日・id）を外す。外さないと、並びの列まで DISTINCT の対象になり、
        # 試合の数だけ同じ年が返る
        return sorted(rows.order_by().values_list("year", flat=True).distinct(), reverse=True)

    def count_by_team(self, *, year: int | None = None) -> dict[int, int]:
        """チームid → 試合数。規定打席・規定投球回の基準になる。

        数えるだけなので試合を1件も組み立てない。ホームとビジターで別に数えて
        足す（1試合は両チームの1試合として数える）。未記録の試合は数えない
        （規定打席・規定投球回は、実際に行われた試合の数で決まる）。
        """
        rows = games_in(self._scope).filter(recorded_games_filter())
        if year is not None:
            rows = rows.filter(year=year)

        counts: dict[int, int] = {}
        for field in ("home_team_id", "away_team_id"):
            for entry in rows.values(field).annotate(played=Count("id")):
                counts[entry[field]] = counts.get(entry[field], 0) + entry["played"]
        return counts

    def list_months(
        self, *, year: int | None = None, team_id: int | None = None, league_id: int | None = None
    ) -> list[int]:
        """その絞り込みで試合がある月を昇順に。

        月の切り出しは SQL 側で行う。1シーズンで千件を超えるため、
        全部の試合日を持ってきて Python で数えると月を割る意味が薄れる。
        """
        rows = self._rows(year=year, team_id=team_id, league_id=league_id)
        # dates() は月ごとに1つの日付を SQL 側で重複なく返す（返るのは最大12件）
        return sorted({date.month for date in rows.dates("played_on", "month")})

    def latest_year(self) -> int | None:
        """最新シーズン。一覧の既定に使う。"""
        seasons = self.list_seasons()
        return seasons[0] if seasons else None


class DjangoPennantActivityQuery:
    """PennantActivityQuery の Django ORM 実装。期間の見どころを、明細の SQL 集計で読む。

    読む行は範囲（`WorldScope`）の中の、対象の試合・球団・期間に絞る。集約は組み立てない。
    """

    def __init__(self, scope: WorldScope) -> None:
        self._scope = scope

    def game_notes(self, game_ids: Sequence[int]) -> dict[int, GameNote]:
        ids = list(game_ids)
        if not ids:
            return {}
        in_scope = world_condition("game__home_team__league", self._scope)
        decided: dict[int, dict[str, str]] = {}
        pitching = (
            orm_models.GamePitchingLine.objects.filter(in_scope, game_id__in=ids)
            .filter(Q(wins__gt=0) | Q(losses__gt=0) | Q(saves__gt=0))
            .order_by("game_id", "appearance_order")
            .values_list("game_id", "player__name", "wins", "losses", "saves")
        )
        for game_id, name, wins, losses, saves in pitching:
            entry = decided.setdefault(game_id, {})
            if wins:
                entry["win"] = name
            if losses:
                entry["loss"] = name
            if saves:
                entry["save"] = name

        hitters: dict[int, list[str]] = {}
        batting = (
            orm_models.GameBattingLine.objects.filter(in_scope, game_id__in=ids, home_runs__gt=0)
            .order_by("game_id", "team_id", "batting_order", "slot_sequence")
            .values_list("game_id", "player__name", "home_runs")
        )
        for game_id, name, home_runs in batting:
            hitters.setdefault(game_id, []).append(name if home_runs == 1 else f"{name}（{home_runs}本）")

        return {
            game_id: GameNote(
                game_id=game_id,
                winning_pitcher=decided.get(game_id, {}).get("win", ""),
                losing_pitcher=decided.get(game_id, {}).get("loss", ""),
                save_pitcher=decided.get(game_id, {}).get("save", ""),
                home_runs=tuple(hitters.get(game_id, ())),
            )
            for game_id in sorted(decided.keys() | hitters.keys())
        }

    def batting_between(self, team_id: int, *, after: date, through: date) -> list[PeriodBatting]:
        # 合計と率は BattingLine が出典。ここは内訳（打数・単打・二塁打…）を選手ごとに足すだけ
        games = (
            world_condition("game__home_team__league", self._scope)
            & Q(team_id=team_id)
            & Q(game__played_on__gt=after, game__played_on__lte=through)
        )
        totals = batting_totals(games=games)
        names = dict(orm_models.Player.objects.filter(id__in=list(totals)).values_list("id", "name"))
        return [
            PeriodBatting(player_id=player_id, name=names[player_id], batting=line)
            for player_id, line in sorted(totals.items())
        ]

    def pitching_between(self, team_id: int, *, after: date, through: date) -> list[PeriodPitching]:
        rows = (
            orm_models.GamePitchingLine.objects.filter(
                world_condition("game__home_team__league", self._scope),
                game__played_on__gt=after,
                game__played_on__lte=through,
                # 投球の明細は球団を持たない。在籍中の球団で引く（世界の選手の在籍は1つ）
                player__stints__team_id=team_id,
                player__stints__to_year__isnull=True,
            )
            .values("player_id", "player__name")
            .annotate(wins=Sum("wins"), losses=Sum("losses"), saves=Sum("saves"))
            .order_by("player_id")
        )
        return [
            PeriodPitching(
                player_id=row["player_id"],
                name=row["player__name"],
                wins=row["wins"] or 0,
                losses=row["losses"] or 0,
                saves=row["saves"] or 0,
            )
            for row in rows
        ]


class DjangoTeamListQuery:
    """TeamListQuery の Django ORM 実装。チーム一覧に必要な値だけを取得する。"""

    def __init__(self, scope: WorldScope) -> None:
        self._scope = scope

    def list_summaries(self) -> list[TeamSummary]:
        rows = (
            teams_in(self._scope)
            .select_related("league", "home_stadium")
            # 在籍中＝退団年が空の在籍
            .annotate(active_player_count=Count("stints", filter=Q(stints__to_year__isnull=True)))
            # 管理画面で手動設定した表示順を既定にする
            .order_by("display_order", "name")
        )
        return [
            TeamSummary(
                id=row.id,
                name=row.name,
                league_id=row.league_id,
                league_name=row.league.name,
                player_count=row.active_player_count,
                stadium_name=row.home_stadium.name if row.home_stadium else "",
                # 所在地は球場から取る。チーム側には持たせない
                city=row.home_stadium.city if row.home_stadium else "",
            )
            for row in rows
        ]


class DjangoWorldSummaryQuery:
    """WorldSummaryQuery の Django ORM 実装。世界の台帳そのものなので、範囲は持たない。

    世界の数にかかわらず、一定のクエリ数（世界・最後の試合日・次の対戦の日・リーグの4本）で読む。
    """

    def get(self, world_id: int) -> WorldSummary:
        rows = self._read(orm_models.PennantWorld.objects.filter(id=world_id))
        if not rows:
            raise WorldNotFound(f"世界が見つかりません（id={world_id}）。")
        return rows[0]

    def list_all(self) -> list[WorldSummary]:
        return self._read(orm_models.PennantWorld.objects.all())

    @staticmethod
    def _read(worlds: QuerySet[orm_models.PennantWorld]) -> list[WorldSummary]:
        rows = list(worlds.select_related("managed_team"))
        ids = [row.id for row in rows]
        # order_by() で既定の並びを外す（外さないと、並びの列が GROUP BY に入って世界ごとにまとまらない）
        last_played = dict(
            games_in_worlds(ids).order_by().values_list("home_team__league__world_id").annotate(last=Max("played_on"))
        )
        next_fixture = dict(
            fixtures_in_worlds(ids).order_by().values_list("home_team__league__world_id").annotate(first=Min("date"))
        )
        first_league: dict[int, int] = {}
        leagues = leagues_in_worlds(ids).order_by("display_order", "name").values_list("world_id", "id")
        for world_id, league_id in leagues:
            first_league.setdefault(world_id, league_id)
        return [
            WorldSummary(
                world_id=row.id,
                name=row.name,
                start_year=row.start_year,
                managed_team_id=row.managed_team_id,
                managed_team_name=row.managed_team.name if row.managed_team is not None else "",
                default_league_id=(
                    row.managed_team.league_id if row.managed_team is not None else first_league.get(row.id)
                ),
                last_played_on=last_played.get(row.id),
                next_fixture_on=next_fixture.get(row.id),
                owner_id=row.owner_id,
            )
            for row in rows
        ]


class DjangoSeasonPlayingTimeQuery:
    """SeasonPlayingTimeQuery の Django ORM 実装。範囲（`WorldScope`）の中の、その年の試合だけを数える。

    打席数は打撃の集計（`batting_totals`。`BattingLine.plate_appearances` が出典で、打撃妨害・走塁妨害は
    含まない）から、アウト数は投球回の集計（`pitching_totals`。5.2 = 17アウトの換算は
    `InningsPitched` が出典）から取る。
    """

    def __init__(self, scope: WorldScope) -> None:
        self._scope = scope

    def for_year(self, year: int) -> dict[int, PlayingTime]:
        games = world_condition("game__home_team__league", self._scope) & Q(game__year=year)
        appearances = {player_id: line.plate_appearances for player_id, line in batting_totals(games=games).items()}
        outs = {player_id: line.innings.outs for player_id, line in pitching_totals(games=games).items()}
        return {
            player_id: PlayingTime(plate_appearances=appearances.get(player_id, 0), outs=outs.get(player_id, 0))
            for player_id in sorted(appearances.keys() | outs.keys())
        }
