"""アプリケーションサービス（ユースケース）。

「集約を読む → ドメインに操作させる → 保存する」という手順のみを担う。
背番号の重複判定や指標の計算といった業務ルールはドメイン層にあり、ここには無い。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date

from django.db import transaction

from ..domain import services as domain_services
from ..domain.entities import Game, Player, Stint, Team
from ..domain.value_objects import (
    JerseyNumber,
    Position,
    Season,
    ensure_quota_not_exceeded,
    format_average,
)
from .dto import (
    AdminOverview,
    BatterRow,
    CareerRow,
    Dashboard,
    DashboardLeague,
    GameDetail,
    GameLineScore,
    GamePlayerRow,
    GameRow,
    GameTeamBox,
    InningScoreColumn,
    LeagueDetail,
    LeaguePlayerRow,
    LeagueRankings,
    LeagueStandings,
    LeagueStats,
    LeagueTeams,
    LeagueTitles,
    Listing,
    MatchupCell,
    MatchupColumn,
    MatchupRow,
    MatchupTable,
    MonthlyRow,
    PitcherRow,
    PlayerDetail,
    PlayerGameRow,
    PlayerProfile,
    RankingEntry,
    StandingRow,
    Standings,
    TeamMonthlyRow,
    TeamTotals,
    TitleDepartment,
)


def _record_label(record) -> str:
    """勝敗を「3-1-1」（勝-敗-分）の1行で表す。

    対戦成績はチーム数ぶんの列が並ぶため、「3勝1敗1分」では表が横に伸びる。
    引分が0でも省かない。列によって桁数が変わると読み違えるため。
    """
    return f"{record.wins}-{record.losses}-{record.ties}"


# ランキングの値の書き方。率は小数3桁（打率は「.333」形式）、防御率は2桁、
# 本数は整数。ダッシュボードとタイトル一覧で同じ書式を使う
def _as_average(value: float) -> str:
    """打率・出塁率の形式。野球慣例の「.333」で表す。"""
    return format_average(value)


def _as_rate(value: float) -> str:
    """防御率のように小数2桁で読む値。"""
    return f"{value:.2f}"


def _as_count(value: float) -> str:
    """本塁打・奪三振のように本数そのものが記録になる値。"""
    return str(int(value))


def _decision_label(line) -> str:
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


@dataclass(frozen=True)
class _LeagueContext:
    """リーグ全体の成績から決まる、個々の成績を評価するための基準値。

    FIP の定数と、OPS+・ERA+ の基準になるリーグ平均。いずれもリーグ全体を
    見ないと決まらない値で、1回の要求の中で何度も参照されるため
    リーグ単位でまとめて求めて覚えておく。
    """

    fip_constant: float
    average_ops: float
    average_era: float


_EMPTY_LEAGUE_CONTEXT = _LeagueContext(fip_constant=0.0, average_ops=0.0, average_era=0.0)


class TeamApplicationService:
    """チームとロスターに関するユースケース。"""

    def __init__(self, teams, team_list_query=None, games=None, leagues=None):
        # 具象クラスではなくリポジトリのインターフェースに依存する
        self._teams = teams
        # 一覧表示は集約を組み立てないリードモデルを使う
        self._team_list_query = team_list_query
        # 勝敗と通算成績の出典
        self._games = games
        # 順位はリーグの中で決まるため、リーグの一覧が要る
        self._leagues = leagues
        # リーグの基準値（FIP 定数・OPS+/ERA+ の平均）は何度も引くため覚えておく
        self._league_contexts: dict[int, _LeagueContext] = {}

    # --- 参照系 ---

    # チーム一覧の並べ替え。業務ルールではなく表示上の都合なのでここに置く。
    # 既定は管理画面で設定した手動の表示順（DTO の並びそのまま）。
    TEAM_SORT_KEYS = {
        "name": (lambda t: t.name, False),
        "league": (lambda t: (t.league_name, t.name), False),
        # 未設定は末尾に来るよう、比較の最後に並ぶ文字を使う
        "stadium": (lambda t: (t.stadium_name or "￿", t.name), False),
        "players": (lambda t: t.player_count, True),
    }

    def list_teams(self, *, sort: str | None = None, descending: bool | None = None) -> Listing:
        """チームの平坦な一覧。絞り込みの選択肢などに使う。"""
        rows = self._team_list_query.list_summaries()

        if sort not in self.TEAM_SORT_KEYS:
            # 既定は手動の表示順。並べ替えずにそのまま返す
            return Listing(rows=rows, sort="order", descending=False)

        getter, default_desc = self.TEAM_SORT_KEYS[sort]
        desc = default_desc if descending is None else bool(descending)
        return Listing(rows=sorted(rows, key=getter, reverse=desc), sort=sort, descending=desc)

    def list_teams_by_league(self, *, sort: str | None = None, descending: bool | None = None) -> Listing:
        """リーグごとに分けたチーム一覧。

        チームはリーグに所属して戦うので、一覧もその単位で見せる。
        並べ替えはリーグの中で効く。所属チームが1つも無いリーグは出さない。
        """
        listing = self.list_teams(sort=sort, descending=descending)

        grouped: dict[int, list] = {}
        for team in listing.rows:
            grouped.setdefault(team.league_id, []).append(team)

        groups = [
            LeagueTeams(league_id=league.id, league_name=league.name, teams=grouped[league.id])
            for league in self._leagues.find_all()
            if grouped.get(league.id)
        ]
        return Listing(rows=groups, sort=listing.sort, descending=listing.descending)

    def get_dashboard(self, *, leaders: int = 5) -> Dashboard:
        """ホーム画面の概況と、リーグごとのランキング・順位表を組み立てる。

        順位づけの規則そのものはドメインサービスに委ね、ここでは
        集約をまたいで選手を集め、DTO に詰め替えるだけにとどめる。
        """
        teams = self._teams.find_all_with_roster()
        team_name_by_id = {team.id: team.name for team in teams}

        players: list[tuple[Player, int]] = [(player, team.id) for team in teams for player in team.active_players]
        team_of = {id(player): team_id for player, team_id in players}
        all_players = [player for player, _ in players]

        def to_entries(ranked, formatter) -> list[RankingEntry]:
            return [
                RankingEntry(
                    rank=item.rank,
                    player_id=item.player.id,
                    player_name=item.player.name,
                    team_id=team_of[id(item.player)],
                    team_name=team_name_by_id[team_of[id(item.player)]],
                    value=formatter(item.value),
                )
                for item in ranked
            ]

        # 規定打席・規定投球回は所属チームの試合数で決まる
        games_played = self._team_game_counts()
        team_games = {player.id: games_played.get(team_id, 0) for player, team_id in players}

        all_games = self._games.find_all()
        teams_by_league = {group.league_id: group.teams for group in self.list_teams_by_league().rows}

        leagues = []
        for league in self._leagues.find_all():
            league_teams = [t for t in teams if t.league_id == league.id]
            if not league_teams:
                # チームの無いリーグは切り替えても何も出せないので、タブを作らない
                continue
            standings_rows, standings_year = self._latest_standings(league_teams, all_games)
            leagues.append(
                DashboardLeague(
                    league_id=league.id,
                    league_name=league.name,
                    rankings=self._league_rankings(league_teams, leaders, team_games, to_entries),
                    standings=standings_rows,
                    standings_year=standings_year,
                    teams=teams_by_league.get(league.id, []),
                )
            )

        return Dashboard(
            league_count=len({team.league_id for team in teams}),
            team_count=len(teams),
            batter_count=sum(1 for p in all_players if not p.is_pitcher),
            pitcher_count=sum(1 for p in all_players if p.is_pitcher),
            leagues=leagues,
        )

    @staticmethod
    def _league_rankings(league_teams, leaders, team_games, to_entries) -> LeagueRankings:
        """1リーグぶんのランキング。

        タイトルはリーグの中で争われるので、他リーグの選手と同じ表に並べない。
        部門は NPB の個人成績ページにならい、打者は打率・本塁打・打点、
        投手は防御率・勝利・セーブを出す。
        """
        members = [p for team in league_teams for p in team.active_players]
        return LeagueRankings(
            average_leaders=to_entries(
                domain_services.leaders_by_batting_average(members, limit=leaders, team_games=team_games),
                _as_average,
            ),
            # 本塁打・打点・勝利・セーブは数そのものが記録なので規定を設けない
            home_run_leaders=to_entries(domain_services.leaders_by_home_runs(members, limit=leaders), _as_count),
            rbi_leaders=to_entries(domain_services.leaders_by_runs_batted_in(members, limit=leaders), _as_count),
            era_leaders=to_entries(
                domain_services.leaders_by_era(members, limit=leaders, team_games=team_games),
                _as_rate,
            ),
            win_leaders=to_entries(domain_services.leaders_by_wins(members, limit=leaders), _as_count),
            save_leaders=to_entries(domain_services.leaders_by_saves(members, limit=leaders), _as_count),
        )

    def _latest_standings(self, league_teams, all_games) -> tuple[list[StandingRow], int | None]:
        """1リーグぶんの最新シーズンの順位表と、そのシーズンの年。

        ダッシュボードは概況なので年は選ばせず、最新シーズンだけを出す。
        年をさかのぼる場合は順位表ページが受け持つ。
        """
        member_ids = {t.id for t in league_teams}
        league_games = [g for g in all_games if g.home_team_id in member_ids and g.away_team_id in member_ids]
        seasons = domain_services.seasons_of(league_games)
        if not seasons:
            return [], None

        latest = seasons[0]
        rows = domain_services.standings(league_teams, [g for g in league_games if g.season == latest])
        return self._to_standing_rows(rows, None, None), latest.year

    def get_team_name(self, team_id: int) -> str:
        return self._teams.find_by_id(team_id).name

    def list_batters(self, team_id: int, *, sort: str | None = None, descending: bool | None = None) -> Listing:
        team = self._teams.find_by_id(team_id)
        batters = [p for p in team.active_players if not p.is_pitcher]
        players, key, desc = domain_services.sort_batters(batters, sort, descending)
        captain = team.current_captain
        context = self._league_context(team.league_id)
        return Listing(
            rows=[self._to_batter_row(p, is_captain=p is captain, league_context=context) for p in players],
            sort=key,
            descending=desc,
        )

    def list_pitchers(self, team_id: int, *, sort: str | None = None, descending: bool | None = None) -> Listing:
        team = self._teams.find_by_id(team_id)
        pitchers = [p for p in team.active_players if p.is_pitcher]
        players, key, desc = domain_services.sort_pitchers(pitchers, sort, descending)
        captain = team.current_captain
        context = self._league_context(team.league_id)
        return Listing(
            rows=[self._to_pitcher_row(p, is_captain=p is captain, league_context=context) for p in players],
            sort=key,
            descending=desc,
        )

    def get_player_detail(self, team_id: int, player_id: int) -> PlayerDetail:
        team = self._teams.find_by_id(team_id)
        return self._to_detail(team, team.find_player(player_id), self._league_context(team.league_id))

    # 順位表の並べ替え。順位そのものは勝率から決まるので、ここでの並べ替えは
    # 表示順を変えるだけで rank の値は変えない。
    STANDING_SORT_KEYS = {
        "rank": (lambda r: r.rank, False),
        "team": (lambda r: r.team_name, False),
        "games": (lambda r: r.games_played, True),
        "wins": (lambda r: r.wins, True),
        "losses": (lambda r: r.losses, True),
        "ties": (lambda r: r.ties, True),
        "pct": (lambda r: r.rank, False),  # 勝率順＝順位順
    }

    def get_standings(
        self, year: int | None = None, *, sort: str | None = None, descending: bool | None = None
    ) -> Standings:
        """指定シーズンの順位表を返す。年を省略した場合は最新シーズン。

        順位づけの規則そのものはドメインサービスにあり、ここでは
        表示用に整形するだけにとどめる。
        """
        teams = self._teams.find_all_with_roster()
        all_games = self._games.find_all()
        seasons = domain_services.seasons_of(all_games)

        if not seasons:
            return Standings(year=year or 0, leagues=[], available_years=[])

        target = Season(year) if year is not None else seasons[0]
        season_games = [g for g in all_games if g.season == target]

        leagues = []
        for league in self._leagues.find_all():
            members = [t for t in teams if t.league_id == league.id]
            rows = domain_services.standings(members, season_games)
            if not rows:
                continue
            leagues.append(
                LeagueStandings(
                    league_id=league.id,
                    league_name=league.name,
                    rows=self._to_standing_rows(rows, sort, descending),
                )
            )

        return Standings(
            year=target.year,
            leagues=leagues,
            available_years=[s.year for s in seasons],
            sort=sort if sort in self.STANDING_SORT_KEYS else "rank",
            descending=bool(descending) if sort in self.STANDING_SORT_KEYS else False,
        )

    def _to_standing_rows(self, rows, sort, descending) -> list[StandingRow]:
        """ドメインの順位表を表示用に整え、必要なら並べ替える。

        並べ替えても rank の値は動かさない。順位は勝率で決まっているため。
        """

        display_rows = [
            StandingRow(
                rank=row.rank,
                team_id=row.team_id,
                team_name=row.team_name,
                wins=row.record.wins,
                losses=row.record.losses,
                ties=row.record.ties,
                games_played=row.record.games_played,
                winning_percentage=format_average(row.record.winning_percentage),
                games_behind="—" if row.is_leader else f"{row.games_behind:.1f}",
            )
            for row in rows
        ]

        if sort in self.STANDING_SORT_KEYS:
            getter, default_desc = self.STANDING_SORT_KEYS[sort]
            desc = default_desc if descending is None else bool(descending)
            display_rows = sorted(display_rows, key=getter, reverse=desc)

        return display_rows

    def get_league_detail(self, league_id: int, year: int | None = None) -> LeagueDetail:
        """リーグ画面。所属チーム・順位表・直近の試合をまとめて返す。"""
        league = self._leagues.find_by_id(league_id)
        teams = [t for t in self._teams.find_all_with_roster() if t.league_id == league_id]
        member_ids = {t.id for t in teams}

        all_games = self._games.find_all()
        league_games = [g for g in all_games if g.home_team_id in member_ids and g.away_team_id in member_ids]
        seasons = domain_services.seasons_of(league_games)

        target = None
        rows = []
        if seasons:
            target = Season(year) if year is not None else seasons[0]
            rows = self._to_standing_rows(
                domain_services.standings(teams, [g for g in league_games if g.season == target]),
                None,
                None,
            )

        names = self._team_names()
        recent = sorted(
            (self._to_game_row(g, names) for g in league_games),
            key=lambda r: (r.played_on, r.id),
            reverse=True,
        )[:10]

        summaries = {s.id: s for s in self._team_list_query.list_summaries()}

        matchups = None
        if target is not None:
            matchups = self._to_matchup_table(
                domain_services.matchups(teams, [g for g in league_games if g.season == target])
            )

        return LeagueDetail(
            id=league.id,
            name=league.name,
            year=target.year if target else None,
            available_years=[s.year for s in seasons],
            teams=[summaries[t.id] for t in teams if t.id in summaries],
            standings=rows,
            recent_games=recent,
            matchups=matchups,
        )

    def get_league_titles(self, league_id: int, year: int | None = None, *, leaders: int = 10) -> LeagueTitles:
        """リーグのタイトル一覧。シーズンで区切った部門別の上位者。

        ダッシュボードのランキングは通算成績だが、タイトルはシーズンごとに
        争われるので、こちらは対象シーズンの試合だけから成績を積み直す。
        """
        league = self._leagues.find_by_id(league_id)
        teams = [t for t in self._teams.find_all_with_roster() if t.league_id == league_id]
        member_ids = {t.id for t in teams}

        league_games = [
            g for g in self._games.find_all() if g.home_team_id in member_ids and g.away_team_id in member_ids
        ]
        seasons = domain_services.seasons_of(league_games)

        if not seasons:
            return LeagueTitles(
                league_id=league.id,
                league_name=league.name,
                year=None,
                available_years=[],
                departments=[],
            )

        target = Season(year) if year is not None else seasons[0]
        season_games = [g for g in league_games if g.season == target]

        # そのシーズンの成績だけを持つ選手に組み替える。通算値のままでは
        # 別のシーズンの記録まで混ざってタイトルの対象にならない
        games_played: dict[int, int] = {}
        for game in season_games:
            for team_id in (game.home_team_id, game.away_team_id):
                games_played[team_id] = games_played.get(team_id, 0) + 1

        players, team_of, team_games = [], {}, {}
        for team in teams:
            for player in team.active_players:
                scoped = replace(
                    player,
                    batting=domain_services.player_batting_total(season_games, player.id),
                    pitching=domain_services.player_pitching_total(season_games, player.id),
                )
                players.append(scoped)
                team_of[scoped.id] = (team.id, team.name)
                team_games[scoped.id] = games_played.get(team.id, 0)

        return LeagueTitles(
            league_id=league.id,
            league_name=league.name,
            year=target.year,
            available_years=[s.year for s in seasons],
            departments=self._title_departments(players, team_of, team_games, leaders),
        )

    @staticmethod
    def _title_departments(players, team_of, team_games, leaders) -> list[TitleDepartment]:
        """部門ごとの上位者。

        率の部門（打率・防御率）は規定に達した選手だけを対象にする。
        本数そのものが記録になる部門（本塁打・打点・奪三振）は規定を設けない。
        """

        def to_entries(ranked, formatter) -> list[RankingEntry]:
            rows = []
            for item in ranked:
                team_id, team_name = team_of[item.player.id]
                rows.append(
                    RankingEntry(
                        rank=item.rank,
                        player_id=item.player.id,
                        player_name=item.player.name,
                        team_id=team_id,
                        team_name=team_name,
                        value=formatter(item.value),
                    )
                )
            return rows

        return [
            TitleDepartment(
                key="average",
                label="首位打者",
                note="規定打席以上",
                entries=to_entries(
                    domain_services.leaders_by_batting_average(players, limit=leaders, team_games=team_games),
                    _as_average,
                ),
            ),
            TitleDepartment(
                key="home_runs",
                label="本塁打王",
                entries=to_entries(domain_services.leaders_by_home_runs(players, limit=leaders), _as_count),
            ),
            TitleDepartment(
                key="rbi",
                label="打点王",
                entries=to_entries(
                    domain_services.leaders_by_runs_batted_in(players, limit=leaders),
                    _as_count,
                ),
            ),
            TitleDepartment(
                key="era",
                label="最優秀防御率",
                note="規定投球回以上",
                entries=to_entries(
                    domain_services.leaders_by_era(players, limit=leaders, team_games=team_games),
                    _as_rate,
                ),
            ),
            TitleDepartment(
                key="wins",
                label="最多勝利",
                entries=to_entries(domain_services.leaders_by_wins(players, limit=leaders), _as_count),
            ),
            TitleDepartment(
                key="saves",
                label="最多セーブ",
                entries=to_entries(domain_services.leaders_by_saves(players, limit=leaders), _as_count),
            ),
            TitleDepartment(
                key="strikeouts",
                label="最多奪三振",
                entries=to_entries(domain_services.leaders_by_strikeouts(players, limit=leaders), _as_count),
            ),
        ]

    def get_league_stats(
        self,
        league_id: int,
        *,
        pitchers: bool = False,
        sort: str | None = None,
        descending: bool | None = None,
    ) -> LeagueStats:
        """リーグの成績一覧。所属する全選手の通算成績を1つの表に並べる。

        ダッシュボードのランキング（通算の上位だけ）から全体を確認しに来る
        場所なので、こちらも通算で揃える。並べ替えの規則はドメイン側にあり、
        不正なキーは既定の並びに落ちる。
        """
        league = self._leagues.find_by_id(league_id)
        teams = [t for t in self._teams.find_all_with_roster() if t.league_id == league_id]
        context = self._league_context(league_id)

        members: list[Player] = []
        home_of: dict[int, tuple[int, str]] = {}
        captains: set[int] = set()
        for team in teams:
            captain = team.current_captain
            for player in team.active_players:
                if player.is_pitcher != pitchers:
                    continue
                members.append(player)
                home_of[id(player)] = (team.id, team.name)
                if player is captain:
                    captains.add(id(player))

        sorter = domain_services.sort_pitchers if pitchers else domain_services.sort_batters
        ordered, key, desc = sorter(members, sort, descending)
        to_row = self._to_pitcher_row if pitchers else self._to_batter_row

        rows = []
        for player in ordered:
            team_id, team_name = home_of[id(player)]
            rows.append(
                LeaguePlayerRow(
                    team_id=team_id,
                    team_name=team_name,
                    player=to_row(player, is_captain=id(player) in captains, league_context=context),
                )
            )

        return LeagueStats(
            league_id=league.id,
            league_name=league.name,
            listing=Listing(rows=rows, sort=key, descending=desc),
        )

    @staticmethod
    def _to_matchup_table(rows) -> MatchupTable:
        """ドメインの対戦成績を表示用の表に整える。

        行と列を同じ順（順位表の順）に並べる。こうすると対角線が自分自身に
        なり、どのマスが誰と誰の対戦かを迷わず読める。
        """
        columns = [MatchupColumn(team_id=row.team_id, team_name=row.team_name) for row in rows]

        table_rows = []
        for row in rows:
            cells = []
            for column in columns:
                record = row.record_against(column.team_id)
                if record is None:
                    cells.append(MatchupCell(opponent_id=None, label="—", is_self=True))
                    continue
                cells.append(
                    MatchupCell(
                        opponent_id=column.team_id,
                        label=_record_label(record),
                        is_winning=record.wins > record.losses,
                        is_losing=record.losses > record.wins,
                    )
                )
            table_rows.append(
                MatchupRow(
                    team_id=row.team_id,
                    team_name=row.team_name,
                    cells=cells,
                    total_label=_record_label(row.total),
                )
            )

        return MatchupTable(columns=columns, rows=table_rows)

    # --- 試合の参照 ---

    def list_games(self, *, year: int | None = None, team_id: int | None = None) -> Listing:
        """試合の一覧。新しい順。"""
        games = self._games.find_by_team(team_id, year) if team_id is not None else self._games.find_all(year)
        names = self._team_names()
        rows = [self._to_game_row(g, names) for g in games]
        rows.sort(key=lambda r: (r.played_on, r.id), reverse=True)
        return Listing(rows=rows, sort="date", descending=True)

    def list_game_seasons(self) -> list[int]:
        return [s.year for s in domain_services.seasons_of(self._games.find_all())]

    def get_game_detail(self, game_id: int) -> GameDetail:
        """試合詳細。ボックススコアの形（チームごと・打順の順）で返す。"""
        game = self._games.find_by_id(game_id)
        names = self._team_names()
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
                    player_name=info["name"],
                    number=info["number"],
                    team_id=info["team_id"],
                    team_name=names.get(info["team_id"], ""),
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
                    career_batting_average=info["batting_average"],
                    batting_order=entry.batting_order,
                    slot_sequence=entry.slot_sequence,
                    position_label=entry.position_label,
                )
            )

        pitching = []
        for entry in game.pitching_in_order():
            info = players.get(entry.player_id)
            if info is None:
                continue
            line = entry.line
            pitching.append(
                GamePlayerRow(
                    player_id=entry.player_id,
                    player_name=info["name"],
                    number=info["number"],
                    team_id=info["team_id"],
                    team_name=names.get(info["team_id"], ""),
                    innings_pitched=str(line.innings),
                    earned_runs=line.earned_runs,
                    strikeouts=line.strikeouts,
                    hits_allowed=line.hits_allowed,
                    walks_allowed=line.walks_allowed,
                    hit_by_pitch_allowed=line.hit_by_pitch_allowed,
                    home_runs_allowed=line.home_runs_allowed,
                    earned_run_average=line.earned_run_average,
                    career_earned_run_average=info["earned_run_average"],
                    appearance_order=entry.appearance_order,
                    decision=_decision_label(line),
                )
            )

        return GameDetail(
            game=self._to_game_row(game, names),
            batting=batting,
            pitching=pitching,
            line_score=self._to_line_score(game, batting),
            away_box=self._to_team_box(game.away_team_id, names, batting, pitching, game),
            home_box=self._to_team_box(game.home_team_id, names, batting, pitching, game),
        )

    @staticmethod
    def _to_team_box(team_id, names, batting, pitching, game) -> GameTeamBox | None:
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
        )

    @staticmethod
    def _to_line_score(game, batting) -> GameLineScore | None:
        """スコアボード。回ごとの得点が記録されていなければ出さない。"""
        score = game.line_score
        if score.is_empty:
            return None

        columns = []
        for inning in range(1, score.innings + 1):
            away = str(score.runs_in(inning, home=False))
            # ホームが最終回を攻めずに終わった場合は 'X' を置く（記録の慣例）
            home = str(score.runs_in(inning, home=True)) if inning <= len(score.home) else "X"
            columns.append(InningScoreColumn(inning=inning, away=away, home=home))

        def hits_of(team_id: int) -> int:
            return sum(row.hits for row in batting if row.team_id == team_id)

        return GameLineScore(
            columns=columns,
            away_total=score.away_total,
            home_total=score.home_total,
            away_hits=hits_of(game.away_team_id),
            home_hits=hits_of(game.home_team_id),
        )

    def get_player_profile(self, team_id: int, player_id: int) -> PlayerProfile:
        """選手個人ページ。通算成績と、試合ごとの成績。"""
        detail = self.get_player_detail(team_id, player_id)
        names = self._team_names()
        team_games = self._games.find_by_team(team_id)

        rows = []
        for game in team_games:
            batting = next((e for e in game.batting if e.player_id == player_id), None)
            pitching = next((e for e in game.pitching if e.player_id == player_id), None)
            if batting is None and pitching is None:
                continue

            opponent_id = game.away_team_id if game.home_team_id == team_id else game.home_team_id
            rows.append(
                PlayerGameRow(
                    game_id=game.id,
                    played_on=game.played_on,
                    opponent_name=names.get(opponent_id, ""),
                    at_bats=batting.line.at_bats if batting else 0,
                    hits=batting.line.hits if batting else 0,
                    home_runs=batting.line.home_runs if batting else 0,
                    runs_batted_in=batting.line.runs_batted_in if batting else 0,
                    innings_pitched=str(pitching.line.innings) if pitching else "0.0",
                    earned_runs=pitching.line.earned_runs if pitching else 0,
                    strikeouts=pitching.line.strikeouts if pitching else 0,
                    decision=_decision_label(pitching.line) if pitching else "",
                )
            )

        rows.sort(key=lambda r: (r.played_on, r.game_id), reverse=True)

        player = self._teams.find_by_id(team_id).find_player(player_id)
        profile = player.profile

        return PlayerProfile(
            detail=detail,
            games=rows,
            months=[self._to_monthly_row(split) for split in domain_services.monthly_splits(team_games, player_id)],
            career=[
                CareerRow(
                    team_id=s.team_id,
                    team_name=s.team_name,
                    number=s.number.value,
                    from_year=s.from_year,
                    to_year=s.to_year,
                    is_current=s.is_current,
                )
                for s in player.career
            ],
            age=profile.age(date.today()),
            name_kana=profile.name_kana,
            back_name=profile.back_name,
            throws_bats=profile.throws_bats,
            height_cm=profile.height_cm,
            weight_kg=profile.weight_kg,
            birthplace=profile.birthplace,
            debut_year=profile.debut_year,
            amateur_career=profile.amateur_career,
            has_profile=not profile.is_empty,
        )

    @staticmethod
    def _to_monthly_row(split) -> MonthlyRow:
        """月別成績を表示用に整える。率は月ごとの合計から計算し直された値。"""
        batting, pitching = split.batting, split.pitching
        return MonthlyRow(
            label=split.label,
            appearances=split.appearances,
            at_bats=batting.at_bats,
            hits=batting.hits,
            home_runs=batting.home_runs,
            runs_batted_in=batting.runs_batted_in,
            batting_average=batting.batting_average,
            ops=batting.ops,
            innings_pitched=str(pitching.innings),
            earned_runs=pitching.earned_runs,
            strikeouts=pitching.strikeouts,
            earned_run_average=pitching.earned_run_average,
            whip=pitching.whip,
        )

    # --- 試合の登録 ---

    def get_game_edit_data(self, game_id: int) -> dict:
        """試合の編集画面に必要な材料をまとめて返す。

        両チームのロスターと、既に入力されている成績を対応づける。
        """
        game = self._games.find_by_id(game_id)
        names = self._team_names()

        batting = {e.player_id: e.line for e in game.batting}
        pitching = {e.player_id: e.line for e in game.pitching}
        lineup = {e.player_id: (e.batting_order, e.slot_sequence, e.fielding_position) for e in game.batting}
        entered = {e.player_id: e.entered_inning for e in game.pitching}

        rosters = []
        for team_id in (game.home_team_id, game.away_team_id):
            team = self._teams.find_by_id(team_id)
            rosters.append(
                {
                    "team_id": team_id,
                    "team_name": names.get(team_id, team.name),
                    "players": [
                        {
                            "id": p.id,
                            "name": p.name,
                            "number": p.number.value,
                            "position": p.position.label,
                            "is_pitcher": p.is_pitcher,
                            "batting": batting.get(p.id),
                            "pitching": pitching.get(p.id),
                            "lineup": lineup.get(p.id),
                            "entered_inning": entered.get(p.id),
                        }
                        for p in sorted(team.active_players, key=lambda p: p.number.value)
                    ],
                }
            )

        return {"game": game, "rosters": rosters}

    def create_game(self, *, year, played_on, home_team_id, away_team_id, home_score, away_score) -> Game:
        """試合を作る。成績は後から入力する。"""
        return self._games.save(
            Game(
                season=Season(year),
                played_on=played_on,
                home_team_id=home_team_id,
                away_team_id=away_team_id,
                home_score=home_score,
                away_score=away_score,
            )
        )

    def update_game(
        self,
        game_id: int,
        *,
        year,
        played_on,
        home_team_id,
        away_team_id,
        home_score,
        away_score,
        batting: dict | None = None,
        pitching: dict | None = None,
        lineup: dict | None = None,
        staff: dict | None = None,
        line_score=None,
    ) -> Game:
        """試合の基本情報と、出場選手の成績をまとめて更新する。

        batting / pitching は {選手id: ライン}。渡された辞書に含まれない選手の
        記録は取り消す（出場していない扱いに戻せるようにするため）。

        lineup は {選手id: (打順, 交代の順, 守備位置)}、staff は {選手id: 登板した回}。
        line_score があれば、勝敗・セーブ・ホールドは日本プロ野球の規則で導出して
        上書きする。手入力させないのは、規則から一意に決まるものを人が入れると
        記録どうしが食い違うため。
        """
        current = self._games.find_by_id(game_id)

        game = Game(
            id=current.id,
            season=Season(year),
            played_on=played_on,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
            home_score=home_score,
            away_score=away_score,
            line_score=line_score if line_score is not None else current.line_score,
        )
        game.ensure_line_score_matches()

        batting = batting or {}
        pitching = pitching or {}
        lineup = lineup or {}
        staff = staff or {}
        for player_id, line in batting.items():
            order, sequence, position = lineup.get(player_id, (None, 0, None))
            game.record_batting(
                player_id,
                line,
                batting_order=order,
                slot_sequence=sequence,
                fielding_position=position,
            )
        # 登板順は登板した回の順に振る。**チームごとに1から振る**（両チームの投手を
        # まとめて数えると、相手の先発が2番手になってしまう）
        entered = {pid: staff.get(pid, 1) for pid in pitching}
        players = self._player_index()
        # 選手索引に無い選手は team_id が None のまとまりに入る（現状の挙動を維持）
        by_team: dict[int | None, list[int]] = {}
        for player_id in sorted(entered, key=lambda pid: (entered[pid], pid)):
            team_id = players.get(player_id, {}).get("team_id")
            by_team.setdefault(team_id, []).append(player_id)

        for ordered in by_team.values():
            for order, player_id in enumerate(ordered, start=1):
                game.record_pitching(
                    player_id,
                    pitching[player_id],
                    appearance_order=order,
                    entered_inning=entered[player_id],
                )

        self._ensure_foreign_player_game_quota(game, batting, pitching)
        self._apply_pitching_decisions(game)
        return self._games.save(game)

    def _apply_pitching_decisions(self, game: Game) -> None:
        """勝敗・セーブ・ホールドをドメインの規則で決め、記録に反映する。

        イニングスコアが無い試合では判定できないので、そのまま残す。
        """
        if game.line_score.is_empty:
            return

        players = self._player_index()
        team_of = {pid: info["team_id"] for pid, info in players.items()}
        decisions = domain_services.pitching_decisions(game, team_of)

        for entry in game.pitching:
            line = entry.line
            wins = decisions.wins_for(entry.player_id)
            entry.line = replace(
                line,
                wins=wins,
                losses=decisions.losses_for(entry.player_id),
                saves=decisions.saves_for(entry.player_id),
                holds=decisions.holds_for(entry.player_id),
                starts=1 if entry.appearance_order == 1 else 0,
                relief_wins=wins if entry.appearance_order > 1 else 0,
            )

    def _ensure_foreign_player_game_quota(self, game: Game, batting: dict, pitching: dict) -> None:
        """1試合の出場選手（打撃または投球成績が記録される選手）のうち、
        外国人選手がチームごとの上限を超えていないか確認する。

        ホーム・ビジターはそれぞれ独立に判定する（合算しない）。
        """
        players = self._player_index()
        names = self._team_names()
        participant_ids = set(batting) | set(pitching)

        for team_id in (game.home_team_id, game.away_team_id):
            foreign_count = sum(
                1
                for pid in participant_ids
                if players.get(pid, {}).get("team_id") == team_id and players[pid]["is_foreign_player"]
            )
            league_id = self._teams.find_by_id(team_id).league_id
            limit = self._leagues.find_by_id(league_id).foreign_player_game_limit
            ensure_quota_not_exceeded(
                foreign_count,
                limit,
                f"「{names.get(team_id, '')}」の外国人選手出場人数（{foreign_count}人）が"
                f"上限（{limit}人）を超えています。",
            )

    def get_admin_overview(self) -> AdminOverview:
        """管理画面トップ用の概況。

        「成績が未入力か」の判定はドメインサービスの規定（打数0・投球回0を除く）を
        そのまま使う。ここで独自に条件を書くと、ランキングの対象と管理画面の
        警告がずれてしまう。
        """
        teams = self._teams.find_all_with_roster()
        active = [p for team in teams for p in team.active_players]
        retired = sum(1 for team in teams for p in team.players if not p.is_active)

        qualified = {id(p) for p in domain_services.qualified_batters(active)} | {
            id(p) for p in domain_services.qualified_pitchers(active)
        }

        return AdminOverview(
            league_count=len({team.league_id for team in teams}),
            team_count=len(teams),
            player_count=len(active),
            batter_count=sum(1 for p in active if not p.is_pitcher),
            pitcher_count=sum(1 for p in active if p.is_pitcher),
            players_without_stats=sum(1 for p in active if id(p) not in qualified),
            retired_count=retired,
            teams_without_players=sum(1 for team in teams if not team.active_players),
        )

    # --- 更新系 ---

    def register_player(self, team_id: int, name: str, number: int, position_label: str) -> Player:
        """新しい選手をロスターに加える。"""
        team = self._teams.find_by_id(team_id)
        player = team.add_player(
            name=name,
            number=JerseyNumber(number),
            position=Position.from_label(position_label),
        )
        self._teams.save(team)
        return player

    def update_player(self, team_id: int, player_id: int, *, name: str, number: int, position_label: str) -> Player:
        """選手の基本情報を更新する。

        成績は試合から集計するため、ここでは扱わない。
        """
        team = self._teams.find_by_id(team_id)
        player = team.find_player(player_id)

        player.rename(name)
        team.change_player_number(player, JerseyNumber(number))
        player.change_position(Position.from_label(position_label))

        self._teams.save(team)
        return player

    def retire_player(self, team_id: int, player_id: int, year: int | None = None) -> Player:
        """選手を退団させる。成績は残り、背番号は再利用できるようになる。"""
        team = self._teams.find_by_id(team_id)
        player = team.find_player(player_id)
        team.retire_player(player, year)
        self._teams.save(team)
        return player

    @transaction.atomic
    def transfer_player(
        self,
        player_id: int,
        *,
        from_team_id: int,
        to_team_id: int,
        number: int,
        year: int | None = None,
    ) -> None:
        """選手を移籍させる。元の在籍を閉じ、移籍先で新しい在籍を開く。

        成績は選手に紐づくため移籍しても失われない。経歴として
        「いつどのチームに居たか」が残る。

        検査がすべて終わってから保存する。途中で拒否された場合に、元チームだけ
        退団済みで移籍先には入らない、という中途半端な状態を残さないため。
        """
        season = year if year is not None else date.today().year

        source = self._teams.find_by_id(from_team_id)
        player = source.find_player(player_id)
        source.retire_player(player, season)

        destination = self._teams.find_by_id(to_team_id)
        destination._ensure_number_is_available(JerseyNumber(number))
        player.career.append(
            Stint(
                team_id=to_team_id,
                team_name=destination.name,
                number=JerseyNumber(number),
                from_year=season,
            )
        )
        player.number = JerseyNumber(number)
        player.is_active = True
        destination.players.append(player)

        league = self._leagues.find_by_id(destination.league_id)
        destination.ensure_foreign_player_quota(league.foreign_player_roster_limit)

        self._teams.save(source)
        self._teams.save(destination)

    def appoint_captain(self, team_id: int, player_id: int, year: int | None = None) -> Player:
        """選手をチームの主将に指名する。"""
        team = self._teams.find_by_id(team_id)
        player = team.find_player(player_id)
        team.appoint_captain(player, year)
        self._teams.save(team)
        return player

    def remove_captain(self, team_id: int, player_id: int, year: int | None = None) -> Player:
        """選手の主将を解任する。"""
        team = self._teams.find_by_id(team_id)
        player = team.find_player(player_id)
        team.remove_captain(player, year)
        self._teams.save(team)
        return player

    # --- 参照用の索引 ---

    def _team_game_counts(self, year: int | None = None) -> dict[int, int]:
        """チームごとの試合数。規定打席・規定投球回の基準になる。"""
        counts: dict[int, int] = {}
        for game in self._games.find_all(year):
            for team_id in (game.home_team_id, game.away_team_id):
                counts[team_id] = counts.get(team_id, 0) + 1
        return counts

    def get_team_totals(self, team_id: int) -> TeamTotals:
        """チームの打撃・投球の合計と、そこから求めた指標。"""
        team = self._teams.find_by_id(team_id)
        active = team.active_players
        batting = domain_services.team_batting(active)
        pitching = domain_services.team_pitching(active)
        games = self._team_game_counts().get(team_id, 0)

        return TeamTotals(
            games=games,
            batting_average=batting.batting_average,
            on_base_percentage=batting.on_base_percentage,
            slugging_percentage=batting.slugging_percentage,
            ops=batting.ops,
            home_runs=batting.home_runs,
            runs_batted_in=batting.runs_batted_in,
            earned_run_average=pitching.earned_run_average,
            whip=pitching.whip,
            strikeouts=pitching.strikeouts,
            innings_pitched=str(pitching.innings),
            required_plate_appearances=domain_services.required_plate_appearances(games),
            required_innings=f"{domain_services.required_outs(games) / 3:.1f}",
            fip=pitching.fip(self._league_context(team.league_id).fip_constant),
        )

    def list_team_monthly_splits(self, team_id: int) -> list[TeamMonthlyRow]:
        """チームの月別成績。個人の月別成績と対になる、チーム単位の推移。"""
        team = self._teams.find_by_id(team_id)
        member_ids = {p.id for p in team.players}

        return [
            TeamMonthlyRow(
                label=split.label,
                games_played=split.games_played,
                record_label=_record_label(split.record),
                winning_percentage=format_average(split.record.winning_percentage),
                batting_average=split.batting.batting_average,
                ops=split.batting.ops,
                home_runs=split.batting.home_runs,
                earned_run_average=split.pitching.earned_run_average,
                whip=split.pitching.whip,
                strikeouts=split.pitching.strikeouts,
            )
            for split in domain_services.team_monthly_splits(self._games.find_by_team(team_id), team_id, member_ids)
        ]

    def _league_context(self, league_id: int | None) -> _LeagueContext:
        """そのリーグの基準値。リーグ全体の成績から決まるので、リーグを
        知らなければ求められない（未登板と同じ、全て0の基準値を返す）。
        """
        if league_id is None:
            return _EMPTY_LEAGUE_CONTEXT
        if league_id not in self._league_contexts:
            members = [
                player
                for team in self._teams.find_all_with_roster()
                if team.league_id == league_id
                for player in team.active_players
            ]
            batting = domain_services.team_batting(members)
            pitching = domain_services.team_pitching(members)
            self._league_contexts[league_id] = _LeagueContext(
                fip_constant=domain_services.fip_constant(pitching),
                average_ops=batting.ops,
                average_era=pitching.earned_run_average,
            )
        return self._league_contexts[league_id]

    def _team_names(self) -> dict[int, str]:
        return {t.id: t.name for t in self._teams.find_all()}

    def _player_index(self) -> dict[int, dict]:
        """選手 id から名前・背番号・所属チーム・通算の率を引ける索引。"""
        index = {}
        for team in self._teams.find_all_with_roster():
            for player in team.players:
                index[player.id] = {
                    "name": player.name,
                    "number": player.number.value,
                    "team_id": team.id,
                    "is_foreign_player": player.profile.is_foreign_player,
                    # ボックススコアに並べる参考値。1試合の率は読めないため通算を出す
                    "batting_average": player.batting.batting_average,
                    "earned_run_average": player.pitching.earned_run_average,
                }
        return index

    @staticmethod
    def _to_game_row(game: Game, names: dict[int, str]) -> GameRow:
        assert game.id is not None, "一覧に載る試合は保存済み"
        winner = game.winner_team_id
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
            result="引分" if winner is None else f"{names.get(winner, '')} の勝ち",
            winner_team_id=winner,
        )

    # --- DTO への詰め替え ---

    @staticmethod
    def _to_batter_row(
        player: Player,
        *,
        is_captain: bool = False,
        league_context: _LeagueContext = _EMPTY_LEAGUE_CONTEXT,
    ) -> BatterRow:
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
            isolated_power=line.isolated_power,
            walks=line.walks,
            sacrifice_flies=line.sacrifice_flies,
            slugging_percentage=line.slugging_percentage,
            ops_plus=line.ops_plus(league_context.average_ops),
            is_captain=is_captain,
            is_foreign_player=profile.is_foreign_player,
            throws_bats=profile.throws_bats,
            height_cm=profile.height_cm,
            weight_kg=profile.weight_kg,
            age=profile.age(date.today()),
        )

    @staticmethod
    def _to_pitcher_row(
        player: Player,
        *,
        is_captain: bool = False,
        league_context: _LeagueContext = _EMPTY_LEAGUE_CONTEXT,
    ) -> PitcherRow:
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
            is_foreign_player=profile.is_foreign_player,
            throws_bats=profile.throws_bats,
            height_cm=profile.height_cm,
            weight_kg=profile.weight_kg,
            age=profile.age(date.today()),
        )

    @staticmethod
    def _to_detail(
        team: Team,
        player: Player,
        league_context: _LeagueContext = _EMPTY_LEAGUE_CONTEXT,
    ) -> PlayerDetail:
        assert player.id is not None and team.id is not None, "個人ページの選手・チームは保存済み"
        batting, pitching = player.batting, player.pitching
        return PlayerDetail(
            id=player.id,
            team_id=team.id,
            name=player.name,
            number=player.number.value,
            position=player.position.label,
            is_pitcher=player.is_pitcher,
            is_captain=team.current_captain is player,
            at_bats=batting.at_bats,
            singles=batting.singles,
            doubles=batting.doubles,
            triples=batting.triples,
            home_runs=batting.home_runs,
            runs_batted_in=batting.runs_batted_in,
            walks=batting.walks,
            hit_by_pitch=batting.hit_by_pitch,
            sacrifice_flies=batting.sacrifice_flies,
            innings_pitched=str(pitching.innings),
            wins=pitching.wins,
            losses=pitching.losses,
            saves=pitching.saves,
            earned_runs=pitching.earned_runs,
            strikeouts=pitching.strikeouts,
            hits_allowed=pitching.hits_allowed,
            walks_allowed=pitching.walks_allowed,
            home_runs_allowed=pitching.home_runs_allowed,
            hit_by_pitch_allowed=pitching.hit_by_pitch_allowed,
            batting_average=batting.batting_average,
            on_base_percentage=batting.on_base_percentage,
            slugging_percentage=batting.slugging_percentage,
            ops=batting.ops,
            earned_run_average=pitching.earned_run_average,
            whip=pitching.whip,
            strikeouts_per_nine=pitching.strikeouts_per_nine,
            isolated_power=batting.isolated_power,
            walks_per_nine=pitching.walks_per_nine,
            fip=pitching.fip(league_context.fip_constant),
            ops_plus=batting.ops_plus(league_context.average_ops),
            era_plus=pitching.era_plus(league_context.average_era),
            holds=pitching.holds,
            hold_points=pitching.hold_points,
            starts=pitching.starts,
        )
