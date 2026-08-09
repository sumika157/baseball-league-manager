"""アプリケーションサービス（ユースケース）。

「集約を読む → ドメインに操作させる → 保存する」という手順のみを担う。
背番号の重複判定や指標の計算といった業務ルールはドメイン層にあり、ここには無い。
"""

from __future__ import annotations

from datetime import date

from ..domain import services as domain_services
from ..domain.entities import Game, Player, Stint, Team
from ..domain.value_objects import (
    BattingLine,
    InningsPitched,
    JerseyNumber,
    PitchingLine,
    Position,
    Season,
    TeamRecord,
    format_average,
)
from .dto import (
    AdminOverview,
    BatterRow,
    CareerRow,
    Dashboard,
    GameDetail,
    GamePlayerRow,
    GameRow,
    LeagueDetail,
    LeagueStandings,
    LeagueTeams,
    Listing,
    PitcherRow,
    PlayerDetail,
    PlayerGameRow,
    PlayerProfile,
    RankingEntry,
    Standings,
    StandingRow,
    TeamSummary,
    TeamTotals,
)


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

    # --- 参照系 ---

    # チーム一覧の並べ替え。業務ルールではなく表示上の都合なのでここに置く。
    # 既定は管理画面で設定した手動の表示順（DTO の並びそのまま）。
    TEAM_SORT_KEYS = {
        'name': (lambda t: t.name, False),
        'league': (lambda t: (t.league_name, t.name), False),
        # 未設定は末尾に来るよう、比較の最後に並ぶ文字を使う
        'stadium': (lambda t: (t.stadium_name or '￿', t.name), False),
        'players': (lambda t: t.player_count, True),
    }

    def list_teams(self, *, sort: str = None, descending: bool = None) -> Listing:
        """チームの平坦な一覧。絞り込みの選択肢などに使う。"""
        rows = self._team_list_query.list_summaries()

        if sort not in self.TEAM_SORT_KEYS:
            # 既定は手動の表示順。並べ替えずにそのまま返す
            return Listing(rows=rows, sort='order', descending=False)

        getter, default_desc = self.TEAM_SORT_KEYS[sort]
        desc = default_desc if descending is None else bool(descending)
        return Listing(
            rows=sorted(rows, key=getter, reverse=desc), sort=sort, descending=desc
        )

    def list_teams_by_league(self, *, sort: str = None, descending: bool = None) -> Listing:
        """リーグごとに分けたチーム一覧。

        チームはリーグに所属して戦うので、一覧もその単位で見せる。
        並べ替えはリーグの中で効く。所属チームが1つも無いリーグは出さない。
        """
        listing = self.list_teams(sort=sort, descending=descending)

        grouped: dict[int, list] = {}
        for team in listing.rows:
            grouped.setdefault(team.league_id, []).append(team)

        groups = [
            LeagueTeams(
                league_id=league.id, league_name=league.name, teams=grouped[league.id]
            )
            for league in self._leagues.find_all()
            if grouped.get(league.id)
        ]
        return Listing(rows=groups, sort=listing.sort, descending=listing.descending)

    def get_dashboard(self, *, leaders: int = 5) -> Dashboard:
        """ホーム画面の概況とランキングを組み立てる。

        順位づけの規則そのものはドメインサービスに委ね、ここでは
        集約をまたいで選手を集め、DTO に詰め替えるだけにとどめる。
        """
        teams = self._teams.find_all_with_roster()
        team_name_by_id = {team.id: team.name for team in teams}

        players: list[tuple[Player, int]] = [
            (player, team.id) for team in teams for player in team.active_players
        ]
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

        rate3 = lambda v: f"{v:.3f}"
        rate2 = lambda v: f"{v:.2f}"
        count = lambda v: str(int(v))

        # 規定打席・規定投球回は所属チームの試合数で決まる
        games_played = self._team_game_counts()
        team_games = {
            player.id: games_played.get(team_id, 0)
            for player, team_id in players
        }

        return Dashboard(
            league_count=len({team.league_id for team in teams}),
            team_count=len(teams),
            batter_count=sum(1 for p in all_players if not p.is_pitcher),
            pitcher_count=sum(1 for p in all_players if p.is_pitcher),
            ops_leaders=to_entries(
                domain_services.leaders_by_ops(
                    all_players, limit=leaders, team_games=team_games
                ),
                rate3,
            ),
            # 本塁打は本数そのものが記録なので規定を設けない
            home_run_leaders=to_entries(
                domain_services.leaders_by_home_runs(all_players, limit=leaders), count
            ),
            era_leaders=to_entries(
                domain_services.leaders_by_era(
                    all_players, limit=leaders, team_games=team_games
                ),
                rate2,
            ),
            strikeout_leaders=to_entries(
                domain_services.leaders_by_strikeouts(all_players, limit=leaders), count
            ),
            teams=self._team_list_query.list_summaries(),
        )

    def get_team_name(self, team_id: int) -> str:
        return self._teams.find_by_id(team_id).name

    def list_batters(
        self, team_id: int, *, sort: str = None, descending: bool = None
    ) -> Listing:
        team = self._teams.find_by_id(team_id)
        batters = [p for p in team.active_players if not p.is_pitcher]
        players, key, desc = domain_services.sort_batters(batters, sort, descending)
        return Listing(
            rows=[self._to_batter_row(p) for p in players], sort=key, descending=desc
        )

    def list_pitchers(
        self, team_id: int, *, sort: str = None, descending: bool = None
    ) -> Listing:
        team = self._teams.find_by_id(team_id)
        pitchers = [p for p in team.active_players if p.is_pitcher]
        players, key, desc = domain_services.sort_pitchers(pitchers, sort, descending)
        return Listing(
            rows=[self._to_pitcher_row(p) for p in players], sort=key, descending=desc
        )

    def get_player_detail(self, team_id: int, player_id: int) -> PlayerDetail:
        team = self._teams.find_by_id(team_id)
        return self._to_detail(team, team.find_player(player_id))

    # 順位表の並べ替え。順位そのものは勝率から決まるので、ここでの並べ替えは
    # 表示順を変えるだけで rank の値は変えない。
    STANDING_SORT_KEYS = {
        'rank': (lambda r: r.rank, False),
        'team': (lambda r: r.team_name, False),
        'games': (lambda r: r.games_played, True),
        'wins': (lambda r: r.wins, True),
        'losses': (lambda r: r.losses, True),
        'ties': (lambda r: r.ties, True),
        'pct': (lambda r: r.rank, False),  # 勝率順＝順位順
    }

    def get_standings(
        self, year: int | None = None, *, sort: str = None, descending: bool = None
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
            leagues.append(LeagueStandings(
                league_id=league.id,
                league_name=league.name,
                rows=self._to_standing_rows(rows, sort, descending),
            ))

        return Standings(
            year=target.year,
            leagues=leagues,
            available_years=[s.year for s in seasons],
            sort=sort if sort in self.STANDING_SORT_KEYS else 'rank',
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
                games_behind='—' if row.is_leader else f'{row.games_behind:.1f}',
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
        league_games = [
            g for g in all_games
            if g.home_team_id in member_ids and g.away_team_id in member_ids
        ]
        seasons = domain_services.seasons_of(league_games)

        target = None
        rows = []
        if seasons:
            target = Season(year) if year is not None else seasons[0]
            rows = self._to_standing_rows(
                domain_services.standings(
                    teams, [g for g in league_games if g.season == target]
                ),
                None, None,
            )

        names = self._team_names()
        recent = sorted(
            (self._to_game_row(g, names) for g in league_games),
            key=lambda r: (r.played_on, r.id),
            reverse=True,
        )[:10]

        summaries = {s.id: s for s in self._team_list_query.list_summaries()}

        return LeagueDetail(
            id=league.id,
            name=league.name,
            year=target.year if target else None,
            available_years=[s.year for s in seasons],
            teams=[summaries[t.id] for t in teams if t.id in summaries],
            standings=rows,
            recent_games=recent,
        )

    # --- 試合の参照 ---

    def list_games(self, *, year: int | None = None, team_id: int | None = None) -> Listing:
        """試合の一覧。新しい順。"""
        games = (
            self._games.find_by_team(team_id, year)
            if team_id is not None
            else self._games.find_all(year)
        )
        names = self._team_names()
        rows = [self._to_game_row(g, names) for g in games]
        rows.sort(key=lambda r: (r.played_on, r.id), reverse=True)
        return Listing(rows=rows, sort='date', descending=True)

    def list_game_seasons(self) -> list[int]:
        return [s.year for s in domain_services.seasons_of(self._games.find_all())]

    def get_game_detail(self, game_id: int) -> GameDetail:
        """試合詳細。出場選手の成績を名前付きで返す。"""
        game = self._games.find_by_id(game_id)
        names = self._team_names()
        players = self._player_index()

        batting = []
        for entry in game.batting:
            info = players.get(entry.player_id)
            if info is None:
                continue
            line = entry.line
            batting.append(GamePlayerRow(
                player_id=entry.player_id,
                player_name=info['name'],
                number=info['number'],
                team_id=info['team_id'],
                team_name=names.get(info['team_id'], ''),
                at_bats=line.at_bats,
                hits=line.hits,
                home_runs=line.home_runs,
                runs_batted_in=line.runs_batted_in,
                walks=line.walks,
                batting_average=line.batting_average,
            ))

        pitching = []
        for entry in game.pitching:
            info = players.get(entry.player_id)
            if info is None:
                continue
            line = entry.line
            pitching.append(GamePlayerRow(
                player_id=entry.player_id,
                player_name=info['name'],
                number=info['number'],
                team_id=info['team_id'],
                team_name=names.get(info['team_id'], ''),
                innings_pitched=str(line.innings),
                earned_runs=line.earned_runs,
                strikeouts=line.strikeouts,
                hits_allowed=line.hits_allowed,
                earned_run_average=line.earned_run_average,
            ))

        batting.sort(key=lambda r: (r.team_name, r.number))
        pitching.sort(key=lambda r: (r.team_name, r.number))

        return GameDetail(
            game=self._to_game_row(game, names), batting=batting, pitching=pitching
        )

    def get_player_profile(self, team_id: int, player_id: int) -> PlayerProfile:
        """選手個人ページ。通算成績と、試合ごとの成績。"""
        detail = self.get_player_detail(team_id, player_id)
        names = self._team_names()

        rows = []
        for game in self._games.find_by_team(team_id):
            batting = next((e for e in game.batting if e.player_id == player_id), None)
            pitching = next((e for e in game.pitching if e.player_id == player_id), None)
            if batting is None and pitching is None:
                continue

            opponent_id = (
                game.away_team_id if game.home_team_id == team_id else game.home_team_id
            )
            rows.append(PlayerGameRow(
                game_id=game.id,
                played_on=game.played_on,
                opponent_name=names.get(opponent_id, ''),
                result={'win': '勝', 'loss': '敗', 'tie': '分'}[game.result_for(team_id)],
                at_bats=batting.line.at_bats if batting else 0,
                hits=batting.line.hits if batting else 0,
                home_runs=batting.line.home_runs if batting else 0,
                runs_batted_in=batting.line.runs_batted_in if batting else 0,
                innings_pitched=str(pitching.line.innings) if pitching else '0.0',
                earned_runs=pitching.line.earned_runs if pitching else 0,
                strikeouts=pitching.line.strikeouts if pitching else 0,
            ))

        rows.sort(key=lambda r: (r.played_on, r.game_id), reverse=True)

        player = self._teams.find_by_id(team_id).find_player(player_id)
        profile = player.profile

        return PlayerProfile(
            detail=detail,
            games=rows,
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
            throws_bats=profile.throws_bats,
            height_cm=profile.height_cm,
            weight_kg=profile.weight_kg,
            birthplace=profile.birthplace,
            debut_year=profile.debut_year,
            amateur_career=profile.amateur_career,
            has_profile=not profile.is_empty,
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

        rosters = []
        for team_id in (game.home_team_id, game.away_team_id):
            team = self._teams.find_by_id(team_id)
            rosters.append({
                'team_id': team_id,
                'team_name': names.get(team_id, team.name),
                'players': [
                    {
                        'id': p.id,
                        'name': p.name,
                        'number': p.number.value,
                        'position': p.position.label,
                        'is_pitcher': p.is_pitcher,
                        'batting': batting.get(p.id),
                        'pitching': pitching.get(p.id),
                    }
                    for p in sorted(team.active_players, key=lambda p: p.number.value)
                ],
            })

        return {'game': game, 'rosters': rosters}

    def create_game(
        self, *, year, played_on, home_team_id, away_team_id, home_score, away_score
    ) -> Game:
        """試合を作る。成績は後から入力する。"""
        return self._games.save(Game(
            season=Season(year),
            played_on=played_on,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
            home_score=home_score,
            away_score=away_score,
        ))

    def update_game(
        self, game_id: int, *,
        year, played_on, home_team_id, away_team_id, home_score, away_score,
        batting: dict = None, pitching: dict = None,
    ) -> Game:
        """試合の基本情報と、出場選手の成績をまとめて更新する。

        batting / pitching は {選手id: ライン}。渡された辞書に含まれない選手の
        記録は取り消す（出場していない扱いに戻せるようにするため）。
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
        )
        for player_id, line in (batting or {}).items():
            game.record_batting(player_id, line)
        for player_id, line in (pitching or {}).items():
            game.record_pitching(player_id, line)

        return self._games.save(game)

    def record_game(
        self,
        *,
        year: int,
        played_on,
        home_team_id: int,
        away_team_id: int,
        home_score: int,
        away_score: int,
    ) -> Game:
        """試合を登録する。勝敗はここから集計されるので、別途入力しない。"""
        game = Game(
            season=Season(year),
            played_on=played_on,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
            home_score=home_score,
            away_score=away_score,
        )
        return self._games.save(game)

    def get_admin_overview(self) -> AdminOverview:
        """管理画面トップ用の概況。

        「成績が未入力か」の判定はドメインサービスの規定（打数0・投球回0を除く）を
        そのまま使う。ここで独自に条件を書くと、ランキングの対象と管理画面の
        警告がずれてしまう。
        """
        teams = self._teams.find_all_with_roster()
        active = [p for team in teams for p in team.active_players]
        retired = sum(
            1 for team in teams for p in team.players if not p.is_active
        )

        qualified = {
            id(p) for p in domain_services.qualified_batters(active)
        } | {
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

    def register_player(
        self, team_id: int, name: str, number: int, position_label: str
    ) -> Player:
        """新しい選手をロスターに加える。"""
        team = self._teams.find_by_id(team_id)
        player = team.add_player(
            name=name,
            number=JerseyNumber(number),
            position=Position.from_label(position_label),
        )
        self._teams.save(team)
        return player

    def update_player(
        self, team_id: int, player_id: int, *, name: str, number: int, position_label: str
    ) -> Player:
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

    def retire_player(self, team_id: int, player_id: int, year: int = None) -> Player:
        """選手を退団させる。成績は残り、背番号は再利用できるようになる。"""
        team = self._teams.find_by_id(team_id)
        player = team.find_player(player_id)
        team.retire_player(player, year)
        self._teams.save(team)
        return player

    def transfer_player(
        self, player_id: int, *, from_team_id: int, to_team_id: int,
        number: int, year: int = None,
    ) -> None:
        """選手を移籍させる。元の在籍を閉じ、移籍先で新しい在籍を開く。

        成績は選手に紐づくため移籍しても失われない。経歴として
        「いつどのチームに居たか」が残る。
        """
        season = year if year is not None else date.today().year

        source = self._teams.find_by_id(from_team_id)
        player = source.find_player(player_id)
        source.retire_player(player, season)
        self._teams.save(source)

        destination = self._teams.find_by_id(to_team_id)
        destination._ensure_number_is_available(JerseyNumber(number))
        player.career.append(Stint(
            team_id=to_team_id,
            team_name=destination.name,
            number=JerseyNumber(number),
            from_year=season,
        ))
        player.number = JerseyNumber(number)
        player.is_active = True
        destination.players.append(player)
        self._teams.save(destination)

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
            required_innings=f'{domain_services.required_outs(games) / 3:.1f}',
        )

    def _team_names(self) -> dict[int, str]:
        return {t.id: t.name for t in self._teams.find_all()}

    def _player_index(self) -> dict[int, dict]:
        """選手 id から名前・背番号・所属チームを引ける索引。"""
        index = {}
        for team in self._teams.find_all_with_roster():
            for player in team.players:
                index[player.id] = {
                    'name': player.name,
                    'number': player.number.value,
                    'team_id': team.id,
                }
        return index

    @staticmethod
    def _to_game_row(game: Game, names: dict[int, str]) -> GameRow:
        winner = game.winner_team_id
        return GameRow(
            id=game.id,
            year=game.season.year,
            played_on=game.played_on,
            home_team_id=game.home_team_id,
            home_team_name=names.get(game.home_team_id, ''),
            away_team_id=game.away_team_id,
            away_team_name=names.get(game.away_team_id, ''),
            home_score=game.home_score,
            away_score=game.away_score,
            result='引分' if winner is None else f'{names.get(winner, "")} の勝ち',
            winner_team_id=winner,
        )

    # --- DTO への詰め替え ---

    @staticmethod
    def _to_batter_row(player: Player) -> BatterRow:
        line = player.batting
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
        )

    @staticmethod
    def _to_pitcher_row(player: Player) -> PitcherRow:
        line = player.pitching
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
        )

    @staticmethod
    def _to_detail(team: Team, player: Player) -> PlayerDetail:
        batting, pitching = player.batting, player.pitching
        return PlayerDetail(
            id=player.id,
            team_id=team.id,
            name=player.name,
            number=player.number.value,
            position=player.position.label,
            is_pitcher=player.is_pitcher,
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
            batting_average=batting.batting_average,
            on_base_percentage=batting.on_base_percentage,
            slugging_percentage=batting.slugging_percentage,
            ops=batting.ops,
            earned_run_average=pitching.earned_run_average,
            whip=pitching.whip,
            strikeouts_per_nine=pitching.strikeouts_per_nine,
        )
