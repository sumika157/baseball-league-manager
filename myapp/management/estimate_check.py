"""実データのリーグから初期能力を推定し、その能力でシミュレーションして元の水準と比べる。

`simulate_sample --from-real-leagues` の中身。**DB には一切書かない**（実データを読み、推定とシミュレーションは
メモリの上だけで行う）。世界の作成（`PennantWorldService.create_world`）と同じ入口
（`PennantWorldService.estimate_ratings`。材料の組み立てと散らばりの調整を含む）・同じ母集団
（現在在籍の選手全員）で推定するので、ここで水準が合えば世界の作成も合う。

比べるのは、リーグ全体の水準（打率・防御率・K/9 など）と、リーグごとのタイトル争い（首位打者・本塁打王など）。
どちらも**選手ごとの通算成績から同じ式で**数える（実データは保存済みの通算、シミュレーションは試合から合計）。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Sequence
from statistics import mean, pstdev

from myapp.application.pennant_world import ForkSource, PennantWorldService
from myapp.domain.entities import Team
from myapp.domain.exceptions import DomainError
from myapp.domain.pennant.ratings import PlayerRatings
from myapp.domain.pennant.schedule import ScheduleRules, default_opening_day, generate_schedule
from myapp.domain.simulation.engine import simulate_game
from myapp.domain.simulation.levels import SeasonTally
from myapp.domain.simulation.manager import ClubRoster, PitchingHistory, SimBatter, SimPitcher, choose_active_roster
from myapp.domain.simulation.randomness import game_seed, make_random
from myapp.domain.simulation.ratings import BatterRatings, PitcherRatings, RatingGrade
from myapp.domain.value_objects import BattingLine, InningsPitched, PitchingLine
from myapp.presentation.views import build_pennant_world_service

OUTS_PER_INNING = InningsPitched.OUTS_PER_INNING
# 外国人の登録枠が空欄（無制限）のリーグで使う上限。分岐元がすでに超えていても動かすため
DEFAULT_FOREIGN_ROSTER_LIMIT = 4


def _pad(text: str, width: int) -> str:
    """全角を2桁として右に空白を足す。"""
    used = sum(2 if ord(char) > 0x7F else 1 for char in text)
    return text + " " * max(0, width - used)


def _id(value: int | None) -> int:
    assert value is not None, "保存済みの集約には id がある"
    return value


def estimate_from_source(
    service: PennantWorldService, source: ForkSource, *, seed: int, year: int
) -> dict[int, PlayerRatings]:
    """分岐元の選手の id → 推定した能力。世界の作成と同じ入口（`estimate_ratings`）と同じ母集団
    （現在在籍の選手全員）を通る。"""
    players = source.active_players
    ratings = service.estimate_ratings(players, seed=seed, year=year)
    return {item.player_id: item for item in ratings}


# --- 能力の分布 ------------------------------------------------------------------------


def format_distribution(ratings: Iterable[PlayerRatings]) -> str:
    """項目ごとの平均・標準偏差と、S〜G の人数。"""
    items = list(ratings)
    lines = ["推定した能力の分布:"]
    for label, group, labels in (
        ("野手", [i for i in items if not i.is_pitcher], BatterRatings.LABELS),
        ("投手", [i for i in items if i.is_pitcher], PitcherRatings.LABELS),
    ):
        lines.append(
            f"  【{label} {len(group)}人】                平均   標準偏差   " + "  ".join(g.label for g in RatingGrade)
        )
        for name, name_label in labels.items():
            values = [getattr(i.ratings, name) for i in group]
            if not values:
                continue
            counts = [sum(1 for v in values if RatingGrade.from_value(v) is g) for g in RatingGrade]
            lines.append(
                f"    {_pad(name_label, 12)}{mean(values):>12.1f}{pstdev(values):>10.1f}   "
                + " ".join(f"{c:>3}" for c in counts)
            )
    return "\n".join(lines)


# --- 水準の比較 ------------------------------------------------------------------------


LEVEL_LABELS = {
    "runs_allowed": "失点（9回あたり）",
    "batting_average": "打率",
    "era": "防御率",
    "strikeouts_per_9": "K/9",
    "walks_per_9": "BB/9",
    "home_runs_per_9": "HR/9",
    "stolen_bases": "盗塁（9回あたり）",
    "caught_stealing": "盗塁刺（9回あたり）",
    "sacrifice_bunts": "犠打（9回あたり）",
    "double_plays": "併殺打（9回あたり）",
    "strikeout_rate": "三振 / 打数",
    "walk_rate": "四球 / 打席",
    "home_run_rate": "本塁打 / 打数",
    "babip": "BABIP",
    "double_share": "二塁打 / 安打（本塁打除く）",
}


def summarize(batting: Iterable[BattingLine], pitching: Iterable[PitchingLine]) -> dict[str, float]:
    batters = BattingLine.total(batting)
    pitchers = PitchingLine.total(pitching)
    innings = max(1.0, pitchers.innings.outs / OUTS_PER_INNING)
    return {
        "runs_allowed": pitchers.runs_allowed * 9 / innings,
        "batting_average": batters.batting_average,
        "era": pitchers.earned_run_average,
        "strikeouts_per_9": pitchers.strikeouts_per_nine,
        "walks_per_9": pitchers.walks_per_nine,
        "home_runs_per_9": pitchers.home_runs_allowed * 9 / innings,
        "stolen_bases": batters.stolen_bases * 9 / innings,
        "caught_stealing": batters.caught_stealing * 9 / innings,
        "sacrifice_bunts": batters.sacrifice_bunts * 9 / innings,
        "double_plays": batters.double_plays * 9 / innings,
        # 打者の側の率（能力の推定が直接ねらう段ごとの率）
        "strikeout_rate": batters.strikeouts / max(1, batters.at_bats),
        "walk_rate": batters.walks / max(1, batters.plate_appearances),
        "home_run_rate": batters.home_runs / max(1, batters.at_bats),
        "babip": (batters.hits - batters.home_runs) / max(1, batters.at_bats - batters.strikeouts - batters.home_runs),
        "double_share": batters.doubles / max(1, batters.hits - batters.home_runs),
    }


def league_titles(
    batting: dict[int, BattingLine], pitching: dict[int, PitchingLine], player_ids: set[int], team_games: int
) -> dict[str, float]:
    """リーグ1つのタイトル争い（そのリーグの選手だけで数える）。"""
    tally = SeasonTally(team_games=team_games)
    tally.batting = {pid: line for pid, line in batting.items() if pid in player_ids}
    tally.pitching = {pid: line for pid, line in pitching.items() if pid in player_ids}
    return {row.label: row.value for row in tally.rows()}


def average_titles(per_league: list[dict[str, float]]) -> dict[str, float]:
    return {label: mean(titles[label] for titles in per_league) for label in per_league[0]}


def _format_value(label: str, value: float) -> str:
    """打率は小数点以下3桁、それ以外は2桁。"""
    if ("打率" in label and "防御率" not in label) or label in (
        "BABIP",
        "三振 / 打数",
        "四球 / 打席",
        "本塁打 / 打数",
    ):
        return f"{value:.3f}"
    return f"{value:.2f}"


def format_comparison(real: dict[str, float], simulated: dict[str, float], title: str, labels: dict[str, str]) -> str:
    lines = [title, f"  {_pad('', 24)}{'実データ':>12}{'推定能力で':>12}"]
    for key, label in labels.items():
        lines.append(
            f"  {_pad(label, 24)}{_format_value(label, real[key]):>10}  {_format_value(label, simulated[key]):>10}"
        )
    return "\n".join(lines)


# --- 推定した能力でのシミュレーション ------------------------------------------------------


def _club(team: Team, ratings: dict[int, PlayerRatings]) -> ClubRoster:
    batters: list[SimBatter] = []
    pitchers: list[SimPitcher] = []
    for player in team.active_players:
        rating = ratings[_id(player.id)].ratings
        if isinstance(rating, PitcherRatings):
            pitchers.append(SimPitcher(_id(player.id), player.name, rating, player.profile.is_foreign_player))
        elif isinstance(rating, BatterRatings):
            batters.append(
                SimBatter(
                    _id(player.id),
                    player.name,
                    player.position,
                    rating,
                    player.profile.is_foreign_player,
                    throws=player.profile.throws,
                )
            )
    return ClubRoster(team_id=_id(team.id), name=team.name, batters=tuple(batters), pitchers=tuple(pitchers))


def simulate_season(
    source: ForkSource, ratings: dict[int, PlayerRatings], *, seed: int, year: int
) -> tuple[dict[int, BattingLine], dict[int, PitchingLine], int, int]:
    """推定した能力で1シーズンを回す。(選手ごとの打撃, 投球, 試合数, 1球団の試合数)。"""
    rosters = {}
    for league, teams in zip(source.leagues, source.rosters, strict=True):
        limit = league.foreign_player_roster_limit
        for team in teams:
            club = _club(team, ratings)
            rosters[_id(team.id)] = choose_active_roster(
                club, DEFAULT_FOREIGN_ROSTER_LIMIT if limit is None else limit
            )
    leagues = {
        _id(league.id): [_id(team.id) for team in teams]
        for league, teams in zip(source.leagues, source.rosters, strict=True)
    }
    return play_season(rosters, leagues, seed=seed, year=year)


def play_season(
    rosters: dict[int, ClubRoster], leagues: dict[int, list[int]], *, seed: int, year: int
) -> tuple[dict[int, BattingLine], dict[int, PitchingLine], int, int]:
    """球団の1軍の登録（`choose_active_roster` を通した後）で1シーズンを回す。

    (選手ごとの打撃, 投球, 試合数, 1球団の試合数)。複数シーズンの確認（`multi_season_check.py`）もここを通る。
    """
    rules = ScheduleRules()
    schedule_rng = make_random(game_seed(seed, year, "schedule-check"))
    try:
        fixtures = generate_schedule(leagues, rules, default_opening_day(year, rules), schedule_rng)
    except DomainError as error:
        raise ValueError(f"日程を組めません: {error}") from error

    history = PitchingHistory()
    season = SeasonTally(team_games=rules.games_per_team)
    for fixture in fixtures:
        rng = make_random(game_seed(seed, year, f"check-{fixture.date}-{fixture.home_team_id}"))
        played = simulate_game(
            rng,
            rosters[fixture.home_team_id],
            rosters[fixture.visitor_team_id],
            played_on=fixture.date,
            history=history,
        )
        season.add(played.game)
    return season.batting, season.pitching, len(fixtures), rules.games_per_team


def run_check(league_ids: Sequence[int], *, seed: int, year: int, say: Callable[[str], None]) -> None:
    service = build_pennant_world_service()
    source = service.load_source(league_ids)
    players = source.active_players
    team_count = sum(len(teams) for teams in source.rosters)
    say(f"実データ {len(source.leagues)} リーグ・{team_count} 球団・{len(players)} 人から初期能力を推定します。")
    started = time.perf_counter()
    ratings = estimate_from_source(service, source, seed=seed, year=year)
    say(f"推定 {(time.perf_counter() - started) * 1000:.0f} ms")
    say("")
    say(format_distribution(ratings.values()))
    say("")

    real_batting = {_id(p.id): p.batting for p in players}
    real_pitching = {_id(p.id): p.pitching for p in players}
    say("推定した能力で1シーズンを回します（時間がかかります）...")
    started = time.perf_counter()
    batting, pitching, games, team_games = simulate_season(source, ratings, seed=seed, year=year)
    say(f"{games} 試合  {(time.perf_counter() - started) / games * 1000:.1f} ms/試合")
    say("")

    say(
        format_comparison(
            summarize(real_batting.values(), real_pitching.values()),
            summarize(batting.values(), pitching.values()),
            "リーグ全体の水準:",
            LEVEL_LABELS,
        )
    )
    say("")

    real_titles, simulated_titles = [], []
    for teams in source.rosters:
        ids = {_id(p.id) for team in teams for p in team.active_players}
        real_titles.append(league_titles(real_batting, real_pitching, ids, team_games))
        simulated_titles.append(league_titles(batting, pitching, ids, team_games))
    labels = {label: label for label in real_titles[0]}
    say(
        format_comparison(
            average_titles(real_titles),
            average_titles(simulated_titles),
            f"タイトル争い（{len(source.leagues)}リーグの平均）:",
            labels,
        )
    )
