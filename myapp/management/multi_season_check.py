"""実データのリーグから、複数のシーズンを続けて回し、オフの規則（成長・衰え・引退・ドラフト）を確かめる。

`simulate_sample --from-real-leagues ... --seasons N` の中身。**DB には一切書かない**（実データを読み、
推定・シミュレーション・オフはメモリの上だけで行う）。

    1年目: 実成績から能力を推定 → 1シーズン回す
    シーズンの間: 出場機会（シミュレーションした試合から数える）で `plan_offseason` を回し、結果を適用
    2年目以降: 翌年の能力で1シーズン回す

シーズンごとに、水準の表（実データと並べる）・タイトル争い・年齢と能力の分布・入れ替わり・錨の補正を出す。
曲線や引退の倍率を直したら、ここで3シーズンの分布を読んで確かめる（設計書 12.「P6a の詳細」）。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from statistics import fmean, pstdev

from myapp.application.pennant_world import ForkSource
from myapp.domain.pennant.fork import with_birth_date
from myapp.domain.pennant.initial_ratings import age_at_season_start
from myapp.domain.pennant.offseason import OffseasonClub, OffseasonPlan, OffseasonPlayer, plan_offseason
from myapp.domain.pennant.ratings import PlayerRatings
from myapp.domain.pennant.retirement import PlayingTime
from myapp.domain.pennant.schedule import ScheduleRules
from myapp.domain.simulation.manager import ClubRoster, SimBatter, SimPitcher, choose_active_roster
from myapp.domain.simulation.ratings import PitcherRatings
from myapp.domain.value_objects import BattingLine, PitchingLine, Position, Profile
from myapp.management.estimate_check import (
    DEFAULT_FOREIGN_ROSTER_LIMIT,
    LEVEL_LABELS,
    average_titles,
    estimate_from_source,
    format_comparison,
    format_distribution,
    league_titles,
    play_season,
    summarize,
)
from myapp.presentation.views import build_pennant_world_service


@dataclass(frozen=True)
class Member:
    """メモリ上の現役の選手。"""

    player_id: int
    team_id: int
    league_id: int
    name: str
    position: Position
    profile: Profile
    number: int
    joined_year: int
    ratings: PlayerRatings


@dataclass(frozen=True)
class TeamInfo:
    team_id: int
    league_id: int
    name: str
    foreign_limit: int | None


def _id(value: int | None) -> int:
    assert value is not None, "保存済みの集約には id がある"
    return value


def initial_members(
    source: ForkSource, ratings: dict[int, PlayerRatings], *, year: int
) -> tuple[list[Member], dict[int, TeamInfo], set[str]]:
    """分岐元の現在の選手を、メモリ上の状態にする。生年月日が空の選手は推定で補う。"""
    members: list[Member] = []
    teams: dict[int, TeamInfo] = {}
    names: set[str] = set()
    for league, league_teams in zip(source.leagues, source.rosters, strict=True):
        for team in league_teams:
            teams[_id(team.id)] = TeamInfo(_id(team.id), _id(league.id), team.name, league.foreign_player_roster_limit)
            for player in team.active_players:
                stint = team.current_stint(player)
                assert stint is not None
                members.append(
                    Member(
                        player_id=_id(player.id),
                        team_id=_id(team.id),
                        league_id=_id(league.id),
                        name=player.name,
                        position=player.position,
                        profile=with_birth_date(player.profile, year),
                        number=stint.number.value,
                        joined_year=year,
                        ratings=ratings[_id(player.id)],
                    )
                )
    for league_teams in source.rosters:
        for team in league_teams:
            names.update(player.name for player in team.players)
    return members, teams, names


def rosters_of(
    members: Sequence[Member], teams: dict[int, TeamInfo]
) -> tuple[dict[int, ClubRoster], dict[int, list[int]]]:
    """1軍の登録（`choose_active_roster`）と、リーグごとの球団 id。"""
    batters: dict[int, list[SimBatter]] = {team_id: [] for team_id in teams}
    pitchers: dict[int, list[SimPitcher]] = {team_id: [] for team_id in teams}
    for member in members:
        rating = member.ratings.ratings
        foreign = member.profile.is_foreign_player
        if isinstance(rating, PitcherRatings):
            pitchers[member.team_id].append(SimPitcher(member.player_id, member.name, rating, foreign))
        else:
            batters[member.team_id].append(SimBatter(member.player_id, member.name, member.position, rating, foreign))
    rosters = {}
    leagues: dict[int, list[int]] = {}
    for team_id, team in teams.items():
        club = ClubRoster(team_id, team.name, tuple(batters[team_id]), tuple(pitchers[team_id]))
        limit = DEFAULT_FOREIGN_ROSTER_LIMIT if team.foreign_limit is None else team.foreign_limit
        rosters[team_id] = choose_active_roster(club, limit)
        leagues.setdefault(team.league_id, []).append(team_id)
    return rosters, leagues


def _playing_time(batting: dict[int, BattingLine], pitching: dict[int, PitchingLine], player_id: int) -> PlayingTime:
    hit = batting.get(player_id)
    pitched = pitching.get(player_id)
    return PlayingTime(
        plate_appearances=0 if hit is None else hit.plate_appearances,
        outs=0 if pitched is None else pitched.innings.outs,
    )


def offseason_clubs(
    members: Sequence[Member],
    teams: dict[int, TeamInfo],
    batting: dict[int, BattingLine],
    pitching: dict[int, PitchingLine],
) -> list[OffseasonClub]:
    by_team: dict[int, list[OffseasonPlayer]] = {team_id: [] for team_id in teams}
    for member in members:
        by_team[member.team_id].append(
            OffseasonPlayer(
                player_id=member.player_id,
                position=member.position,
                profile=member.profile,
                number=member.number,
                joined_year=member.joined_year,
                ratings=member.ratings,
                playing_time=_playing_time(batting, pitching, member.player_id),
            )
        )
    return [
        OffseasonClub(team_id=team_id, foreign_roster_limit=team.foreign_limit, players=tuple(by_team[team_id]))
        for team_id, team in teams.items()
    ]


def apply_plan(members: Sequence[Member], plan: OffseasonPlan) -> list[Member]:
    """計画を適用した翌年の現役。新人の id は既存の最大 + 1 から振る。"""
    retired = set(plan.retired)
    after = {change.player_id: change.after for change in plan.retained}
    kept = [
        replace(m, ratings=after[m.player_id]) for m in members if m.player_id not in retired and m.player_id in after
    ]
    league_of = {m.team_id: m.league_id for m in members}
    next_id = max(m.player_id for m in members) + 1
    for offset, draftee in enumerate(plan.draftees):
        player_id = next_id + offset
        kept.append(
            Member(
                player_id=player_id,
                team_id=draftee.team_id,
                league_id=league_of[draftee.team_id],
                name=draftee.name,
                position=draftee.position,
                profile=draftee.profile,
                number=draftee.number,
                joined_year=plan.year + 1,
                ratings=PlayerRatings(player_id, plan.year + 1, draftee.ratings),
            )
        )
    return kept


# --- 表示 ------------------------------------------------------------------------------


def _mean(values: Sequence[float]) -> float:
    """平均。空（引退者が 0 人など）でも落ちずに 0。"""
    return fmean(values) if values else 0.0


def format_population(members: Sequence[Member], year: int) -> str:
    """現役の年齢の分布（開幕日=4月1日の満年齢）。"""
    ages = [age for m in members if (age := age_at_season_start(m.profile, year)) is not None]
    foreign = sum(1 for m in members if m.profile.is_foreign_player)
    sizes = [sum(1 for m in members if m.team_id == team_id) for team_id in {m.team_id for m in members}]
    return (
        f"現役 {len(members)}人（1球団 {min(sizes)}〜{max(sizes)}人・外国人 {foreign}人）  "
        f"年齢 平均 {fmean(ages):.1f}・標準偏差 {pstdev(ages):.1f}・"
        f"35歳以上 {sum(1 for a in ages if a >= 35) / len(ages):.1%}・"
        f"22歳以下 {sum(1 for a in ages if a <= 22) / len(ages):.1%}"
    )


def format_offseason(plan: OffseasonPlan, members: Sequence[Member], year: int) -> str:
    """直前のオフ（Y 年を締めた）の要約。"""
    by_id = {m.player_id: m for m in members}
    ages = [
        age for player_id in plan.retired if (age := age_at_season_start(by_id[player_id].profile, year)) is not None
    ]
    foreign_retired = sum(1 for player_id in plan.retired if by_id[player_id].profile.is_foreign_player)
    deltas = [change.delta for change in plan.retained]
    routes: dict[str, int] = {}
    for draftee in plan.draftees:
        routes[draftee.route.label] = routes.get(draftee.route.label, 0) + 1
    return "\n".join(
        [
            f"  引退 {len(plan.retired)}人（うち外国人 {foreign_retired}人・平均 {_mean(ages):.1f}歳）  "
            f"新人 {len(plan.draftees)}人（{'・'.join(f'{k} {v}' for k, v in routes.items())}）  "
            f"入れ替わり {len(plan.retired) / max(1, len(members)):.1%}",
            f"  能力の変化（総合値）平均 {_mean(deltas):+.2f}・標準偏差 {pstdev(deltas) if deltas else 0.0:.2f}・"
            f"最大 {max(deltas, default=0.0):+.1f}・最小 {min(deltas, default=0.0):+.1f}",
            f"  錨の補正（前の分布の目標からのずれ）平均 最大 {plan.anchor.mean_shift:.2f} 点"
            f"（{plan.anchor.mean_item} {plan.anchor.mean_signed:+.2f}）・標準偏差 最大 {plan.anchor.sd_shift:.2f} 点",
        ]
    )


def run_seasons(league_ids: Sequence[int], *, seasons: int, seed: int, year: int, say: Callable[[str], None]) -> None:
    service = build_pennant_world_service()
    source = service.load_source(league_ids)
    players = source.active_players
    say(f"実データ {len(source.leagues)} リーグ・{len(players)} 人から初期能力を推定し、{seasons}シーズン続けます。")
    ratings = estimate_from_source(service, source, seed=seed, year=year)
    members, teams, names = initial_members(source, ratings, year=year)
    real = summarize((p.batting for p in players), (p.pitching for p in players))
    real_batting = {_id(p.id): p.batting for p in players}
    real_pitching = {_id(p.id): p.pitching for p in players}
    real_titles = []
    for league_teams in source.rosters:
        ids = {_id(p.id) for team in league_teams for p in team.active_players}
        real_titles.append(league_titles(real_batting, real_pitching, ids, ScheduleRules().games_per_team))

    for season in range(seasons):
        current = year + season
        say("")
        say(f"==== {current}年（{season + 1}シーズン目） ====")
        say(format_population(members, current))
        say(format_distribution(m.ratings for m in members))
        say("")
        rosters, leagues = rosters_of(members, teams)
        started = time.perf_counter()
        batting, pitching, games, team_games = play_season(rosters, leagues, seed=seed, year=current)
        say(f"{games}試合  {(time.perf_counter() - started) / games * 1000:.1f} ms/試合")
        say(format_comparison(real, summarize(batting.values(), pitching.values()), "リーグ全体の水準:", LEVEL_LABELS))
        say("")
        simulated_titles = []
        for team_ids in leagues.values():
            ids = {m.player_id for m in members if m.team_id in team_ids}
            simulated_titles.append(league_titles(batting, pitching, ids, team_games))
        labels = {label: label for label in real_titles[0]}
        say(
            format_comparison(
                average_titles(real_titles), average_titles(simulated_titles), "タイトル争い（リーグの平均）:", labels
            )
        )
        if season + 1 == seasons:
            break
        started = time.perf_counter()
        plan = plan_offseason(
            offseason_clubs(members, teams, batting, pitching),
            year=current,
            world_seed=seed,
            start_year=year,
            used_names=names,
        )
        elapsed = time.perf_counter() - started
        names.update(draftee.name for draftee in plan.draftees)
        say("")
        say(f"---- {current}年のオフ（計算 {elapsed * 1000:.0f} ms）----")
        say(format_offseason(plan, members, current))
        members = apply_plan(members, plan)
