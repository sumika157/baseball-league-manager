"""参照専用のクエリ（リードモデル）。

一覧表示のように「集約の不変条件を扱わず、値を読むだけ」の処理は、
集約を組み立てずに直接 DTO を作った方が素直で速い。
更新はリポジトリ経由（集約単位）、参照はこちら、と役割を分ける。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import fields
from typing import Any

from django.contrib.auth.models import AnonymousUser, User
from django.db.models import BooleanField, Case, Count, Exists, OuterRef, Q, QuerySet, Sum, Value, When

from ..application.dto import (
    ActivePlayerStats,
    AnalysisRosterRow,
    FielderUsage,
    FieldingRow,
    GameRow,
    MoveStintRow,
    PitcherUsage,
    PlayerFielding,
    PlayerSearchRow,
    RelatedStint,
    TeamAnalysisFacts,
    TeamSummary,
)
from ..domain.entities import Game, winning_team_id
from ..domain.value_objects import (
    BattingLine,
    FieldingLine,
    FieldingPosition,
    Handedness,
    PitchingLine,
    Position,
    Season,
)
from . import orm_models
from .repositories import batting_totals, pitching_totals


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

    def for_player(self, player_id: int, team_id: int) -> PlayerFielding | None:
        rows = orm_models.GameFieldingLine.objects.filter(player_id=player_id)
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


class DjangoPlayerStatsQuery:
    """PlayerStatsQuery の Django ORM 実装。

    ランキング・タイトルに要るのは、在籍中の選手の名前・守備位置・所属と成績だけ。
    チーム集約を全部組み立てると経歴・主将歴・プロフィールまで作ることになり、
    その Python 側の時間が応答の大半を占めていた。成績は SQL で集計し、選手ごとに1つの
    DTO にする（集計の式は `repositories.batting_totals` / `pitching_totals` が出典）。
    """

    def list_career(self) -> list[ActivePlayerStats]:
        stints = self._active_stints()
        # 全選手の通算を1度に集計する（在籍外の選手の分も出るが、引かないだけ）
        return self._rows(stints, batting_totals(), pitching_totals())

    def list_season(self, league_id: int, year: int) -> list[ActivePlayerStats]:
        stints = self._active_stints().filter(team__league_id=league_id)
        player_ids = [stint.player_id for stint in stints]
        # リーグのチームどうしの試合だけ。明細の行から見た試合の条件で絞る
        games = Q(game__year=year, game__home_team__league_id=league_id, game__away_team__league_id=league_id)
        return self._rows(
            stints,
            batting_totals(player_ids, games=games),
            pitching_totals(player_ids, games=games),
        )

    @staticmethod
    def _active_stints() -> QuerySet[orm_models.PlayerStint]:
        """在籍中＝退団年が空の在籍。チームの表示順、背番号順（ランキングの同値の並びに効く）。"""
        return (
            orm_models.PlayerStint.objects.filter(to_year__isnull=True)
            .select_related("player", "team")
            .order_by("team__display_order", "team__name", "number", "id")
        )

    @staticmethod
    def _rows(
        stints: Iterable[orm_models.PlayerStint],
        batting: Mapping[int, BattingLine],
        pitching: Mapping[int, PitchingLine],
    ) -> list[ActivePlayerStats]:
        return [
            ActivePlayerStats(
                player_id=stint.player_id,
                name=stint.player.name,
                number=stint.number,
                position=Position.from_label(stint.player.position),
                team_id=stint.team_id,
                team_name=stint.team.name,
                batting=batting.get(stint.player_id, BattingLine()),
                pitching=pitching.get(stint.player_id, PitchingLine()),
            )
            for stint in stints
        ]


class DjangoPlayerSearchQuery:
    """選手を名前で探す。

    チーム数が増えると、所属を知らないと選手にたどり着けないため。
    集約を組み立てず、一覧に必要な値だけを読む。
    """

    LIMIT = 50

    def search(self, keyword: str) -> list[PlayerSearchRow]:
        keyword = (keyword or "").strip()
        if not keyword:
            return []

        rows = (
            orm_models.Player.objects.filter(name__icontains=keyword)
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
    """

    def can_manage(self, user: User | AnonymousUser, team_id: int) -> bool:
        """指定チームを編集できるか。"""
        return self.can_manage_any(user, (team_id,))

    def can_manage_any(self, user: User | AnonymousUser, team_ids: Iterable[int]) -> bool:
        """渡したチームのうち、少なくとも1つを編集できるか。

        試合は2チームにまたがるため、どちらか一方の担当者であれば編集できる。
        """
        if not user.is_authenticated:
            return False
        if user.is_staff or user.is_superuser:
            return True
        return orm_models.Team.objects.filter(id__in=team_ids, managers=user).exists()


