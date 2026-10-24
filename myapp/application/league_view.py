"""リーグ・順位・タイトル・成績・チーム一覧の参照のユースケース。

読むだけで、書き込みはしない（ロスターの更新は `RosterService`、球団と選手の画面の参照は `TeamViewService`、
試合の参照は `GameViewService`。管理画面の概況は `admin.py` の `operator_overview`）。
指標の計算や並べ替えといった業務ルールはドメイン層にあり、ここには無い。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import date

from ..domain import services as domain_services
from ..domain.entities import Game, Player, Team
from ..domain.repositories import LeagueRepository, TeamRepository
from ..domain.services import Title
from ..domain.value_objects import (
    Season,
    format_average,
)
from .dto import (
    ActivePlayerStats,
    Dashboard,
    DashboardLeague,
    GameRow,
    LeagueDetail,
    LeagueOption,
    LeaguePlayerRow,
    LeagueStandings,
    LeagueStats,
    LeagueTeams,
    LeagueTitles,
    Listing,
    MatchupCell,
    MatchupColumn,
    MatchupRow,
    MatchupTable,
    RankingEntry,
    StandingRow,
    Standings,
    TitleDepartment,
)
from .ids import saved_id, team_names
from .league_context import LeagueContexts
from .player_stats_view import played_counts, stats_player
from .queries import GameListQuery, PlayerStatsQuery, TeamListQuery
from .standings import league_standings, standing_row_of
from .stat_rows import compact_record_label, to_batter_row, to_game_row, to_pitcher_row

# ランキングの値を画面の書式に直す関数（_as_average など）
ValueFormatter = Callable[[float], str]
# ダッシュボードに首位だけを出す部門。NPB の個人成績ページの主要部門（打率・本塁打・打点・
# 防御率・勝利・セーブ）にならう。残りの部門と上位3人・過去の年はタイトル画面が受け持つ
DASHBOARD_TITLE_KEYS = frozenset(
    title.value
    for title in (
        Title.AVERAGE,
        Title.HOME_RUNS,
        Title.RUNS_BATTED_IN,
        Title.EARNED_RUN_AVERAGE,
        Title.WINS,
        Title.SAVES,
    )
)
# タイトル画面に出す各部門の人数
TITLE_LEADERS = 3


def _to_ranking_entries(
    ranked: list[domain_services.RankedPlayer], team_of: dict[int, tuple[int, str]], formatter: ValueFormatter
) -> list[RankingEntry]:
    """順位づけの結果を画面向けの行にする。team_of は選手id → (チームid, チーム名)。"""
    entries = []
    for item in ranked:
        player_id = saved_id(item.player.id)
        team_id, team_name = team_of[player_id]
        entries.append(
            RankingEntry(
                rank=item.rank,
                player_id=player_id,
                player_name=item.player.name,
                team_id=team_id,
                team_name=team_name,
                value=formatter(item.value),
            )
        )
    return entries


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


class LeagueViewService:
    """リーグ・順位・タイトル・成績・チーム一覧の参照のユースケース。"""

    def __init__(
        self,
        teams: TeamRepository,
        team_list_query: TeamListQuery,
        leagues: LeagueRepository,
        game_list_query: GameListQuery,
        player_stats_query: PlayerStatsQuery,
        today: Callable[[], date],
    ) -> None:
        # 具象クラスではなくリポジトリ・参照クエリのインターフェースに依存する。
        # 省略可能にすると、一部だけ渡した半端なサービスが作れてしまい、呼ぶ経路に
        # よって「None に find_all は無い」で落ちる。そのため全部を必須にしている。
        self._teams = teams
        # 一覧表示は集約を組み立てないリードモデルを使う
        self._team_list_query = team_list_query
        self._game_list_query = game_list_query
        # ランキング・タイトルの材料（在籍中の選手の成績）は集約を組み立てずに読む
        self._player_stats_query = player_stats_query
        # 順位はリーグの中で決まるため、リーグの一覧が要る
        self._leagues = leagues
        # 年齢の基準日。実データは暦の今日、ペナントの世界はその世界の「今日」（組み立ての1か所が渡す）
        self._today = today
        # リーグの基準値（FIP 定数・OPS+/ERA+ の平均）は何度も引くため覚えておく
        self._league_contexts = LeagueContexts(player_stats_query)

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

        groups = []
        for league in self._leagues.find_all():
            members = grouped.get(saved_id(league.id))
            if members:
                groups.append(LeagueTeams(league_id=saved_id(league.id), league_name=league.name, teams=members))

        return Listing(rows=groups, sort=listing.sort, descending=listing.descending)

    def get_dashboard(self) -> Dashboard:
        """ホーム画面。全リーグの最新シーズンを1画面に並べる。

        リーグごとに順位表と主な部門の首位、それに全リーグの最新の試合日の結果。
        1リーグを掘り下げるのはリーグ画面、部門の上位3人と過去の年はタイトル画面が
        受け持つので、ここでは要約にとどめる。部門の首位はタイトル画面と同じ
        シーズン集計から取る（通算で別の数字を出すと、同じ部門名で値が食い違う）。
        """
        # チームは名前と所属リーグだけ使う。ロスターは作らない（成績は参照クエリが SQL で集計する）
        teams = self._teams.find_all()
        team_name_by_id = {saved_id(team.id): team.name for team in teams}

        # 順位表は得点だけで決まるので、明細を読まない一覧を使う
        # （集約の find_all() を呼ぶと全試合の打撃・投球まで読み込む）
        all_games = self._game_list_query.list_for_standings()
        teams_by_league = {group.league_id: group.teams for group in self.list_teams_by_league().rows}

        # 選手のシーズン成績と規定の基準になる球団の試合数は、年ごとに全リーグぶんをまとめて読む
        # （リーグごとに読むとリーグ数ぶんのクエリになる）。最新シーズンはリーグごとに決まるが、
        # ふつうはどのリーグも同じ年なので1回で済む
        season_stats: dict[int, dict[int, list[ActivePlayerStats]]] = {}
        season_game_counts: dict[int, dict[int, int]] = {}

        def stats_of(league_id: int, year: int) -> list[ActivePlayerStats]:
            if year not in season_stats:
                season_stats[year] = self._player_stats_query.list_season_by_league(year)
            return season_stats[year].get(league_id, [])

        def game_counts_of(year: int) -> dict[int, int]:
            if year not in season_game_counts:
                season_game_counts[year] = self._team_game_counts(year)
            return season_game_counts[year]

        leagues = []
        for league in self._leagues.find_all():
            league_id = saved_id(league.id)
            league_teams = [t for t in teams if t.league_id == league_id]
            if not league_teams:
                # チームの無いリーグは何も出せないので、カードを作らない
                continue
            # 順位表は「そのリーグ内の対戦」だけを見る（部門の首位はタイトル画面と同じく交流戦も数える）
            member_ids = {t.id for t in league_teams}
            league_games = [g for g in all_games if g.home_team_id in member_ids and g.away_team_id in member_ids]
            standings_rows, standings_year = self._latest_standings(league_teams, league_games)
            leaders: list[TitleDepartment] = []
            if standings_year is not None:
                # 順位表と同じシーズン。タイトル画面と同じ人数で順位づけてから1位だけを残す。
                # 1人で切ると、同率の首位が名前順の1人だけになり、タイトル画面と見え方が食い違う
                departments = self._season_titles(
                    stats_of(league_id, standings_year), game_counts_of(standings_year), leaders=TITLE_LEADERS
                )
                leaders = [
                    replace(d, entries=[e for e in d.entries or [] if e.rank == 1])
                    for d in departments
                    if d.key in DASHBOARD_TITLE_KEYS
                ]
            leagues.append(
                DashboardLeague(
                    league_id=league_id,
                    league_name=league.name,
                    standings=standings_rows,
                    standings_year=standings_year,
                    teams=teams_by_league.get(league_id, []),
                    leaders=leaders,
                )
            )

        league_order = {league.league_id: index for index, league in enumerate(leagues)}
        league_of_team = {saved_id(team.id): saved_id(team.league_id) for team in teams}
        return Dashboard(
            leagues=leagues,
            latest_results=self._latest_results(all_games, team_name_by_id, league_of_team, league_order),
        )

    def _latest_results(
        self,
        games: list[Game],
        names: dict[int, str],
        league_of_team: dict[int, int],
        league_order: dict[int, int],
    ) -> list[GameRow]:
        """全リーグの、最新の試合日の結果。リーグの並び順に並べる。

        件数で切ると、同じ日の試合の一部のリーグだけが残る（1日に全リーグで十数試合ある）。
        日で切れば、その日の全リーグの結果がそろう。結果を出す場所なので未記録の試合は除く
        （登録しただけの先の日付の試合で、行われた試合が押し出されないように）。
        """
        recorded = domain_services.recorded_games(games)
        if not recorded:
            return []

        latest = max(g.played_on for g in recorded)

        def order(game: Game) -> tuple[int, int]:
            # リーグをまたぐ対戦はホーム側のリーグに並べる。カードの無いリーグ（チームが無い）は末尾に回す
            return league_order.get(league_of_team.get(game.home_team_id, -1), len(league_order)), game.id or 0

        return [to_game_row(g, names) for g in sorted((g for g in recorded if g.played_on == latest), key=order)]

    def _latest_standings(
        self, league_teams: list[Team], league_games: list[Game]
    ) -> tuple[list[StandingRow], int | None]:
        """1リーグぶんの最新シーズンの順位表と、そのシーズンの年。

        league_games はそのリーグ内の対戦だけに絞ったもの（絞り込みは呼ぶ側が行う）。
        ダッシュボードは概況なので年は選ばせず、最新シーズンだけを出す。
        年をさかのぼる場合は順位表ページが受け持つ。
        """
        seasons = domain_services.seasons_of(league_games)
        if not seasons:
            return [], None

        latest = seasons[0]
        rows = domain_services.standings(league_teams, [g for g in league_games if g.season == latest])
        return self._to_standing_rows(rows, None, None), latest.year

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

        順位は得点と対戦カードだけで決まる。**ロスターも成績の明細も読まない**
        （どちらも件数ぶん重くなるだけで、順位には影響しない）。
        """
        teams = self._teams.find_all()
        # 年の選択肢は SQL で調べ、試合は対象の年だけ読む（全シーズンの試合を `Game` として組み立てない）
        seasons = self._game_list_query.list_seasons(recorded_only=True)

        if not seasons:
            return Standings(year=year or 0, leagues=[], available_years=[])

        target_year = Season(year).year if year is not None else seasons[0]
        season_games = self._game_list_query.list_for_standings(year=target_year)

        leagues = self._league_standings(teams, season_games, sort, descending)

        return Standings(
            year=target_year,
            leagues=leagues,
            available_years=seasons,
            sort=sort if sort in self.STANDING_SORT_KEYS else "rank",
            descending=bool(descending) if sort in self.STANDING_SORT_KEYS else False,
        )

    def get_league_standings(self, year: int, *, through: date | None = None) -> list[LeagueStandings]:
        """指定シーズンのリーグ別の順位だけを返す。**その年の試合だけを SQL で絞って読む。**

        年の選択肢（`get_standings` の `available_years`）が要らない呼び出し（世界の一覧の自軍の順位）用。
        全シーズンの試合を組み立てない。`through` を渡すと、その日までの試合だけで順位を作る
        （ペナントの「進める前の順位」を出すのに使う）。
        """
        games = self._game_list_query.list_for_standings(year=year)
        if through is not None:
            games = [game for game in games if game.played_on <= through]
        return self._league_standings(self._teams.find_all(), games, None, None)

    def _league_standings(
        self,
        teams: list[Team],
        season_games: list[Game],
        sort: str | None,
        descending: bool | None,
    ) -> list[LeagueStandings]:
        leagues = league_standings(teams, self._leagues.find_all(), season_games)
        if sort not in self.STANDING_SORT_KEYS:
            return leagues
        return [
            LeagueStandings(
                league_id=league.league_id,
                league_name=league.league_name,
                rows=self._sorted_standing_rows(league.rows, sort, descending),
            )
            for league in leagues
        ]

    def _to_standing_rows(
        self, rows: list[domain_services.StandingRow], sort: str | None, descending: bool | None
    ) -> list[StandingRow]:
        """ドメインの順位表を表示用に整え、必要なら並べ替える。

        並べ替えても rank の値は動かさない。順位は勝率で決まっているため。
        """

        display_rows = [standing_row_of(row) for row in rows]
        return self._sorted_standing_rows(display_rows, sort, descending)

    def _sorted_standing_rows(
        self, rows: list[StandingRow], sort: str | None, descending: bool | None
    ) -> list[StandingRow]:
        if sort in self.STANDING_SORT_KEYS:
            getter, default_desc = self.STANDING_SORT_KEYS[sort]
            desc = default_desc if descending is None else bool(descending)
            return sorted(rows, key=getter, reverse=desc)
        return rows

    def get_league_detail(self, league_id: int, year: int | None = None) -> LeagueDetail:
        """リーグ画面。所属チーム・順位表・直近の試合をまとめて返す。

        並べるのは順位・対戦成績・直近の試合だけなので、**ロスターも成績の明細も
        読まない**。所属チームの一覧は参照クエリの概要（TeamSummary）を使う。
        """
        league = self._leagues.find_by_id(league_id)
        teams = [t for t in self._teams.find_all() if t.league_id == league_id]
        member_ids = {t.id for t in teams}

        all_games = self._game_list_query.list_for_standings()
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

        names = team_names(self._teams)
        recent = sorted(
            (to_game_row(g, names) for g in league_games),
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
            id=saved_id(league.id),
            name=league.name,
            year=target.year if target else None,
            available_years=[s.year for s in seasons],
            teams=[summaries[t.id] for t in teams if t.id in summaries],
            standings=rows,
            recent_games=recent,
            matchups=matchups,
        )

    def get_league_titles(
        self, league_id: int, year: int | None = None, *, leaders: int | None = TITLE_LEADERS
    ) -> LeagueTitles:
        """リーグのタイトル一覧。シーズンで区切った部門別の上位者（既定は各部門3人まで。`leaders=None` なら全員）。

        タイトルはシーズンごとに争われるので、対象シーズンの試合だけから成績を積み直す。

        **成績に数える試合は、その年のすべての試合（リーグをまたぐ交流戦も含む。NPB と同じ）。**
        規定（規定打席・規定投球回）の基準になる試合数も、同じく交流戦を含む球団の試合数。
        対象の選手は、その年に球団に在籍していた選手（引退した選手も、在籍していた年のタイトルには残る）。

        **チームも試合も集約として組み立てない。** どの年が選べるかは SQL（`list_seasons`）で、
        規定の基準になる試合数は SQL の集計（`count_by_team`）で決め、選手の成績は参照クエリが
        対象シーズンの明細だけを SQL で集計する（集約を経由すると、経歴や数万行の明細まで
        組み立てて、応答の大半がそこで消えた。全シーズンの試合を `Game` として読むこともしない）。
        """
        league = self._leagues.find_by_id(league_id)
        seasons = self._game_list_query.list_seasons(league_id=league_id, recorded_only=True)

        if not seasons:
            return LeagueTitles(
                league_id=saved_id(league.id),
                league_name=league.name,
                year=None,
                available_years=[],
                departments=[],
            )

        target_year = Season(year).year if year is not None else seasons[0]

        return LeagueTitles(
            league_id=saved_id(league.id),
            league_name=league.name,
            year=target_year,
            available_years=seasons,
            # そのシーズンの成績だけを持つ選手を参照クエリに作らせる。通算値のままでは
            # 別のシーズンの記録まで混ざってタイトルの対象にならない。規定の基準になる試合数に、
            # 未記録の試合は数えない（`count_by_team` が記録済みだけを数える）
            departments=self._season_titles(
                self._player_stats_query.list_season(league_id, target_year),
                self._team_game_counts(target_year),
                leaders=leaders,
            ),
        )

    @classmethod
    def _season_titles(
        cls,
        stats_rows: list[ActivePlayerStats],
        games_played: dict[int, int],
        *,
        leaders: int | None,
    ) -> list[TitleDepartment]:
        """1リーグ×1シーズンの部門別の上位者。タイトル画面とダッシュボードの部門の首位の出典。

        stats_rows はそのリーグ・そのシーズンの選手の成績（`PlayerStatsQuery.list_season` の形）。
        games_played はそのシーズンの球団ごとの試合数（交流戦を含む。規定打席・規定投球回の基準）。
        """
        players = [stats_player(row) for row in stats_rows]
        team_of = {row.player_id: (row.team_id, row.team_name) for row in stats_rows}
        team_games = {row.player_id: games_played.get(row.team_id, 0) for row in stats_rows}
        return cls._title_departments(players, team_of, team_games, leaders)

    @staticmethod
    def _title_departments(
        players: list[Player],
        team_of: dict[int, tuple[int, str]],
        team_games: dict[int, int],
        leaders: int | None,
    ) -> list[TitleDepartment]:
        """部門ごとの上位者。NPB のタイトルのうち、成績だけで決まる部門をすべて並べる。

        部門の並び・名前・条件の表記は domain の `Title` が出典（歴代の列とペナントの表彰も同じ `Title` を読む）。
        率の部門（打率・出塁率・防御率）は規定に達した選手だけ、最高勝率は13勝以上の
        投手だけを対象にする（条件はドメインの順位づけが持つ）。本数そのものが記録になる
        部門（本塁打・打点・安打・盗塁・勝利・奪三振・セーブ・HP）は規定を設けない。
        投票や選考で決まる賞（MVP・新人王など）は成績からは決まらないので出さない。
        `leaders` が None なら部門の全員を並べる（ペナントの表彰が、同率の1位を全員拾うため）。
        """
        ranked: dict[Title, tuple[list[domain_services.RankedPlayer], ValueFormatter]] = {
            Title.AVERAGE: (
                domain_services.leaders_by_batting_average(players, limit=leaders, team_games=team_games),
                _as_average,
            ),
            Title.HOME_RUNS: (domain_services.leaders_by_home_runs(players, limit=leaders), _as_count),
            Title.RUNS_BATTED_IN: (domain_services.leaders_by_runs_batted_in(players, limit=leaders), _as_count),
            Title.HITS: (domain_services.leaders_by_hits(players, limit=leaders), _as_count),
            Title.STOLEN_BASES: (domain_services.leaders_by_stolen_bases(players, limit=leaders), _as_count),
            Title.ON_BASE_PERCENTAGE: (
                domain_services.leaders_by_on_base_percentage(players, limit=leaders, team_games=team_games),
                _as_average,
            ),
            Title.EARNED_RUN_AVERAGE: (
                domain_services.leaders_by_era(players, limit=leaders, team_games=team_games),
                _as_rate,
            ),
            Title.WINS: (domain_services.leaders_by_wins(players, limit=leaders), _as_count),
            Title.WINNING_PERCENTAGE: (
                domain_services.leaders_by_winning_percentage(players, limit=leaders),
                _as_average,
            ),
            Title.STRIKEOUTS: (domain_services.leaders_by_strikeouts(players, limit=leaders), _as_count),
            Title.SAVES: (domain_services.leaders_by_saves(players, limit=leaders), _as_count),
            Title.HOLD_POINTS: (domain_services.leaders_by_hold_points(players, limit=leaders), _as_count),
        }
        return [
            TitleDepartment(
                key=title.value,
                label=title.label,
                note=title.note,
                entries=_to_ranking_entries(ranked[title][0], team_of, ranked[title][1]),
                is_pitching=title.is_pitching,
            )
            # 並びは `Title` の定義順（打撃 → 投手）
            for title in Title
        ]

    def get_league_stats(
        self,
        league_id: int,
        *,
        pitchers: bool = False,
        qualified: bool = False,
        sort: str | None = None,
        descending: bool | None = None,
        year: int | None = None,
    ) -> LeagueStats:
        """リーグの成績一覧。所属する全選手の成績を1つの表に並べる。

        既定（`year` を省く）は通算で揃える（シーズンで区切った部門の上位はタイトル一覧、
        その首位だけはダッシュボードが受け持つ）。ペナントの世界は `year` を渡して、その年の成績（今季）も見せる。
        並べ替えの規則はドメイン側にあり、不正なキーは既定の並びに落ちる。

        qualified を立てると規定（規定打席・規定投球回）に到達した選手だけに
        絞る。規定の条件はドメインサービスが持つ（タイトルの対象と同じ規則で、
        画面側に別の条件を書かない）。切り替えの前に規模が分かるよう、
        到達した人数と全体の人数はどちらの状態でも返す。
        """
        league = self._leagues.find_by_id(league_id)
        # 集約（Team）を組み立てず、参照クエリがリーグの選手と成績をまとめて読む
        all_rows = self._player_stats_query.list_roster(
            year=year, league_id=league_id, with_profile=True, with_games=True
        )
        context = self._league_contexts.get(league_id, year, rows=all_rows)

        members: list[Player] = []
        home_of: dict[int, tuple[int, str]] = {}
        captains: set[int] = set()
        # 規定は所属チームの試合数で決まるため、選手ごとに引けるようにしておく
        games_played = self._team_game_counts(year)
        team_games: dict[int, int] = {}
        # 選手ごとの出場数（野手は出場試合数、投手は登板数）。列と並べ替えに使う
        played = played_counts([row for row in all_rows if row.position.is_pitcher == pitchers], pitchers=pitchers)
        for row in all_rows:
            if row.position.is_pitcher != pitchers:
                continue
            player = stats_player(row)
            members.append(player)
            home_of[id(player)] = (row.team_id, row.team_name)
            team_games[row.player_id] = games_played.get(row.team_id, 0)
            if row.is_captain:
                captains.add(id(player))

        reaching = (
            domain_services.qualified_pitchers(members, team_games=team_games)
            if pitchers
            else domain_services.qualified_batters(members, team_games=team_games)
        )

        listed = reaching if qualified else members
        if pitchers:
            ordered, key, desc = domain_services.sort_pitchers(listed, sort, descending, appearances=played)
        else:
            ordered, key, desc = domain_services.sort_batters(listed, sort, descending, games=played)
        to_row = to_pitcher_row if pitchers else to_batter_row
        today = self._today()

        rows = []
        for player in ordered:
            team_id, team_name = home_of[id(player)]
            rows.append(
                LeaguePlayerRow(
                    team_id=team_id,
                    team_name=team_name,
                    player=to_row(
                        player,
                        is_captain=id(player) in captains,
                        played=played.get(player.id or 0, 0),
                        league_context=context,
                        today=today,
                    ),
                )
            )

        return LeagueStats(
            league_id=saved_id(league.id),
            league_name=league.name,
            listing=Listing(rows=rows, sort=key, descending=desc),
            qualified=qualified,
            qualified_count=len(reaching),
            total_count=len(members),
        )

    @staticmethod
    def _to_matchup_table(rows: list[domain_services.MatchupRow]) -> MatchupTable:
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
                        label=compact_record_label(record),
                        is_winning=record.wins > record.losses,
                        is_losing=record.losses > record.wins,
                    )
                )
            table_rows.append(
                MatchupRow(
                    team_id=row.team_id,
                    team_name=row.team_name,
                    cells=cells,
                    total_label=compact_record_label(row.total),
                )
            )

        return MatchupTable(columns=columns, rows=table_rows)

    def list_leagues(self) -> list[LeagueOption]:
        """リーグの絞り込みに使う選択肢。表示順は管理画面で決めた順。"""
        return [LeagueOption(id=saved_id(league.id), name=league.name) for league in self._leagues.find_all()]

    # --- 参照用の索引 ---

    def _team_game_counts(self, year: int | None = None) -> dict[int, int]:
        """チームごとの試合数。規定打席・規定投球回の基準になる。

        数えるだけなので集約を組み立てず、参照クエリに SQL で数えさせる。
        """
        return self._game_list_query.count_by_team(year=year)
