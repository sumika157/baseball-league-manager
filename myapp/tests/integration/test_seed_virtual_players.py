"""仮想選手の投入コマンド（seed_virtual_players）。

ORM に直接書き込むため Team 集約の検査を通らない。背番号の一意性・支配下の上限
など、集約が守る不変条件を投入結果が満たしているかをここで確かめる。
"""

from collections import Counter, defaultdict
from datetime import date
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from myapp.domain.entities import ensure_draft_fits_career
from myapp.domain.pennant.roster import DEVELOPMENT_BAND, OPENING_REGISTERED_BAND, RegisteredBand
from myapp.domain.value_objects import AcquisitionRoute, ContractStatus
from myapp.domain.virtual_players import generator
from myapp.infrastructure import orm_models
from myapp.infrastructure.repositories import DjangoTeamRepository
from myapp.management.commands import seed_virtual_players
from myapp.management.commands.seed_virtual_players import ORIGINAL_PLAYER_READINGS

from ..helpers import DEFAULT_WORKSPACE_KEY, real_scope
from .base import BaseCase

COMMAND = "seed_virtual_players"


def run(*args) -> str:
    out = StringIO()
    call_command(COMMAND, "--workspace", DEFAULT_WORKSPACE_KEY, "--seed", "1", *args, stdout=out)
    return out.getvalue()


def add_player(team, name, *, number, is_foreign=False, from_year=2020, **fields) -> orm_models.Player:
    player = orm_models.Player.objects.create(
        name=name,
        is_foreign_player=is_foreign,
        nationality="アメリカ合衆国" if is_foreign else "",
        **fields,
    )
    orm_models.PlayerStint.objects.create(player=player, team=team, number=number, from_year=from_year)
    return player


def active_stints(team) -> list[orm_models.PlayerStint]:
    return list(orm_models.PlayerStint.objects.filter(team=team, to_year__isnull=True).select_related("player"))


def contract_of(stint: orm_models.PlayerStint) -> ContractStatus:
    return ContractStatus.now(ContractStatus.from_label(stint.signed_as), stint.promoted_year)


def split_by_contract(team) -> tuple[list[orm_models.PlayerStint], list[orm_models.PlayerStint]]:
    """(支配下, 育成) の在籍。"""
    stints = active_stints(team)
    registered = [s for s in stints if contract_of(s) is ContractStatus.REGISTERED]
    return registered, [s for s in stints if s not in registered]


