"""オフを10年回した母集団の分布（合成した球団）。

曲線・引退・ドラフトの個々の規則は `test_aging` / `test_retirement` / `test_draft` が見る。ここは
**組み合わせたときに分布が保たれるか**を見る。年齢の平均が保たれる条件は
`入れ替わりの割合 × (引退の平均年齢 − 新人の平均年齢) ≈ 1歳` で、どれかの倍率を変えると崩れる。
曲線や倍率を直したら、このテストで帯の内側に入ることを確かめる（3シーズンの実測の代わり）。

3つの場面を見る（#167 で開幕前の支配下の帯を 62〜66人にしてから。3つ目は #168 の戦力外から）:
- **定常状態**（48 球団 × 64 人から）: 年齢・入れ替わり（引退＋戦力外）・錨・人数が帯の中に保たれる
- **帯に追いつく途中**（12 球団 × 34 人から。実データから分岐した球団の人数）: 人数が上端を超えずに帯へ届く
- **上端を超えた球団**（48 球団 × 70 人から）: 1回目のオフの戦力外で帯へ戻る

帯のうち、錨の補正（3年目から 1.2 点以内）は、シードの実測に近い値で、規則の小さな変更でも落ちうる。
落ちたら、曲線の誤りか、シードの揺れ（別のシードでも落ちるか）かを切り分ける。Wiki「ペナントモードの仕組み」の「実測値」に
シード 1〜4 の範囲がある。
"""

from __future__ import annotations

import time
import unittest
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date
from statistics import fmean, pstdev

from myapp.domain.pennant.draft import MAX_DRAFTEES
from myapp.domain.pennant.draft_meeting import DraftContext
from myapp.domain.pennant.draft_order import DraftOrderRule, DraftStandings
from myapp.domain.pennant.offseason import OffseasonClub, OffseasonPlan, OffseasonPlayer, plan_offseason
from myapp.domain.pennant.ratings import PlayerRatings
from myapp.domain.pennant.retirement import PlayingTime
from myapp.domain.pennant.roster import OPENING_REGISTERED_BAND
from myapp.domain.simulation.randomness import GameRandom, make_random, normal
from myapp.domain.simulation.ratings import BatterRatings, PitcherRatings
from myapp.domain.simulation.samples import BATTER_MEAN, BATTER_SD, PITCHER_MEAN, PITCHER_SD, draw_ratings
from myapp.domain.value_objects import ContractStatus, DraftPick, ForeignPlayerRules, Position, Profile
from myapp.domain.virtual_players import generator

START_YEAR = 2026
TEAMS = 48
YEARS = 10
LEAGUE_FOREIGN_RULES = ForeignPlayerRules.NPB
LEAGUE_REGISTERED_LIMIT = 70
# 実データから分岐した球団の支配下の人数（開発用 DB の平均 約34人）と、帯の中の人数（定常状態から始める）
INITIAL_ROSTER = 34
STEADY_ROSTER = 64
# 帯に追いつく途中を見るときの球団数（人数の推移だけを見るので少なくてよい）
GROWTH_TEAMS = 12
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


