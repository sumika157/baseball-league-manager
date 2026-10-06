"""オフを10年回した母集団の分布（合成した 48 球団 × 34 人）。

曲線・引退・ドラフトの個々の規則は `test_aging` / `test_retirement` / `test_draft` が見る。ここは
**組み合わせたときに分布が保たれるか**を見る。年齢の平均が保たれる条件は
`入れ替わりの割合 × (引退の平均年齢 − 新人の平均年齢) ≈ 1歳` で、どれかの倍率を変えると崩れる。
曲線や倍率を直したら、このテストで帯の内側に入ることを確かめる（3シーズンの実測の代わり）。

帯のうち、錨の補正（1 点以内）と平均年齢の下限（27 歳）は、シード 1 の実測に近い値で、規則の小さな変更でも落ちうる
（平均年齢はシード 1 で 27.05 まで下がる）。落ちたら、曲線の誤りか、シードの揺れ（別のシードでも落ちるか）かを
切り分ける。設計書 12.「P6a の詳細」に最終値とシード 2〜4 の範囲がある。
"""

from __future__ import annotations

import time
import unittest
from dataclasses import dataclass, replace
from datetime import date
from statistics import fmean, pstdev

from myapp.domain.pennant.draft import ROSTER_TARGET
from myapp.domain.pennant.offseason import OffseasonClub, OffseasonPlan, OffseasonPlayer, plan_offseason
from myapp.domain.pennant.ratings import PlayerRatings
from myapp.domain.pennant.retirement import PlayingTime
from myapp.domain.simulation.randomness import GameRandom, make_random, normal
from myapp.domain.simulation.ratings import BatterRatings, PitcherRatings
from myapp.domain.simulation.samples import BATTER_MEAN, BATTER_SD, PITCHER_MEAN, PITCHER_SD, draw_ratings
from myapp.domain.value_objects import Position, Profile
from myapp.domain.virtual_players import generator

START_YEAR = 2026
TEAMS = 48
YEARS = 10
LEAGUE_FOREIGN_LIMIT = 4
# 試合で使う人数（1軍の野手は打順9人、投手は先発6人）
REGULAR_BATTERS = 9
PART_TIME_BATTERS = 3
REGULAR_STARTERS = 6
RELIEVERS = 7


@dataclass
class Member:
    """合成した母集団の選手ひとり。"""

    player_id: int
    team_id: int
    position: Position
    profile: Profile
    number: str
    joined_year: int
    ratings: PlayerRatings


def _age_distribution(rng: GameRandom) -> int:
    """開発用 DB の現役の年齢分布（平均 27.8・標準偏差 3.8）に似せる。"""
    return max(19, min(40, round(normal(rng, 27.8, 3.8))))


def make_population(seed: int = 1) -> list[Member]:
    rng = make_random(seed)
    members: list[Member] = []
    positions = [
        position
        for position, count in generator.largest_remainder(ROSTER_TARGET, generator.POSITION_RATIOS).items()
        for _ in range(count)
    ]
    for team_id in range(1, TEAMS + 1):
        for number, position in enumerate(positions, start=1):
            age = _age_distribution(rng)
            is_foreign = rng.random() < generator.FOREIGN_PLAYER_RATIO
            profile = Profile(
                birth_date=generator.birth_date_for(rng, age=age, as_of=date(START_YEAR, 4, 1)),
                debut_year=START_YEAR - generator.randint(rng, 0, max(0, age - 19)),
                is_foreign_player=is_foreign,
            )
            mean, sd = (PITCHER_MEAN, PITCHER_SD) if position.is_pitcher else (BATTER_MEAN, BATTER_SD)
            ratings = draw_ratings(rng, mean=mean, sd=sd, position=position, stamina_mean=50.0)
            player_id = len(members) + 1
            assert profile.debut_year is not None
            members.append(
                Member(
                    player_id=player_id,
                    team_id=team_id,
                    position=position,
                    profile=profile,
                    number=str(number),
                    joined_year=profile.debut_year,
                    ratings=PlayerRatings(player_id=player_id, year=START_YEAR, ratings=ratings),
                )
            )
    return members