class SeedRosterTest(BaseCase):
    """新規投入（オプションなし）。"""

    def setUp(self):
        super().setUp()
        self.original = add_player(self.team, "藤井健吾", number=10)

    def test_fills_each_team_within_the_bands_and_keeps_existing(self):
        run()

        for team in (self.team, self.rival):
            registered, developmental = split_by_contract(team)
            with self.subTest(team=team.name):
                # 支配下はペナントの開幕前の帯（出典を共有する）、育成は投入の範囲
                self.assertTrue(OPENING_REGISTERED_BAND.low <= len(registered) <= OPENING_REGISTERED_BAND.high)
                self.assertTrue(DEVELOPMENT_BAND.low <= len(developmental) <= DEVELOPMENT_BAND.high)
        # 既存の選手と在籍には手を付けない（追加専用）
        self.original.refresh_from_db()
        self.assertEqual(self.original.name, "藤井健吾")
        stint = orm_models.PlayerStint.objects.get(player=self.original)
        self.assertEqual((stint.team_id, stint.number, stint.to_year), (self.team.id, "10", None))
        self.assertIsNone(stint.acquired_via)

    def test_jersey_numbers_are_unique_among_active_players(self):
        run()

        for team in (self.team, self.rival):
            numbers = [s.number for s in active_stints(team)]
            with self.subTest(team=team.name):
                self.assertEqual(len(numbers), len(set(numbers)))
                self.assertTrue(all(0 <= int(n) <= 999 for n in numbers))

    def test_numbers_and_routes_follow_the_contract(self):
        """支配下は 00・0〜99、育成は3桁。経路は作った経路（区分と食い違わない）。

        以前は支配下の番号が尽きると 100〜999 を支配下に配っていた（契約区分の規則に反する）。
        """
        run()

        for team in (self.team, self.rival):
            registered, developmental = split_by_contract(team)
            for stint in registered:
                if stint.player == self.original:
                    continue
                with self.subTest(team=team.name, number=stint.number):
                    self.assertLess(int(stint.number), 100)
                    expected = (
                        AcquisitionRoute.NEW_FOREIGN if stint.player.is_foreign_player else AcquisitionRoute.DRAFT
                    )
                    self.assertEqual(stint.acquired_via, expected.value)
            for stint in developmental:
                with self.subTest(team=team.name, number=stint.number):
                    self.assertGreaterEqual(int(stint.number), 100)
                    self.assertEqual(stint.signed_as, ContractStatus.DEVELOPMENTAL.value)
                    self.assertIsNone(stint.promoted_year)
                    self.assertEqual(stint.acquired_via, AcquisitionRoute.DEVELOPMENTAL_DRAFT.value)
                    # 助っ人は支配下だけ（1球団の外国人の上限は育成も数えるので、上限を支配下に回す）
                    self.assertFalse(stint.player.is_foreign_player)
            # 集約として読み戻しても区分の規則に通る
            DjangoTeamRepository(real_scope()).find_by_id(team.id)

    def test_registered_stays_within_the_league_limit(self):
        self.league.registered_player_limit = 40
        self.league.save()

        run()

        for team in (self.team, self.rival):
            registered, developmental = split_by_contract(team)
            with self.subTest(team=team.name):
                self.assertEqual(len(registered), 40)
                self.assertTrue(developmental)

    def test_stops_when_registered_numbers_run_out(self):
        """支配下の番号（101個）より多く要るときは、3桁を配らずに止める（何も書かない）。"""
        self.league.registered_player_limit = None
        self.league.save()

        with (
            mock.patch.object(seed_virtual_players, "OPENING_REGISTERED_BAND", RegisteredBand(low=110, high=110)),
            self.assertRaisesMessage(CommandError, "支配下の背番号が足りません"),
        ):
            run()

        self.assertEqual(orm_models.Player.objects.count(), 1)

    def test_running_again_stays_within_the_bands(self):
        """埋まった球団に流し直しても、帯を超えて足さない（今のデータに追加で埋める運用）。"""
        run()
        run("--seed", "2")

        for team in (self.team, self.rival):
            registered, developmental = split_by_contract(team)
            with self.subTest(team=team.name):
                self.assertTrue(OPENING_REGISTERED_BAND.low <= len(registered) <= OPENING_REGISTERED_BAND.high)
                self.assertTrue(DEVELOPMENT_BAND.low <= len(developmental) <= DEVELOPMENT_BAND.high)

    def test_profile_is_consistent_with_birth_date_and_stint(self):
        run()

        today = date.today()
        for stint in active_stints(self.team):
            player = stint.player
            if player == self.original:
                continue
            born = player.birth_date
            age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
            with self.subTest(player=player.name):
                self.assertTrue(19 <= age <= 40)
                # 高卒の18歳〜助っ人の30歳で入団している。日本人の在籍の開始はドラフトの翌年、助っ人はドラフトなし
                self.assertTrue(18 <= stint.from_year - born.year <= 30)
                if player.is_foreign_player:
                    self.assertIsNone(player.draft_year)
                else:
                    self.assertEqual(stint.from_year, player.draft_year + 1)
                self.assertTrue(player.back_name)
                self.assertTrue(player.name_kana)

    def test_draft_rounds_are_unique_per_team_year_and_kind(self):
        """順位は球団 × ドラフト年 × 本指名/育成ごとに1から振り、流し直しても重ならない（#108）。

        育成ドラフトかどうかは在籍の経路と食い違わない（`ensure_draft_fits_career`）。
        """
        run()
        run("--seed", "2")

        for team in (self.team, self.rival):
            stints = [s for s in active_stints(team) if s.player != self.original]
            picks = Counter(
                (s.player.draft_year, s.player.is_development_draft, s.player.draft_round)
                for s in stints
                if not s.player.is_foreign_player
            )
            with self.subTest(team=team.name):
                self.assertTrue(picks)
                self.assertEqual(max(picks.values()), 1, "同じ年・区分・順位の選手が2人いる")
                for s in stints:
                    player = s.player
                    if player.is_foreign_player:
                        self.assertEqual((player.draft_round, player.is_development_draft), (None, False))
                        continue
                    self.assertGreaterEqual(player.draft_round, 1)
                    self.assertEqual(
                        player.is_development_draft, s.acquired_via == AcquisitionRoute.DEVELOPMENTAL_DRAFT.value
                    )
            for player in DjangoTeamRepository(real_scope()).find_by_id(team.id).players:
                ensure_draft_fits_career(player.profile.draft, player.career)

    def test_rounds_continue_after_the_existing_ones(self):
        """流し直したときは、同じ球団 × 年 × 区分で既にある最大の順位の次から振る。

        数えるのはこの球団に在籍したことのある選手（他球団の選手は数えない）。
        """
        add_player(self.team, "既存一位", number=50, draft_year=2019, draft_round=5)
        add_player(self.team, "既存育成", number=51, draft_year=2019, draft_round=2, is_development_draft=True)
        add_player(self.rival, "他球団", number=50, draft_year=2019, draft_round=9)

        command = seed_virtual_players.Command()
        command._scope = real_scope()  # `handle` が `--workspace` から決める範囲
        last = command._next_rounds(self.team)

        self.assertEqual(last, {(2019, False): 5, (2019, True): 2})

    def test_foreign_players_carry_nationality_and_no_japanese_amateur_career(self):
        with mock.patch.object(generator, "FOREIGN_PLAYER_RATIO", 0.5):
            run()

        new_players = [s.player for s in active_stints(self.team) if s.player != self.original]
        self.assertTrue(any(p.is_foreign_player for p in new_players))
        self.assertTrue(any(not p.is_foreign_player for p in new_players))
        for player in new_players:
            with self.subTest(player=player.name):
                if player.is_foreign_player:
                    self.assertEqual(player.nationality, player.birthplace)
                    self.assertEqual((player.high_school, player.university, player.corporate_team), ("", "", ""))
                else:
                    self.assertEqual(player.nationality, "")
                    self.assertTrue(player.high_school)

    def test_developmental_players_have_no_corporate_career(self):
        """社会人の選手は育成で指名できない（#307）。育成ドラフトの選手の経歴は社会人にしない。"""
        run()

        for team in (self.team, self.rival):
            _, developmental = split_by_contract(team)
            with self.subTest(team=team.name):
                self.assertTrue(developmental)
                for stint in developmental:
                    self.assertTrue(stint.player.is_development_draft)
                    self.assertEqual(stint.player.corporate_team, "")
        everyone = [s.player for team in (self.team, self.rival) for s in active_stints(team)]
        self.assertTrue(any(p.corporate_team for p in everyone), "前提: 支配下には社会人の経歴がいる")

    def test_back_names_distinguish_same_surname_within_team(self):
        run()

        for team in (self.team, self.rival):
            by_surname = defaultdict(list)
            for stint in active_stints(team):
                if stint.player.back_name:
                    by_surname[stint.player.back_name.split(".")[-1]].append(stint.player.back_name)
            for surname, back_names in by_surname.items():
                if len(back_names) > 1:
                    with self.subTest(team=team.name, surname=surname):
                        self.assertTrue(all("." in name for name in back_names))

    def test_dry_run_writes_nothing(self):
        output = run("--dry-run")

        self.assertIn("[dry-run]", output)
        self.assertEqual(orm_models.Player.objects.count(), 1)
        self.assertEqual(orm_models.PlayerStint.objects.count(), 1)


