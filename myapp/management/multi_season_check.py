"""実データのリーグから、複数のシーズンを続けて回し、オフの規則（成長・衰え・引退・ドラフト）を確かめる。

`simulate_sample --from-real-leagues ... --seasons N` の中身。**DB には一切書かない**（実データを読み、
推定・シミュレーション・オフはメモリの上だけで行う）。

    1年目: 実成績から能力を推定 → 1シーズン回す
    シーズンの間: 出場機会（シミュレーションした試合から数える）で `plan_offseason` を回し、結果（獲得した外国人と
    新人を含む）を適用
    2年目以降: 翌年の能力で1シーズン回す

シーズンごとに、水準の表（実データと並べる）・タイトル争い・年齢と能力の分布・入れ替わり・錨の補正・
外国人の打席と投球回の割合・獲得した外国人と戦力の穴との対応（#169）を出す。
曲線や引退の倍率を直したら、ここで3シーズンの分布を読んで確かめる（Wiki「ペナントモードの仕組み」の「実測値」）。
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from statistics import fmean, pstdev

from myapp.application.pennant_world import ForkSource
from myapp.domain.pennant.draft import DraftTeam
from myapp.domain.pennant.draft_meeting import DraftContext
from myapp.domain.pennant.draft_order import DraftOrderRule, DraftStandings
from myapp.domain.pennant.foreign_market import position_hole, squad_holes, world_strength
from myapp.domain.pennant.fork import with_birth_date
from myapp.domain.pennant.initial_ratings import age_at_season_start
from myapp.domain.pennant.offseason import (
    OffseasonClub,
    OffseasonPlan,
    OffseasonPlayer,
    plan_departures,
    plan_offseason,
)
from myapp.domain.pennant.offseason_player import overall_value
from myapp.domain.pennant.ratings import PlayerRatings
from myapp.domain.pennant.retirement import PlayingTime
from myapp.domain.pennant.schedule import ScheduleRules
from myapp.domain.pennant.world import league_priority
from myapp.domain.simulation.levels import UsageTally
from myapp.domain.simulation.manager import ClubRoster, SimBatter, SimPitcher, choose_active_roster
from myapp.domain.simulation.ratings import PitcherRatings
from myapp.domain.value_objects import BattingLine, ContractStatus, ForeignPlayerRules, PitchingLine, Position, Profile
from myapp.management.estimate_check import (
    LEVEL_LABELS,
    average_titles,
    estimate_from_source,
    format_comparison,
    format_distribution,
    format_usage,
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
    number: str
    joined_year: int
    ratings: PlayerRatings
    # 今の契約区分（分岐元の `Stint.contract_now`。新人は `Draftee.contract`）。育成は1軍の候補に入れない
    contract: ContractStatus


@dataclass(frozen=True)
class TeamInfo:
    team_id: int
    league_id: int
    name: str
    foreign_rules: ForeignPlayerRules
    registered_limit: int | None


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
            teams[_id(team.id)] = TeamInfo(
                _id(team.id),
                _id(league.id),
                team.name,
                league.foreign_rules,
                league.registered_player_limit,
            )
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
                        contract=stint.contract_now,
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
        if member.contract is ContractStatus.DEVELOPMENTAL:
            continue
        rating = member.ratings.ratings
        foreign = member.profile.is_foreign_player
        if isinstance(rating, PitcherRatings):
            pitchers[member.team_id].append(SimPitcher(member.player_id, member.name, rating, foreign))
        else:
            batters[member.team_id].append(
                SimBatter(
                    member.player_id, member.name, member.position, rating, foreign, throws=member.profile.throws
                )
            )
    rosters = {}
    leagues: dict[int, list[int]] = {}
    for team_id, team in teams.items():
        club = ClubRoster(team_id, team.name, tuple(batters[team_id]), tuple(pitchers[team_id]))
        rosters[team_id] = choose_active_roster(club, rules=team.foreign_rules)
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
                contract=member.contract,
                playing_time=_playing_time(batting, pitching, member.player_id),
            )
        )
    return [
        OffseasonClub(
            team_id=team_id,
            foreign_rules=team.foreign_rules,
            registered_player_limit=team.registered_limit,
            players=tuple(by_team[team_id]),
        )
        for team_id, team in teams.items()
    ]


def apply_plan(members: Sequence[Member], plan: OffseasonPlan) -> list[Member]:
    """計画を適用した翌年の現役。新人の id は既存の最大 + 1 から振る。"""
    leaving = set(plan.retired) | set(plan.released)
    after = {change.player_id: change.after for change in plan.retained}
    kept = [
        replace(m, ratings=after[m.player_id]) for m in members if m.player_id not in leaving and m.player_id in after
    ]
    league_of = {m.team_id: m.league_id for m in members}
    next_id = max(m.player_id for m in members) + 1
    for offset, newcomer in enumerate(plan.newcomers):
        player_id = next_id + offset
        kept.append(
            Member(
                player_id=player_id,
                team_id=newcomer.team_id,
                league_id=league_of[newcomer.team_id],
                name=newcomer.name,
                position=newcomer.position,
                profile=newcomer.profile,
                number=newcomer.number,
                joined_year=plan.year + 1,
                ratings=PlayerRatings(player_id, plan.year + 1, newcomer.ratings),
                contract=newcomer.contract,
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
    team_ids = {m.team_id for m in members}
    sizes = [sum(1 for m in members if m.team_id == team_id) for team_id in team_ids]
    registered = [
        sum(1 for m in members if m.team_id == team_id and m.contract is ContractStatus.REGISTERED)
        for team_id in team_ids
    ]
    development = [
        sum(1 for m in members if m.team_id == team_id and m.contract is ContractStatus.DEVELOPMENTAL)
        for team_id in team_ids
    ]
    return (
        f"現役 {len(members)}人（1球団 {min(sizes)}〜{max(sizes)}人・支配下 {min(registered)}〜{max(registered)}人"
        f"＝平均 {_mean(registered):.1f}人・外国人 {foreign}人＝1球団 平均 {foreign / len(team_ids):.1f}人・"
        f"育成 {sum(development)}人＝1球団 {min(development)}〜{max(development)}人・"
        f"平均 {_mean(development):.1f}人）  "
        f"年齢 平均 {fmean(ages):.1f}・標準偏差 {pstdev(ages):.1f}・"
        f"35歳以上 {sum(1 for a in ages if a >= 35) / len(ages):.1%}・"
        f"22歳以下 {sum(1 for a in ages if a <= 22) / len(ages):.1%}"
    )


def format_foreign_share(
    members: Sequence[Member], batting: dict[int, BattingLine], pitching: dict[int, PitchingLine]
) -> str:
    """シーズンの、外国人の打席と投球回（アウト）の割合。"""
    foreign = {m.player_id for m in members if m.profile.is_foreign_player}
    plate = sum(line.plate_appearances for line in batting.values())
    outs = sum(line.innings.outs for line in pitching.values())
    foreign_plate = sum(line.plate_appearances for player_id, line in batting.items() if player_id in foreign)
    foreign_outs = sum(line.innings.outs for player_id, line in pitching.items() if player_id in foreign)
    return f"外国人の打席の割合 {foreign_plate / max(1, plate):.1%}・投球回の割合 {foreign_outs / max(1, outs):.1%}"


def format_foreign_offseason(plan: OffseasonPlan, members: Sequence[Member], year: int, *, start_year: int) -> str:
    """直前のオフの外国人（獲得・戦力外・1年目が外れる割合）。1年目は締める年に加入した外国人（世界の開幕年は除く）。"""
    by_id = {m.player_id: m for m in members}
    teams = len({m.team_id for m in members})
    signed = plan.signings
    before = [overall_value(signing.candidate.ratings) for signing in signed]
    after = [overall_value(signing.ratings) for signing in signed]
    backgrounds: dict[str, int] = {}
    for signing in signed:
        label = signing.candidate.background.label
        backgrounds[label] = backgrounds.get(label, 0) + 1
    ages = [signing.candidate.age for signing in signed]
    first_year = [
        m
        for m in members
        if m.profile.is_foreign_player and m.contract is ContractStatus.REGISTERED and m.joined_year == year
    ]
    released = set(plan.released)
    first_year_released = sum(1 for m in first_year if m.player_id in released)
    foreign_released = sum(1 for player_id in plan.released if by_id[player_id].profile.is_foreign_player)
    first_label = (
        f"{first_year_released}/{len(first_year)}人＝{first_year_released / len(first_year):.0%}"
        if first_year and year > start_year
        else "—"
    )
    return (
        f"  新外国人 {len(signed)}人（1球団 平均 {len(signed) / max(1, teams):.2f}人・"
        f"{'・'.join(f'{k} {v}' for k, v in backgrounds.items())}・平均 {_mean(ages):.1f}歳）  "
        f"総合 錨の前 平均 {_mean(before):.1f}・標準偏差 {pstdev(before) if before else 0.0:.1f}／"
        f"錨の後 平均 {_mean(after):.1f}  "
        f"外国人の戦力外 {foreign_released}人・1年目が外れる割合 {first_label}"
    )


def format_foreign_needs(plan: OffseasonPlan, teams: Sequence[DraftTeam]) -> str:
    """直前のオフの新外国人の位置と、戦力の穴との対応（#169 F1b）。`teams` は引退・戦力外の後の球団
    （`plan_departures` の `draft_teams`。獲得の前の戦力）。

    投手陣の穴（先発と救援の平均）が打線の穴（内野と外野の平均）より大きい球団と、そうでない球団に分け、
    それぞれの新外国人に占める投手の割合を出す（穴の大きい側を取っていれば、前者の方が高い）。
    """
    reference = world_strength([team.squad for team in teams])
    pitching_short: set[int] = set()
    for team in teams:
        holes = squad_holes(team.squad, reference)
        batting = (position_hole(holes, Position.INFIELDER) + position_hole(holes, Position.OUTFIELDER)) / 2
        if position_hole(holes, Position.PITCHER) > batting:
            pitching_short.add(team.team_id)
    positions = Counter(signing.position for signing in plan.signings)
    sides = []
    for label, inside in (("投手陣の穴が大きい球団", True), ("打線の穴が大きい球団", False)):
        signed = [signing for signing in plan.signings if (signing.team_id in pitching_short) is inside]
        clubs = sum(1 for team in teams if (team.team_id in pitching_short) is inside)
        pitchers = sum(1 for signing in signed if signing.position is Position.PITCHER)
        share = f"{pitchers / len(signed):.0%}" if signed else "—"
        sides.append(f"{label} {clubs}球団→新外国人 {len(signed)}人・うち投手 {pitchers}人＝{share}")
    return (
        f"  新外国人の位置 投手 {positions[Position.PITCHER]}・内野手 {positions[Position.INFIELDER]}・"
        f"外野手 {positions[Position.OUTFIELDER]}  穴との対応: {'／'.join(sides)}"
    )


def format_offseason(plan: OffseasonPlan, members: Sequence[Member], year: int) -> str:
    """直前のオフ（Y 年を締めた）の要約。"""
    by_id = {m.player_id: m for m in members}
    ages = [
        age for player_id in plan.retired if (age := age_at_season_start(by_id[player_id].profile, year)) is not None
    ]
    foreign_retired = sum(1 for player_id in plan.retired if by_id[player_id].profile.is_foreign_player)
    released_ages = [
        age for player_id in plan.released if (age := age_at_season_start(by_id[player_id].profile, year)) is not None
    ]
    foreign_released = sum(1 for player_id in plan.released if by_id[player_id].profile.is_foreign_player)
    deltas = [change.delta for change in plan.retained]
    routes: dict[str, int] = {}
    for draftee in plan.draftees:
        if not draftee.is_development:
            routes[draftee.route.label] = routes.get(draftee.route.label, 0) + 1
    development = [draftee for draftee in plan.draftees if draftee.is_development]
    development_routes: dict[str, int] = {}
    for draftee in development:
        development_routes[draftee.route.label] = development_routes.get(draftee.route.label, 0) + 1
    registered_values = [overall_value(d.ratings) for d in plan.draftees if not d.is_development]
    development_values = [overall_value(d.ratings) for d in development]
    return "\n".join(
        [
            f"  引退 {len(plan.retired)}人（うち外国人 {foreign_retired}人・平均 {_mean(ages):.1f}歳）  "
            f"戦力外 {len(plan.released)}人（うち外国人 {foreign_released}人・平均 {_mean(released_ages):.1f}歳）  "
            f"新人 {len(plan.draftees) - len(development)}人（{'・'.join(f'{k} {v}' for k, v in routes.items())}）  "
            f"入れ替わり {(len(plan.retired) + len(plan.released)) / max(1, len(members)):.1%}",
            f"  育成ドラフト {len(development)}人（{'・'.join(f'{k} {v}' for k, v in development_routes.items())}）  "
            f"総合（錨の後）の平均: 本指名の日本人 {_mean(registered_values):.1f}・"
            f"育成 {_mean(development_values):.1f}",
            f"  能力の変化（総合値）平均 {_mean(deltas):+.2f}・標準偏差 {pstdev(deltas) if deltas else 0.0:.2f}・"
            f"最大 {max(deltas, default=0.0):+.1f}・最小 {min(deltas, default=0.0):+.1f}",
            f"  ドラフト: 1位の競合 "
            f"{sum(1 for lot in plan.lotteries if lot.round_number == 1 and not lot.is_development)}件"
            f"（最多 {max((len(lot.bidders) for lot in plan.lotteries), default=0)}球団）",
            f"  錨の補正（前の分布の目標からのずれ）平均 最大 {plan.anchor.mean_shift:.2f} 点"
            f"（{plan.anchor.mean_item} {plan.anchor.mean_signed:+.2f}）・標準偏差 最大 {plan.anchor.sd_shift:.2f} 点",
        ]
    )


def draft_standings(
    leagues: dict[int, list[int]],
    pitching: dict[int, PitchingLine],
    members: Sequence[Member],
    *,
    year: int,
    start_year: int,
) -> DraftStandings:
    """ドラフトの指名順に使う最終順位（メモリの上のシーズン）。勝ち数（投手の勝利の合計）の多い順、同数は球団 id の順。

    確認用の近似（引分・同率の規定は見ない）。リーグはその年の優先順（`league_priority`）に並べる。
    """
    team_of = {m.player_id: m.team_id for m in members}
    wins: dict[int, int] = {}
    for player_id, line in pitching.items():
        team_id = team_of.get(player_id)
        if team_id is not None:
            wins[team_id] = wins.get(team_id, 0) + line.wins
    priority = league_priority(leagues, year=year, start_year=start_year)
    return DraftStandings(
        leagues=tuple(
            tuple(sorted(leagues[league_id], key=lambda team_id: (-wins.get(team_id, 0), team_id)))
            for league_id in priority
        )
    )


def run_seasons(
    league_ids: Sequence[int],
    *,
    seasons: int,
    seed: int,
    year: int,
    say: Callable[[str], None],
    draft_order: DraftOrderRule = DraftOrderRule.NPB,
) -> None:
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
        usage = UsageTally()
        batting, pitching, games, team_games = play_season(rosters, leagues, seed=seed, year=current, usage=usage)
        say(f"{games}試合  {(time.perf_counter() - started) / games * 1000:.1f} ms/試合")
        say(format_usage(usage.rows()))
        say(format_comparison(real, summarize(batting.values(), pitching.values()), "リーグ全体の水準:", LEVEL_LABELS))
        say(format_foreign_share(members, batting, pitching))
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
        clubs = offseason_clubs(members, teams, batting, pitching)
        plan = plan_offseason(
            clubs,
            year=current,
            world_seed=seed,
            start_year=year,
            used_names=names,
            draft=DraftContext(
                draft_order, draft_standings(leagues, pitching, members, year=current, start_year=year)
            ),
        )
        elapsed = time.perf_counter() - started
        names.update(newcomer.name for newcomer in plan.newcomers)
        say("")
        say(f"---- {current}年のオフ（計算 {elapsed * 1000:.0f} ms）----")
        say(format_offseason(plan, members, current))
        say(format_foreign_offseason(plan, members, current, start_year=year))
        departures = plan_departures(clubs, year=current, world_seed=seed, start_year=year)
        say(format_foreign_needs(plan, departures.draft_teams))
        members = apply_plan(members, plan)