def _batting_value(member: Member) -> float:
    ratings = member.ratings.ratings
    assert isinstance(ratings, BatterRatings)
    return -ratings.batting_value


def _pitching_value(member: Member) -> float:
    ratings = member.ratings.ratings
    assert isinstance(ratings, PitcherRatings)
    return -ratings.pitching_value


def _playing_time(members: list[Member]) -> dict[int, PlayingTime]:
    """チームの中の能力の順で、出場機会を合成する（試合は回さない）。"""
    result: dict[int, PlayingTime] = {}
    batters = sorted(
        (m for m in members if isinstance(m.ratings.ratings, BatterRatings)),
        key=_batting_value,
    )
    for index, member in enumerate(batters):
        if index < REGULAR_BATTERS:
            result[member.player_id] = PlayingTime(plate_appearances=550)
        elif index < REGULAR_BATTERS + PART_TIME_BATTERS:
            result[member.player_id] = PlayingTime(plate_appearances=150)
    pitchers = sorted(
        (m for m in members if isinstance(m.ratings.ratings, PitcherRatings)),
        key=_pitching_value,
    )
    for index, member in enumerate(pitchers):
        if index < REGULAR_STARTERS:
            result[member.player_id] = PlayingTime(outs=500)
        elif index < REGULAR_STARTERS + RELIEVERS:
            result[member.player_id] = PlayingTime(outs=150)
    return result


def run_year(members: list[Member], year: int, *, world_seed: int = 1) -> tuple[list[Member], OffseasonPlan]:
    """Y 年を締めて、翌年の母集団を返す。"""
    by_team: dict[int, list[Member]] = {}
    for member in members:
        by_team.setdefault(member.team_id, []).append(member)
    clubs = []
    for team_id, team_members in sorted(by_team.items()):
        playing = _playing_time(team_members)
        clubs.append(
            OffseasonClub(
                team_id=team_id,
                foreign_roster_limit=LEAGUE_FOREIGN_LIMIT,
                players=tuple(
                    OffseasonPlayer(
                        player_id=m.player_id,
                        position=m.position,
                        profile=m.profile,
                        number=m.number,
                        joined_year=m.joined_year,
                        ratings=m.ratings,
                        playing_time=playing.get(m.player_id, PlayingTime()),
                    )
                    for m in team_members
                ),
            )
        )
    plan = plan_offseason(clubs, year=year, world_seed=world_seed, start_year=START_YEAR, used_names=())

    retired = set(plan.retired)
    after = {change.player_id: change.after for change in plan.retained}
    next_members = [replace(m, ratings=after[m.player_id]) for m in members if m.player_id not in retired]
    next_id = max(m.player_id for m in members) + 1
    for offset, draftee in enumerate(plan.draftees):
        player_id = next_id + offset
        next_members.append(
            Member(
                player_id=player_id,
                team_id=draftee.team_id,
                position=draftee.position,
                profile=draftee.profile,
                number=draftee.number,
                joined_year=year + 1,
                ratings=PlayerRatings(player_id=player_id, year=year + 1, ratings=draftee.ratings),
            )
        )
    return next_members, plan


@dataclass(frozen=True)
class YearStats:
    year: int
    mean_age: float
    sd_age: float
    share_35_plus: float
    share_22_minus: float
    turnover: float
    min_roster: int
    max_roster: int
    anchor_mean_shift: float
    anchor_sd_shift: float
    stamina_mean: float
    stamina_sd: float


