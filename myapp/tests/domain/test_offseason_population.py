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

from myapp.domain.pennant.draft import MAX_DEVELOPMENT_PICKS, MAX_DRAFTEES, Draftee, DraftRoute
from myapp.domain.pennant.draft_meeting import DraftContext
from myapp.domain.pennant.draft_order import DraftOrderRule, DraftStandings
from myapp.domain.pennant.offseason import OffseasonClub, OffseasonPlan, OffseasonPlayer, plan_offseason
from myapp.domain.pennant.ratings import PlayerRatings
from myapp.domain.pennant.retirement import PlayingTime
from myapp.domain.pennant.roster import DEVELOPMENT_BAND, OPENING_REGISTERED_BAND
from myapp.domain.simulation.randomness import GameRandom, make_random, normal
from myapp.domain.simulation.ratings import BatterRatings, PitcherRatings, potential_multiplier
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
    # 今の契約区分（初期の母集団は全員支配下。育成ドラフトの新人は育成。#198 D2）
    contract: ContractStatus = ContractStatus.REGISTERED

    @property
    def is_registered(self) -> bool:
        return self.contract is ContractStatus.REGISTERED


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
        # 育成は試合に出ない（出場機会は支配下の中で合成する）
        playing = _playing_time([m for m in team_members if m.is_registered])
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
                        contract=m.contract,
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
                contract=draftee.contract,
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
    # 1球団が1年に迎えた支配下の新人（本指名と外国人）の最大
    max_draftees: int
    # 育成の人数（1球団の最少・最多・平均）と、1球団が1年に迎えた育成の新人の最大
    min_development: int
    max_development: int
    mean_development: float
    max_development_draftees: int
    anchor_mean_shift: float
    anchor_sd_shift: float
    stamina_mean: float
    stamina_sd: float
    # 支配下の伸びしろの倍率の平均と、伸びしろの無い選手の数
    # （#308。合成の初期母集団は伸びしろを持たず、1回目のオフで付く）
    potential_mean: float
    without_potential: int
    # 本指名の日本人の新人の伸びしろの倍率の平均と、経路（高校・大学・社会人）の割合
    # （#308 P2。AI が伸びしろの推定を見る）
    rookie_potential_mean: float
    rookie_route_shares: tuple[float, float, float]


def _main_draft_japanese(plan: OffseasonPlan) -> list[Draftee]:
    return [draftee for draftee in plan.draftees if not draftee.is_foreign and not draftee.is_development]