def make_population(seed: int = 1, roster_size: int = INITIAL_ROSTER, teams: int = TEAMS) -> list[Member]:
    rng = make_random(seed)
    members: list[Member] = []
    positions = [
        position
        for position, count in generator.largest_remainder(roster_size, generator.POSITION_RATIOS).items()
        for _ in range(count)
    ]
    for team_id in range(1, teams + 1):
        for number, position in enumerate(positions, start=1):
            age = _age_distribution(rng)
            is_foreign = rng.random() < generator.FOREIGN_PLAYER_RATIO
            birth_date = generator.birth_date_for(rng, age=age, as_of=date(START_YEAR, 4, 1))
            # プロ入りの年。日本人はその前年のドラフト、外国人はドラフトなし（在籍の開始年がプロ入り）
            entered = START_YEAR - generator.randint(rng, 0, max(0, age - 19))
            profile = Profile(
                birth_date=birth_date,
                draft=None if is_foreign else DraftPick(entered - 1),
                is_foreign_player=is_foreign,
            )
            mean, sd = (PITCHER_MEAN, PITCHER_SD) if position.is_pitcher else (BATTER_MEAN, BATTER_SD)
            ratings = draw_ratings(rng, mean=mean, sd=sd, position=position, stamina_mean=50.0)
            player_id = len(members) + 1
            members.append(
                Member(
                    player_id=player_id,
                    team_id=team_id,
                    position=position,
                    profile=profile,
                    number=str(number),
                    joined_year=entered,
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


def run_year(
    members: list[Member], year: int, *, world_seed: int = 1, rule: DraftOrderRule = DraftOrderRule.NPB
) -> tuple[list[Member], OffseasonPlan]:
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
                foreign_rules=LEAGUE_FOREIGN_RULES,
                registered_player_limit=LEAGUE_REGISTERED_LIMIT,
                players=tuple(
                    OffseasonPlayer(
                        player_id=m.player_id,
                        position=m.position,
                        profile=m.profile,
                        number=m.number,
                        joined_year=m.joined_year,
                        ratings=m.ratings,
                        contract=ContractStatus.REGISTERED,
                        playing_time=playing.get(m.player_id, PlayingTime()),
                    )
                    for m in team_members
                ),
            )
        )
    # 指名順の最終順位は1リーグ・球団 id の順（合成の母集団に順位は無い。規則ごとの違いは `test_draft_meeting` が見る）
    standings = DraftStandings(leagues=(tuple(sorted(by_team)),))
    plan = plan_offseason(
        clubs,
        year=year,
        world_seed=world_seed,
        start_year=START_YEAR,
        used_names=(),
        draft=DraftContext(rule, standings),
    )

    leaving = set(plan.retired) | set(plan.released)
    after = {change.player_id: change.after for change in plan.retained}
    next_members = [replace(m, ratings=after[m.player_id]) for m in members if m.player_id not in leaving]
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
    # 入れ替わり（引退＋戦力外）と、その内訳
    turnover: float
    retired_share: float
    released_share: float
    min_roster: int
    max_roster: int
    mean_roster: float
    # 1球団が1年に迎えた新人の最大
    max_draftees: int
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
        turnover=(len(plan.retired) + len(plan.released)) / before,
        retired_share=len(plan.retired) / before,
        released_share=len(plan.released) / before,
        min_roster=min(sizes.values()),
        max_roster=max(sizes.values()),
        mean_roster=fmean(sizes.values()),
        max_draftees=max(Counter(draftee.team_id for draftee in plan.draftees).values(), default=0),
        anchor_mean_shift=plan.anchor.mean_shift,
        anchor_sd_shift=plan.anchor.sd_shift,
        stamina_mean=fmean(staminas),
        stamina_sd=pstdev(staminas),
    )


def run_years(
    seed: int = 1,
    years: int = YEARS,
    *,
    roster_size: int = INITIAL_ROSTER,
    teams: int = TEAMS,
    rule: DraftOrderRule = DraftOrderRule.NPB,
) -> list[YearStats]:
    members = make_population(seed, roster_size, teams)
    result = []
    for year in range(START_YEAR, START_YEAR + years):
        before = len(members)
        members, plan = run_year(members, year, world_seed=seed, rule=rule)
        result.append(stats(members, plan, before))
    return result