def stats(members: list[Member], plan: OffseasonPlan, before: int) -> YearStats:
    """Y 年を締めた後（Y+1 の開幕時点）の分布。"""
    as_of = date(plan.year + 1, 4, 1)
    ages = [m.profile.age(as_of) for m in members]
    assert all(age is not None for age in ages)
    values = [float(age) for age in ages if age is not None]
    sizes: dict[int, int] = {}
    for member in members:
        sizes[member.team_id] = sizes.get(member.team_id, 0) + 1
    staminas = [float(m.ratings.ratings.stamina) for m in members if isinstance(m.ratings.ratings, PitcherRatings)]
    return YearStats(
        year=plan.year,
        mean_age=fmean(values),
        sd_age=pstdev(values),
        share_35_plus=sum(1 for v in values if v >= 35) / len(values),
        share_22_minus=sum(1 for v in values if v <= 22) / len(values),
        turnover=len(plan.retired) / before,
        min_roster=min(sizes.values()),
        max_roster=max(sizes.values()),
        anchor_mean_shift=plan.anchor.mean_shift,
        anchor_sd_shift=plan.anchor.sd_shift,
        stamina_mean=fmean(staminas),
        stamina_sd=pstdev(staminas),
    )


def run_years(seed: int = 1, years: int = YEARS) -> list[YearStats]:
    members = make_population(seed)
    result = []
    for year in range(START_YEAR, START_YEAR + years):
        before = len(members)
        members, plan = run_year(members, year, world_seed=seed)
        result.append(stats(members, plan, before))
    return result


class OffseasonPopulationTest(unittest.TestCase):
    """合成した 48 球団 × 34 人を 10 年回して、分布が帯の内側に保たれる。"""

    years: list[YearStats]
    elapsed: float

    @classmethod
    def setUpClass(cls) -> None:
        started = time.perf_counter()
        cls.years = run_years()
        cls.elapsed = time.perf_counter() - started

    def test_runs_in_reasonable_time(self) -> None:
        """10年ぶんの計算（1,632人 × 10年）。実測は約1秒。遅い環境で落ちないよう余裕を持たせる。"""
        self.assertLess(self.elapsed, 3.0)

    def test_age_distribution_is_kept(self) -> None:
        """設計書の帯からの変更: 標準偏差の上限 4.5 → 4.8、22歳以下 5〜15% → 5〜20%。

        高校卒（18歳）を日本人の新人の30%入れる規則の定常状態は、22歳以下が約17%・標準偏差が約4.5になる
        （設計書 12.「P6a の詳細」）。平均年齢は 27〜29 歳に保たれる。
        """
        for stat in self.years:
            with self.subTest(year=stat.year):
                self.assertTrue(27.0 <= stat.mean_age <= 29.0, stat.mean_age)
                self.assertTrue(3.3 <= stat.sd_age <= 4.8, stat.sd_age)
                self.assertTrue(0.02 <= stat.share_35_plus <= 0.08, stat.share_35_plus)
                self.assertTrue(0.05 <= stat.share_22_minus <= 0.20, stat.share_22_minus)

    def test_turnover_is_about_a_tenth(self) -> None:
        for stat in self.years:
            with self.subTest(year=stat.year):
                self.assertTrue(0.09 <= stat.turnover <= 0.13, stat.turnover)

    def test_roster_sizes_stay_in_range(self) -> None:
        for stat in self.years:
            with self.subTest(year=stat.year):
                self.assertTrue(stat.min_roster >= 28 and stat.max_roster <= 40, (stat.min_roster, stat.max_roster))

    def test_anchor_correction_is_within_one_point(self) -> None:
        """錨の前の分布が目標から1点以上ずれるなら、曲線が合っていない（錨で歪みを隠さない）。"""
        for stat in self.years:
            with self.subTest(year=stat.year):
                self.assertLessEqual(stat.anchor_mean_shift, 1.0)
                self.assertLessEqual(stat.anchor_sd_shift, 1.0)

    def test_stamina_distribution_is_kept(self) -> None:
        """スタミナは錨（spread）の対象外なので、別に平均と標準偏差を保つ（合成の初期は 50.4 / 9.9）。"""
        for stat in self.years:
            with self.subTest(year=stat.year):
                self.assertTrue(47.0 <= stat.stamina_mean <= 54.0, stat.stamina_mean)
                self.assertTrue(8.0 <= stat.stamina_sd <= 12.0, stat.stamina_sd)

    def test_same_seed_gives_same_result(self) -> None:
        self.assertEqual(run_years(years=3), self.years[:3])


if __name__ == "__main__":
    unittest.main()