def stats(members: list[Member], plan: OffseasonPlan, previous: list[Member]) -> YearStats:
    """Y 年を締めた後（Y+1 の開幕時点）の分布。年齢・入れ替わり・人数・スタミナは**支配下**で測る
    （育成ドラフトの前と同じ物差し。育成は別に人数を測る。#198 D2）。"""
    as_of = date(plan.year + 1, 4, 1)
    registered = [m for m in members if m.is_registered]
    ages = [m.profile.age(as_of) for m in registered]
    assert all(age is not None for age in ages)
    values = [float(age) for age in ages if age is not None]
    sizes: dict[int, int] = {}
    development: dict[int, int] = {m.team_id: 0 for m in members}
    for member in members:
        if member.is_registered:
            sizes[member.team_id] = sizes.get(member.team_id, 0) + 1
        else:
            development[member.team_id] += 1
    staminas = [float(m.ratings.ratings.stamina) for m in registered if isinstance(m.ratings.ratings, PitcherRatings)]
    before = [m.player_id for m in previous if m.is_registered]
    rookies = _main_draft_japanese(plan)
    retired = len(set(plan.retired) & set(before))
    released = len(set(plan.released) & set(before))
    return YearStats(
        year=plan.year,
        mean_age=fmean(values),
        sd_age=pstdev(values),
        share_35_plus=sum(1 for v in values if v >= 35) / len(values),
        share_22_minus=sum(1 for v in values if v <= 22) / len(values),
        turnover=(retired + released) / len(before),
        retired_share=retired / len(before),
        released_share=released / len(before),
        min_roster=min(sizes.values()),
        max_roster=max(sizes.values()),
        mean_roster=fmean(sizes.values()),
        max_draftees=max(
            Counter(draftee.team_id for draftee in plan.draftees if not draftee.is_development).values(), default=0
        ),
        min_development=min(development.values()),
        max_development=max(development.values()),
        mean_development=fmean(development.values()),
        max_development_draftees=max(
            Counter(draftee.team_id for draftee in plan.draftees if draftee.is_development).values(), default=0
        ),
        anchor_mean_shift=plan.anchor.mean_shift,
        anchor_sd_shift=plan.anchor.sd_shift,
        stamina_mean=fmean(staminas),
        stamina_sd=pstdev(staminas),
        potential_mean=fmean(potential_multiplier(m.ratings.ratings.potential) for m in registered),
        without_potential=sum(1 for m in members if m.ratings.ratings.potential is None),
        rookie_potential_mean=fmean(potential_multiplier(d.ratings.potential) for d in rookies),
        rookie_route_shares=(
            sum(1 for d in rookies if d.route is DraftRoute.HIGH_SCHOOL) / len(rookies),
            sum(1 for d in rookies if d.route is DraftRoute.UNIVERSITY) / len(rookies),
            sum(1 for d in rookies if d.route is DraftRoute.CORPORATE) / len(rookies),
        ),
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
        previous = members
        members, plan = run_year(members, year, world_seed=seed, rule=rule)
        result.append(stats(members, plan, previous))
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
        育成の巡と育成の母集団（#198 D2）で手元 約8秒（シード 1〜4 で 7.7〜8.2秒）。上限は変えていない。
        その後の #302・#308 P1 で手元 約9.5秒になり、#308 P2（推定に伸びしろ・見込みの評価）で手元 約12秒・
        CI のランナーで 25.3秒になって上限を超えた。能力の検査で `fields()` を引くのをやめ、将来値を覚えるようにして
        手元 約11秒まで戻したが、CI は手元の約2倍かかり余裕が無いので、上限を 25 → 35 秒にした。
        """
        self.assertLess(self.elapsed, 35.0)

    def test_developmental_rosters_grow_to_the_target_band(self) -> None:
        """育成0人から、育成ドラフト（1年6人まで）で球団ごとの目標（帯 10〜30人の中）へ届き、帯を超えない（#198 D2）。

        実測（シード 1〜4）は5回目のオフの後に全球団が目標に届き、平均 18.8〜21.3人。支配下の帯・年齢・入れ替わり・
        錨（支配下で測る）は育成ドラフトの前と同じ範囲。
        """
        band = DEVELOPMENT_BAND
        for index, stat in enumerate(self.years):
            with self.subTest(year=stat.year):
                self.assertLessEqual(stat.max_development, band.high)
                self.assertLessEqual(stat.max_development_draftees, MAX_DEVELOPMENT_PICKS)
                if index >= 4:
                    self.assertGreaterEqual(stat.min_development, band.low)
                    self.assertTrue(band.low + 5 <= stat.mean_development <= band.high - 5, stat.mean_development)

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

    def test_potential_stays_about_average(self) -> None:
        """伸びしろ（#308 P1）: 1回目のオフで全員に付き、支配下の倍率の平均は 1.0 の近くに保たれる。

        伸びしろの大きい選手は伸びて残りやすいので、平均はわずかに上へ寄る（引退・戦力外は伸びしろを見ないが、
        能力を見る）。新人は事前の分布（平均 1.0）で入るので、寄りは止まる。実測（シード 1〜4）は 0.994〜1.007。
        """
        for stat in self.years:
            with self.subTest(year=stat.year):
                self.assertEqual(stat.without_potential, 0)
                self.assertTrue(0.98 <= stat.potential_mean <= 1.03, stat.potential_mean)

    def test_drafted_rookies_keep_the_average_potential_and_the_route_shares(self) -> None:
        """#308 P2: AI は伸びしろの推定の大きい候補を上に見るが、候補の伸びしろの平均（`POOL_POTENTIAL_MEAN`）を
        下げてあるので、本指名の日本人の新人の倍率の平均は約1.0（年齢曲線を引き直さずに平衡を保つ条件）。
        経路の割合も今までどおり 30/50/20 の近く（`POOL_ROUTE_WEIGHTS` の合わせ込み）。

        実測（シード 1〜8・2〜3年目の合計）は倍率 0.997〜0.999・高校 30〜31%・大学 49〜51%・社会人 19〜20%。
        年ごと（1年 約290人）の揺れを見込んだ帯。
        """
        for stat in self.years:
            high_school, university, corporate = stat.rookie_route_shares
            with self.subTest(year=stat.year):
                self.assertTrue(0.93 <= stat.rookie_potential_mean <= 1.07, stat.rookie_potential_mean)
                self.assertTrue(0.20 <= high_school <= 0.40, high_school)
                self.assertTrue(0.40 <= university <= 0.60, university)
                self.assertTrue(0.12 <= corporate <= 0.28, corporate)
        self.assertAlmostEqual(fmean(stat.rookie_potential_mean for stat in self.years), 1.0, delta=0.03)

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

        標準偏差のずれは1回目のオフだけ 1.0 → 1.3 点（#308 P2）: 合成の初期母集団（能力が年齢と無関係）から新人を
        10人ずつ迎える年で、シードの揺れが大きい（シード 1〜8 の1回目は P2 の前 0.47〜1.05・後 0.51〜1.21 点。
        2〜3回目は前後とも 0.29〜0.74 点）。
        """
        for index, stat in enumerate(self.years):
            with self.subTest(year=stat.year):
                self.assertTrue(25.0 <= stat.mean_age <= 29.0, stat.mean_age)
                self.assertTrue(3.3 <= stat.sd_age <= 5.2, stat.sd_age)
                self.assertTrue(0.01 <= stat.share_35_plus <= 0.08, stat.share_35_plus)
                self.assertTrue(0.05 <= stat.share_22_minus <= 0.32, stat.share_22_minus)
                self.assertLessEqual(stat.anchor_mean_shift, 2.5)
                self.assertLessEqual(stat.anchor_sd_shift, 1.3 if index == 0 else 1.0)


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