class SeedForeignPlayerQuotaTest(BaseCase):
    """1球団の外国人は、生成の上限（`generator.FOREIGN_PLAYERS_PER_TEAM_MAX`）を超えて投入しない。

    以前は枠を見ずに12%の確率で外国人にしていたため、48チーム中11チームが
    枠（5人）を超えていた。協約（第82条の2）は保有を縛らないので、上限はリーグの外国人枠ではなく生成の目安。
    """

    def test_does_not_exceed_the_generation_cap(self):
        add_player(self.team, "マイケル・ジョンソン", number=1, is_foreign=True)

        # 全員を外国人として抽選させ、上限だけが歯止めになる状況を作る
        with (
            mock.patch.object(generator, "FOREIGN_PLAYER_RATIO", 1.0),
            mock.patch.object(generator, "FOREIGN_PLAYERS_PER_TEAM_MAX", 2),
        ):
            run()

        for team in (self.team, self.rival):
            with self.subTest(team=team.name):
                foreign = [s for s in active_stints(team) if s.player.is_foreign_player]
                self.assertEqual(len(foreign), 2)

    def test_the_league_foreign_rules_do_not_cap_holdings(self):
        """リーグの外国人枠は1軍の登録にかかるので、0 にしても投入の人数は変わらない。"""
        orm_models.League.objects.filter(id=self.league.id).update(
            foreign_active_limit=0, foreign_active_pitcher_limit=0, foreign_active_batter_limit=0
        )

        with mock.patch.object(generator, "FOREIGN_PLAYER_RATIO", 1.0):
            run()

        foreign = [s for s in active_stints(self.team) if s.player.is_foreign_player]
        self.assertEqual(len(foreign), generator.FOREIGN_PLAYERS_PER_TEAM_MAX)

    def test_developmental_foreigners_count_toward_the_cap(self):
        """生成の上限は育成も数える（保有の人数の目安）。"""
        player = orm_models.Player.objects.create(name="育成の助っ人", is_foreign_player=True, nationality="キューバ")
        orm_models.PlayerStint.objects.create(
            player=player, team=self.team, number="150", from_year=2024, signed_as=ContractStatus.DEVELOPMENTAL.value
        )

        with (
            mock.patch.object(generator, "FOREIGN_PLAYER_RATIO", 1.0),
            mock.patch.object(generator, "FOREIGN_PLAYERS_PER_TEAM_MAX", 2),
        ):
            run()

        foreign = [s for s in active_stints(self.team) if s.player.is_foreign_player]
        self.assertEqual(len(foreign), 2)