class DjangoGameListQuery:
    """GameListQuery の Django ORM 実装。試合一覧に必要な値だけを取得する。

    一覧に要るのは日付・チーム名・スコアだけなので、集約（Game）を組み立てない。
    集約経由だと1試合ごとに打撃・投球・イニングスコアの明細まで読むため、
    件数が増えると一覧が開かなくなる。
    """

    def _rows(
        self,
        *,
        year: int | None = None,
        team_id: int | None = None,
        month: int | None = None,
        league_id: int | None = None,
    ) -> QuerySet[orm_models.Game]:
        """絞り込みは SQL 側で行う。取得後に Python で捨てると件数ぶん無駄になる。"""
        rows = orm_models.Game.objects.select_related("home_team", "away_team")
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
        return rows

    def list_rows(
        self,
        *,
        year: int | None = None,
        team_id: int | None = None,
        month: int | None = None,
        league_id: int | None = None,
    ) -> list[GameRow]:
        rows = _with_recorded(
            self._rows(year=year, team_id=team_id, month=month, league_id=league_id).order_by("-played_on", "-id")
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
        rows = _with_recorded(orm_models.Game.objects.all())
        if year is not None:
            rows = rows.filter(year=year)
        return [
            Game(
                recorded_hint=row.recorded,  # type: ignore[attr-defined]  # annotate(recorded) で足した属性
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

    def list_seasons(self) -> list[int]:
        """試合のある年を新しい順に。"""
        return sorted(orm_models.Game.objects.values_list("year", flat=True).distinct(), reverse=True)

    def count_by_team(self, *, year: int | None = None) -> dict[int, int]:
        """チームid → 試合数。規定打席・規定投球回の基準になる。

        数えるだけなので試合を1件も組み立てない。ホームとビジターで別に数えて
        足す（1試合は両チームの1試合として数える）。未記録の試合は数えない
        （規定打席・規定投球回は、実際に行われた試合の数で決まる）。
        """
        rows = orm_models.Game.objects.filter(recorded_games_filter())
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


class DjangoTeamListQuery:
    """TeamListQuery の Django ORM 実装。チーム一覧に必要な値だけを取得する。"""

    def list_summaries(self) -> list[TeamSummary]:
        rows = (
            orm_models.Team.objects.select_related("league", "home_stadium")
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


class DjangoTeamAnalysisQuery:
    """TeamAnalysisQuery の Django ORM 実装。戦力分析の材料を固定本数のクエリで集める。

    クエリは在籍・野手の出場・投手の登板・入退団の在籍2本の5本（年の一覧は別に1本）で、選手の数に
    比例しない。集約を組み立てず、数えるだけなので明細の行も Python に持ち込まない。
    """

    def list_years(self, team_id: int) -> list[int]:
        # 試合数の数え方（count_by_team）と同じく、記録済みの試合だけ。登録しただけの試合は編成に効かない
        rows = orm_models.Game.objects.filter(recorded_games_filter()).filter(
            Q(home_team_id=team_id) | Q(away_team_id=team_id)
        )
        # 既定の並び（試合日）が DISTINCT に混ざらないよう order_by() で外す
        return sorted(set(rows.order_by().values_list("year", flat=True)), reverse=True)

    @staticmethod
    def _moves(team_id: int, year: int) -> tuple[list[MoveStintRow], list[RelatedStint]]:
        """入退団の材料。そのチームで加入年か退団年が year の在籍と、その選手の全在籍（他のチームを含む）。

        2本のクエリで済み、選手の数に比例しない。
        """
        own = list(
            orm_models.PlayerStint.objects.filter(team_id=team_id)
            .filter(Q(from_year=year) | Q(to_year=year))
            .select_related("player")
            .order_by("number", "id")
        )
        if not own:
            return [], []
        moves = [
            MoveStintRow(
                stint_id=stint.id,
                team_id=stint.team_id,
                player_id=stint.player_id,
                name=stint.player.name,
                number=stint.number,
                position=Position.from_label(stint.player.position),
                from_year=stint.from_year,
                to_year=stint.to_year,
                throws=Handedness.from_label(stint.player.throws),
                bats=Handedness.from_label(stint.player.bats),
            )
            for stint in own
        ]
        related = [
            RelatedStint(
                stint_id=stint.id,
                player_id=stint.player_id,
                team_id=stint.team_id,
                team_name=stint.team.name,
                from_year=stint.from_year,
                to_year=stint.to_year,
            )
            for stint in orm_models.PlayerStint.objects.filter(player_id__in={s.player_id for s in own})
            .select_related("team")
            .order_by("from_year", "id")
        ]
        return moves, related

    def load(self, team_id: int, year: int) -> TeamAnalysisFacts:
        # 在籍の期間の意味は Stint.covers と同じ（加入年 <= 年 かつ 退団年が空か 年 <= 退団年）
        stints = (
            orm_models.PlayerStint.objects.filter(team_id=team_id, from_year__lte=year)
            .filter(Q(to_year__isnull=True) | Q(to_year__gte=year))
            .select_related("player")
            .order_by("number", "id")
        )
        roster: list[AnalysisRosterRow] = []
        seen: set[int] = set()
        for stint in stints:
            if stint.player_id in seen:
                continue
            seen.add(stint.player_id)
            player = stint.player
            roster.append(
                AnalysisRosterRow(
                    player_id=player.id,
                    name=player.name,
                    number=stint.number,
                    position=Position.from_label(player.position),
                    birth_date=player.birth_date,
                    throws=Handedness.from_label(player.throws),
                    bats=Handedness.from_label(player.bats),
                    is_foreign_player=player.is_foreign_player,
                )
            )
        player_ids = list(seen)
        if not player_ids:
            return TeamAnalysisFacts(roster=[], fielder_usage=[], pitcher_usage=[], moves=[], related_stints=[])

        # 野手: 明細の行がチームを持つので、そのチームの出場だけを数える。
        # スタメンの意味は GameBatting.is_starter（交代の順が0）と同じ
        fielder_rows = (
            orm_models.GameBattingLine.objects.filter(team_id=team_id, game__year=year, player_id__in=player_ids)
            .values("player_id", "fielding_position")
            .annotate(games=Count("id"), starts=Count("id", filter=Q(slot_sequence=0)))
            .order_by()
        )
        # 投手: 投球の明細はチームを持たない。そのチームの試合（ホームかビジター）で、
        # その年に在籍した選手の登板を数える近似（`DjangoPlayerFieldingQuery` の年度別と同じ）。
        # 先発の意味は pitching_totals と同じ（登板順が1以下）
        pitcher_rows = (
            orm_models.GamePitchingLine.objects.filter(
                Q(game__home_team_id=team_id) | Q(game__away_team_id=team_id),
                game__year=year,
                player_id__in=player_ids,
            )
            .values("player_id")
            .annotate(games=Count("id"), starts=Count("id", filter=Q(appearance_order__lte=1)))
            .order_by()
        )
        moves, related = self._moves(team_id, year)
        return TeamAnalysisFacts(
            roster=roster,
            fielder_usage=[
                FielderUsage(
                    player_id=row["player_id"],
                    position=FieldingPosition.from_label(row["fielding_position"]),
                    games=row["games"],
                    starts=row["starts"],
                )
                for row in fielder_rows
            ],
            pitcher_usage=[
                PitcherUsage(player_id=row["player_id"], games=row["games"], starts=row["starts"])
                for row in pitcher_rows
            ],
            moves=moves,
            related_stints=related,
        )
