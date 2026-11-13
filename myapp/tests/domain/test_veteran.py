"""既にプロにいる選手の組み立て（`virtual_players/veteran.py` の `draw_veteran`）。DB 不要。

規則の出典はモジュールの docstring と、実データへの投入（Wiki「仮想データの投入」）: 年齢は契約区分の帯の中、
日本人はプロ入りの前年の秋のドラフトで指名され（支配下は本指名・育成は育成ドラフト）、加入はその翌年、
順位は球団 × ドラフト年 × 本指名/育成ごとに重ならない。外国人はドラフトを持たず経路は新外国人で、支配下だけ。
育成ドラフトの選手は社会人の経歴を持たない（#307）。
"""

from __future__ import annotations

import unittest
from collections import Counter
from datetime import date

from myapp.domain.value_objects import AcquisitionRoute, ContractStatus, Position
from myapp.domain.virtual_players import generator
from myapp.domain.virtual_players.veteran import GeneratedPlayer, draw_veteran

from .doubles import OnlyRandom

AS_OF = date(2027, 4, 1)
SEEDS = range(300)


def draw(
    seed: int,
    *,
    contract: ContractStatus = ContractStatus.REGISTERED,
    is_foreign: bool = False,
    position: Position = Position.PITCHER,
) -> GeneratedPlayer:
    return draw_veteran(
        OnlyRandom(seed),
        position,
        contract=contract,
        is_foreign=is_foreign,
        as_of=AS_OF,
        used_names=set(),
        draft_rounds=Counter(),
    )


class DrawVeteranTest(unittest.TestCase):
    def test_runs_on_random_only_and_repeats_for_the_same_source(self) -> None:
        """`random()` だけを使う（`OnlyRandom` は他のメソッドを持たない。`rng.gauss` は使わない）。

        同じ乱数源なら同じ選手。
        """
        for contract, is_foreign in (
            (ContractStatus.REGISTERED, False),
            (ContractStatus.DEVELOPMENTAL, False),
            (ContractStatus.REGISTERED, True),
        ):
            with self.subTest(contract=contract, is_foreign=is_foreign):
                self.assertEqual(
                    draw(5, contract=contract, is_foreign=is_foreign),
                    draw(5, contract=contract, is_foreign=is_foreign),
                )

    def test_age_stays_within_the_contract_band(self) -> None:
        for contract, (_, _, youngest, oldest) in (
            (ContractStatus.REGISTERED, generator.REGISTERED_AGE),
            (ContractStatus.DEVELOPMENTAL, generator.DEVELOPMENTAL_AGE),
        ):
            ages = [draw(seed, contract=contract).profile.age(AS_OF) for seed in SEEDS]
            with self.subTest(contract=contract):
                self.assertTrue(all(age is not None and youngest <= age <= oldest for age in ages), ages)
                # 帯の中で散らばる（1つの年齢に張り付かない）
                self.assertGreater(len(set(ages)), 5)

    def test_japanese_players_join_the_year_after_their_draft(self) -> None:
        """加入はドラフトの翌年（`DraftPick.entry_year`）で、`as_of` の年を超えない。プロ入りは18〜30歳。"""
        for contract, route, is_development in (
            (ContractStatus.REGISTERED, AcquisitionRoute.DRAFT, False),
            (ContractStatus.DEVELOPMENTAL, AcquisitionRoute.DEVELOPMENTAL_DRAFT, True),
        ):
            for seed in SEEDS:
                player = draw(seed, contract=contract)
                draft = player.profile.draft
                with self.subTest(contract=contract, seed=seed):
                    assert draft is not None
                    self.assertEqual(draft.is_development, is_development)
                    self.assertEqual(player.from_year, draft.year + 1)
                    self.assertEqual(player.acquired_via, route)
                    self.assertLessEqual(player.from_year, AS_OF.year)
                    assert player.profile.birth_date is not None
                    self.assertTrue(18 <= player.from_year - player.profile.birth_date.year <= 30)
                    self.assertFalse(player.is_foreign)
                    self.assertEqual(player.profile.nationality, "")

    def test_developmental_players_have_no_corporate_career(self) -> None:
        """社会人の選手は育成で指名しない（#307）。"""
        careers = [draw(seed, contract=ContractStatus.DEVELOPMENTAL).profile for seed in SEEDS]
        self.assertTrue(all(profile.corporate_team == "" for profile in careers))
        # 支配下には社会人の経歴の選手もいる（経路を絞るのは育成だけ）
        self.assertTrue(any(draw(seed).profile.corporate_team for seed in SEEDS))

    def test_draft_rounds_follow_the_given_counter_per_year_and_kind(self) -> None:
        """順位は渡された（ドラフト年, 育成か）ごとの最後の順位の次。同じ球団で何人作っても重ならない。"""
        rng, used, rounds = OnlyRandom(11), set[str](), Counter({(2020, False): 4})
        picks = []
        for index in range(120):
            contract = ContractStatus.DEVELOPMENTAL if index % 3 == 0 else ContractStatus.REGISTERED
            player = draw_veteran(
                rng,
                Position.OUTFIELDER,
                contract=contract,
                is_foreign=False,
                as_of=AS_OF,
                used_names=used,
                draft_rounds=rounds,
            )
            assert player.profile.draft is not None
            picks.append(player.profile.draft)

        keys = Counter((pick.year, pick.is_development, pick.round) for pick in picks)
        self.assertTrue(all(count == 1 for count in keys.values()))
        for (year, is_development), last in rounds.items():
            drawn = sorted(p.round or 0 for p in picks if (p.year, p.is_development) == (year, is_development))
            start = 5 if (year, is_development) == (2020, False) else 1
            with self.subTest(year=year, is_development=is_development):
                self.assertEqual(drawn, list(range(start, last + 1)))

    def test_foreign_players_have_no_draft_and_carry_their_country(self) -> None:
        for seed in SEEDS:
            player = draw(seed, is_foreign=True, position=Position.INFIELDER)
            profile = player.profile
            with self.subTest(seed=seed):
                self.assertTrue(player.is_foreign)
                self.assertIsNone(profile.draft)
                self.assertEqual(player.acquired_via, AcquisitionRoute.NEW_FOREIGN)
                self.assertEqual(profile.nationality, profile.birthplace)
                self.assertNotEqual(profile.nationality, "")
                self.assertEqual((profile.high_school, profile.university, profile.corporate_team), ("", "", ""))
                self.assertLessEqual(player.from_year, AS_OF.year)

    def test_foreign_players_are_registered_only(self) -> None:
        with self.assertRaises(ValueError):
            draw(1, contract=ContractStatus.DEVELOPMENTAL, is_foreign=True)

    def test_names_are_unique_and_recorded(self) -> None:
        rng, used = OnlyRandom(3), set[str]()
        names = [
            draw_veteran(
                rng,
                Position.CATCHER,
                contract=ContractStatus.REGISTERED,
                is_foreign=index % 4 == 0,
                as_of=AS_OF,
                used_names=used,
                draft_rounds=Counter(),
            ).name
            for index in range(200)
        ]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(used, set(names))

    def test_back_name_is_left_to_the_team(self) -> None:
        """背ネームは球団の中の同姓で決まるので空。ローマ字は持つ（`generator.back_name` に渡す）。"""
        player = draw(2)
        self.assertEqual(player.profile.back_name, "")
        self.assertTrue(player.surname_romaji)
        self.assertTrue(player.given_romaji)


if __name__ == "__main__":
    unittest.main()