class SeedAlreadyOverTheLimitTest(BaseCase):
    """投入前から支配下の上限を超えている球団は、減らさず（追加専用）、支配下は足さずに理由を出す。"""

    def test_reports_and_adds_nothing_to_an_over_limit_section(self):
        self.league.registered_player_limit = 2
        self.league.save()
        for number, is_foreign in ((1, True), (2, True), (3, False)):
            add_player(self.team, f"既存{number}", number=number, is_foreign=is_foreign)

        output = run()

        self.assertIn(f"{self.team.name}: 支配下が既に上限（2人）を超えています（3人）", output)
        registered, developmental = split_by_contract(self.team)
        self.assertEqual(len(registered), 3)
        self.assertTrue(developmental, "育成は上限の外なので足す")
        # 超えていない球団は黙って埋まる
        self.assertNotIn(f"{self.rival.name}: 支配下が既に上限", output)


class SeedReproducibleTest(BaseCase):
    def test_the_same_seed_gives_the_same_players(self):
        def snapshot():
            return sorted(
                (
                    s.team_id,
                    s.player.name,
                    s.number,
                    s.signed_as,
                    s.acquired_via,
                    s.player.position,
                    s.player.birth_date,
                    s.player.is_foreign_player,
                    s.player.back_name,
                )
                for team in (self.team, self.rival)
                for s in active_stints(team)
            )

        run()
        first = snapshot()
        orm_models.Player.objects.all().delete()

        run()

        self.assertEqual(snapshot(), first)


class RenameExistingTest(BaseCase):
    """--rename-existing: 架空の日本人選手の氏名だけを選び直す。"""

    def test_renames_only_virtual_japanese_players(self):
        original = add_player(self.team, "藤井健吾", number=10)
        foreign = add_player(self.team, "マイケル・ジョンソン", number=11, is_foreign=True)
        virtual = add_player(self.team, "佐藤翔太", number=12, name_kana="サトウショウタ")

        output = run("--rename-existing")

        self.assertIn("選手 1人の氏名を選び直しました", output)
        for player, expected in ((original, "藤井健吾"), (foreign, "マイケル・ジョンソン")):
            player.refresh_from_db()
            self.assertEqual(player.name, expected)
        virtual.refresh_from_db()
        self.assertTrue(virtual.name_kana)
        # 新規投入はしない
        self.assertEqual(orm_models.Player.objects.count(), 3)

    def test_dry_run_keeps_names(self):
        virtual = add_player(self.team, "佐藤翔太", number=12)

        run("--rename-existing", "--dry-run")

        virtual.refresh_from_db()
        self.assertEqual(virtual.name, "佐藤翔太")