class OffseasonPopulationTest(unittest.TestCase):
    """合成した 48 球団 × 64 人（開幕前の支配下の帯の中）を 10 年回して、分布が帯の内側に保たれる。"""

    years: list[YearStats]
    elapsed: float

    @classmethod
    def setUpClass(cls) -> None:
        # 壁時計ではなくこのプロセスの CPU 時間で測る。並列実行やほかのエージェントのテストと重なると、
        # CPU を待たされた時間が入って壁時計では 30 秒を超えたことがある（#184）
        started = time.process_time()
        cls.years = run_years(roster_size=STEADY_ROSTER)
        cls.elapsed = time.process_time() - started

    def test_runs_in_reasonable_time(self) -> None:
        """10年ぶんの計算（約3,000人 × 10年）の CPU 時間。遅い環境で落ちないよう余裕を持たせる。

        実測は約1.4秒だったが、ドラフト会議（#198 D1。48球団の候補 約1,200人を球団ごとのスカウトの推定で見比べる）で
        1年あたり約0.45秒増え、手元で約6秒・CI のランナーで約14秒になった（上限を 4 → 25 秒）。
        """
        self.assertLess(self.elapsed, 25.0)

    def test_age_distribution_is_kept(self) -> None:
        """当初の帯からの変更: 標準偏差の上限 4.5 → 4.8、22歳以下 5〜15% → 5〜22%、平均年齢の下限 27 → 26、
        35歳以上の下限 2% → 1.5%。

        高校卒（18歳）を日本人の新人の30%入れる規則の定常状態は、22歳以下が約17〜20%・標準偏差が約4.5になる
        （Wiki「ペナントモードの仕組み」の「実測値」）。戦力外を足して引退の表を下げてから（#168）は、
        平均年齢 27.3〜28.1 歳・35歳以上 3.5〜7.5%・22歳以下 11.1〜16.2%（シード 1〜4。帯は変えていない）。
        """
        for stat in self.years:
            with self.subTest(year=stat.year):
                self.assertTrue(26.0 <= stat.mean_age <= 29.0, stat.mean_age)
                self.assertTrue(3.3 <= stat.sd_age <= 4.8, stat.sd_age)
                self.assertTrue(0.015 <= stat.share_35_plus <= 0.08, stat.share_35_plus)
                self.assertTrue(0.05 <= stat.share_22_minus <= 0.22, stat.share_22_minus)

    def test_turnover_is_about_a_tenth(self) -> None:
        """入れ替わり（引退＋戦力外）の割合。約10%（NPB の退団の水準）。

        #168 から、戦力外がふだんの新人（6人）を迎える空きを作るので、入れ替わりは新人の数でほぼ決まる（6 / 約65人）。
        平均 9.4〜9.6%・年ごとに 6.8〜12.6%（シード 1〜4）。
        うち引退 6.2〜6.6%・戦力外 3.0〜3.2%（外国人の入れ替えを含む）。
        下限を 8% → 6.5% に下げた（前の年に下端を割った球団は空きがあるので戦力外が0人になり、その年だけ少ない。
        1年目の外国人に余裕を持たせてから最小 6.8%）。
        """
        for stat in self.years:
            with self.subTest(year=stat.year):
                self.assertTrue(0.065 <= stat.turnover <= 0.13, stat.turnover)
                self.assertGreater(stat.released_share, 0.0, "戦力外がある")
        # 1年目は合成の初期母集団（外国人の数が球団ごとにばらつく）の入れ替えで戦力外が多い。10年の平均で比べる
        retired = fmean(stat.retired_share for stat in self.years)
        released = fmean(stat.released_share for stat in self.years)
        self.assertGreater(retired, released, "引退のほうが多い（#168 の調整）")

    def test_opening_rosters_stay_in_the_band(self) -> None:
        """開幕前の支配下は上端（66人）を超えず、平均は帯の中。引退が多い年の球団だけ下端を割る（1年の上限10人）。"""
        band = OPENING_REGISTERED_BAND
        for stat in self.years:
            with self.subTest(year=stat.year):
                self.assertLessEqual(stat.max_roster, band.high)
                self.assertTrue(band.low <= stat.mean_roster <= band.high, stat.mean_roster)
                self.assertGreaterEqual(stat.min_roster, band.low - 7)
                self.assertLessEqual(stat.max_draftees, MAX_DRAFTEES)

    def test_anchor_correction_is_small(self) -> None:
        """錨の前の分布が目標から1点以上ずれるなら、曲線が合っていない（錨で歪みを隠さない）。

        合成の初期母集団は能力が年齢と無関係で、曲線と釣り合うまで2〜3年かかる。最初の2年は 1.5 点まで許す。
        3年目からは 1.0 → 1.2 点（#168）: 戦力外は残す価値の低い順に外すので、錨の前の分布が少し上に寄る
        （シード 1〜4 の最大 0.84〜1.06 点）。
        """
        for index, stat in enumerate(self.years):
            limit = 1.5 if index < 2 else 1.2
            with self.subTest(year=stat.year):
                self.assertLessEqual(stat.anchor_mean_shift, limit)
                self.assertLessEqual(stat.anchor_sd_shift, 1.0)

    def test_stamina_distribution_is_kept(self) -> None:
        """スタミナは錨（spread）の対象外なので、別に平均と標準偏差を保つ（合成の初期は約 50 / 10）。"""
        for stat in self.years:
            with self.subTest(year=stat.year):
                self.assertTrue(47.0 <= stat.stamina_mean <= 54.0, stat.stamina_mean)
                self.assertTrue(8.0 <= stat.stamina_sd <= 12.0, stat.stamina_sd)

    def test_same_seed_gives_same_result(self) -> None:
        self.assertEqual(run_years(years=3, roster_size=STEADY_ROSTER), self.years[:3])


class RosterGrowthTest(unittest.TestCase):
    """実データから分岐した球団（支配下 約34人）が、上端を超えずに開幕前の帯（62〜66人）へ届く。

    1年に迎えるのは10人まで（`MAX_DRAFTEES`）なので、引退を差し引いて年に約6人ずつ増え、6回目のオフで全球団が帯に入る
    （シード 1〜4 とも 2031年を締めた後。Wiki「ペナントモードの仕組み」の「実測値」）。
    """

    years: list[YearStats]

    @classmethod
    def setUpClass(cls) -> None:
        cls.years = run_years(roster_size=INITIAL_ROSTER, teams=GROWTH_TEAMS)

    def test_rosters_grow_without_passing_the_upper_end(self) -> None:
        for stat in self.years:
            with self.subTest(year=stat.year):
                self.assertLessEqual(stat.max_roster, OPENING_REGISTERED_BAND.high)
                self.assertLessEqual(stat.max_draftees, MAX_DRAFTEES)
        means = [stat.mean_roster for stat in self.years]
        self.assertEqual(means[:5], sorted(means[:5]), "帯に届くまでは毎年増える")

    def test_every_club_reaches_the_band(self) -> None:
        reached = [stat.year for stat in self.years if stat.min_roster >= OPENING_REGISTERED_BAND.low]
        self.assertTrue(reached, "10回のオフのうちに全球団が帯に入る")
        self.assertLessEqual(reached[0], START_YEAR + 5)
        for stat in self.years:
            if stat.year >= reached[0]:
                with self.subTest(year=stat.year):
                    self.assertTrue(OPENING_REGISTERED_BAND.low <= stat.mean_roster, stat.mean_roster)

    def test_age_and_anchor_stay_within_loose_bands_while_catching_up(self) -> None:
        """追いつく途中は新人の割合が高く、若く偏る。定常状態より緩い帯で、崩れすぎていないことだけを見る。

        実測（シード 1〜4。#168 の戦力外と引退の表の後）は平均年齢 26.0〜27.2・22歳以下 12〜25%・35歳以上 2.2〜5.1%・
        錨の平均のずれ 最大 1.81 点・標準偏差のずれ 最大 0.98 点（テストはシード 1）。
        """
        for stat in self.years:
            with self.subTest(year=stat.year):
                self.assertTrue(25.0 <= stat.mean_age <= 29.0, stat.mean_age)
                self.assertTrue(3.3 <= stat.sd_age <= 5.2, stat.sd_age)
                self.assertTrue(0.01 <= stat.share_35_plus <= 0.08, stat.share_35_plus)
                self.assertTrue(0.05 <= stat.share_22_minus <= 0.32, stat.share_22_minus)
                self.assertLessEqual(stat.anchor_mean_shift, 2.5)
                self.assertLessEqual(stat.anchor_sd_shift, 1.0)


class OverfullRosterTest(unittest.TestCase):
    """帯の上端を超えた球団（48 球団 × 支配下の上限 70 人から）を、戦力外が帯へ戻す（#168）。

    1回目のオフで、引退の後に残った人数から、ふだんの新人（6人）を迎えても上端（66人）を超えない人数まで外す。
    実測（シード 1〜4）は1回目の入れ替わり 約15%（うち戦力外 約8%）、その後は定常状態と同じ（平均 約10%）。
    """

    years: list[YearStats]

    @classmethod
    def setUpClass(cls) -> None:
        cls.years = run_years(roster_size=LEAGUE_REGISTERED_LIMIT)

    def test_rosters_come_back_into_the_band_at_the_first_offseason(self) -> None:
        first = self.years[0]
        self.assertLessEqual(first.max_roster, OPENING_REGISTERED_BAND.high)
        self.assertGreaterEqual(first.min_roster, OPENING_REGISTERED_BAND.low)
        self.assertGreater(first.released_share, first.retired_share, "1回目は戦力外のほうが多い")
        for stat in self.years:
            with self.subTest(year=stat.year):
                self.assertLessEqual(stat.max_roster, OPENING_REGISTERED_BAND.high)

    def test_distribution_stays_within_the_steady_bands(self) -> None:
        for stat in self.years:
            with self.subTest(year=stat.year):
                self.assertTrue(26.0 <= stat.mean_age <= 29.0, stat.mean_age)
                self.assertTrue(0.015 <= stat.share_35_plus <= 0.08, stat.share_35_plus)
                self.assertTrue(0.07 <= stat.turnover <= 0.16, stat.turnover)
                self.assertLessEqual(stat.anchor_mean_shift, 1.5)
                self.assertLessEqual(stat.anchor_sd_shift, 1.0)


if __name__ == "__main__":
    unittest.main()