class AssignReadingsTest(BaseCase):
    """--assign-readings: 氏名から読みを逆引きし、背ネームを付ける。"""

    def test_assigns_readings_and_back_names(self):
        original = add_player(self.team, "藤井健吾", number=10)
        sato_a = add_player(self.team, "佐藤翔太", number=11)
        sato_b = add_player(self.team, "佐藤大輔", number=12)
        rare = add_player(self.team, "小鳥遊一心", number=13)
        foreign = add_player(self.team, "マイケル・ジョンソン", number=14, is_foreign=True)
        # 別チームの同姓は区別しなくてよい
        other_sato = add_player(self.rival, "佐藤健太", number=11)

        run("--assign-readings")

        expected = {
            original: (ORIGINAL_PLAYER_READINGS["藤井健吾"][0], "FUJII"),
            sato_a: ("サトウショウタ", "S.SATO"),
            sato_b: ("サトウダイスケ", "D.SATO"),
            rare: ("タカナシイッシン", "TAKANASHI"),
            foreign: ("マイケル・ジョンソン", "JOHNSON"),
            other_sato: ("サトウケンタ", "SATO"),
        }
        for player, (kana, back_name) in expected.items():
            player.refresh_from_db()
            with self.subTest(player=player.name):
                self.assertEqual((player.name_kana, player.back_name), (kana, back_name))

    def test_reports_names_it_cannot_read(self):
        unknown = add_player(self.team, "無名太郎", number=10)

        output = run("--assign-readings")

        self.assertIn("読みを特定できず未対応のまま: 1人", output)
        unknown.refresh_from_db()
        self.assertEqual(unknown.back_name, "")


class RefreshSchoolsTest(BaseCase):
    """--refresh-schools: 架空の日本人選手の出身校だけを選び直す。"""

    def test_refreshes_only_virtual_japanese_players(self):
        original = add_player(self.team, "藤井健吾", number=10, high_school="実在高校")
        foreign = add_player(self.team, "マイケル・ジョンソン", number=11, is_foreign=True)
        virtual = add_player(self.team, "佐藤翔太", number=12, birthplace="北海道", draft_year=2019, draft_round=3)

        output = run("--refresh-schools")

        self.assertIn("選手 1人の出身高校", output)
        original.refresh_from_db()
        foreign.refresh_from_db()
        virtual.refresh_from_db()
        self.assertEqual(original.high_school, "実在高校")
        self.assertEqual(foreign.high_school, "")
        self.assertTrue(virtual.high_school)
        # ドラフトなど他の項目は変えない
        self.assertEqual((virtual.draft_year, virtual.draft_round), (2019, 3))

    def test_development_draft_players_do_not_get_a_corporate_career(self):
        """社会人の選手は育成で指名できない（#307）。経路の重みを社会人に寄せても、育成ドラフトの選手は社会人にならない。"""
        development = add_player(
            self.team,
            "佐藤翔太",
            number=13,
            birthplace="北海道",
            draft_year=2019,
            draft_round=1,
            is_development_draft=True,
        )
        registered = add_player(self.team, "鈴木大輔", number=12, birthplace="北海道", draft_year=2019, draft_round=1)
        weights = {generator.AmateurPath.HIGH_SCHOOL: 0, generator.AmateurPath.UNIVERSITY: 1}
        with mock.patch.dict(generator._AMATEUR_PATH_WEIGHTS, {**weights, generator.AmateurPath.CORPORATE: 1000}):
            run("--refresh-schools")

        development.refresh_from_db()
        registered.refresh_from_db()
        self.assertEqual(development.corporate_team, "")
        self.assertTrue(development.university)
        self.assertTrue(registered.corporate_team, "前提: 支配下は社会人を引く")

    def test_public_high_school_is_in_birthplace(self):
        virtual = add_player(self.team, "佐藤翔太", number=12, birthplace="北海道")

        # 私立を引かせない
        with mock.patch.object(generator, "PRIVATE_HIGH_SCHOOL_RATIO", 0.0):
            run("--refresh-schools")

        virtual.refresh_from_db()
        self.assertTrue(virtual.high_school.startswith("北海道"))
        self.assertNotIn("北海道立", virtual.high_school)


class LargestRemainderTest(SimpleTestCase):
    """ポジションの人数配分（最大剰余法）は合計を崩さない。"""

    def test_allocation_sums_to_total(self):
        ratios = generator.POSITION_RATIOS
        for total in range(DEVELOPMENT_BAND.low, OPENING_REGISTERED_BAND.high + 1):
            with self.subTest(total=total):
                allocation = generator.largest_remainder(total, ratios)
                self.assertEqual(sum(allocation.values()), total)
                self.assertEqual(Counter(allocation.keys()), Counter(ratios.keys()))
