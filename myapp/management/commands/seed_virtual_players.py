"""仮想の選手データを投入する。

各球団の支配下はペナントの開幕前の帯（`OPENING_REGISTERED_BAND`。62〜66人。リーグの支配下上限が
それより低ければ上限まで）の中で、育成は `generator.DEVELOPMENTAL_RANGE`（10〜30人）の中で目標を引き、
既存の選手・在籍は残したまま不足分を架空の選手で埋める（追加専用）。ポジション構成は
`generator.POSITION_RATIOS` を支配下・育成それぞれに最大剰余法で配る。

一部（約12%）は「助っ人」として nationality / is_foreign_player を設定する
（既存データは 0024_backfill_foreign_players_from_birthplace で移行済み）。助っ人は支配下だけにする
（1球団の外国人の上限 `generator.FOREIGN_PLAYERS_PER_TEAM_MAX` は育成も数えるので、上限は支配下の助っ人に回す）。

背番号は支配下が 00・0〜99、育成が3桁（100番台から）。日本人選手にはドラフトの指名（`DraftPick`。年は加入年の前年、
支配下は本指名・育成は育成ドラフト）を付け、入団の経路はそこから導く（`DraftPick.acquisition_route`）。助っ人はドラフトを
持たず、経路は新外国人。順位は球団 × ドラフト年 × 本指名/育成ごとに、既にある最大の順位の次から1つずつ振る
（重複しない）。

氏名は「苗字＋名前」を漢字プールから組み合わせて作るため、よみがな（カタカナ）と
背ネーム（ユニフォーム背面のヘボン式アルファベット表記）もプール側に持たせてある。
背ネームは同じチーム内に同姓の選手がいる場合、ファーストネームの頭文字＋ピリオドを
先頭に付けて区別する（例: 「K.SATO」）。
"""

import random
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Max

from myapp.domain.exceptions import DomainError, RegisteredPlayerLimitExceeded
from myapp.domain.pennant.roster import OPENING_REGISTERED_BAND
from myapp.domain.pennant.world import WorldScope
from myapp.domain.value_objects import (
    DEVELOPMENTAL_MIN_NUMBER,
    AcquisitionRoute,
    ContractStatus,
    DraftPick,
    JerseyNumber,
    Position,
    ensure_quota_not_exceeded,
)
from myapp.domain.virtual_players import generator, pools
from myapp.infrastructure.repositories import draft_columns
from myapp.infrastructure.scoping import players_in, stints_in, teams_in
from myapp.management.workspaces import legacy_real_scope
from myapp.models import Player, PlayerStint

# 支配下の背番号（00・0〜99）
REGISTERED_NUMBERS = ["00", "0", *map(str, range(1, DEVELOPMENTAL_MIN_NUMBER))]
# 育成の背番号（3桁）。100番台を先に配り、尽きたら200番台以降を使う
DEVELOPMENTAL_FIRST_NUMBERS = [str(n) for n in range(DEVELOPMENTAL_MIN_NUMBER, 200)]
DEVELOPMENTAL_SPARE_NUMBERS = [str(n) for n in range(200, 1000)]

# このコマンドの初回投入より前から存在した選手。--rename-existing で名前を
# 選び直す対象から除外し、--assign-readings では固定のよみがな・ローマ字を使う
# （架空データではなく実際に登録された選手であり、生成プールに読みを持たないため）。
ORIGINAL_PLAYER_READINGS = {
    "藤井健吾": ("フジイケンゴ", "FUJII", "KENGO"),
    "中島亮太": ("ナカジマリョウタ", "NAKAJIMA", "RYOTA"),
    "渡辺春希": ("ワタナベハルキ", "WATANABE", "HARUKI"),
    "大田光誠": ("オオタコウセイ", "OTA", "KOSEI"),
    "山中俊": ("ヤマナカシュン", "YAMANAKA", "SHUN"),
    "坂上康太": ("サカウエコウタ", "SAKAUE", "KOTA"),
    "黒田裕貴": ("クロダユウキ", "KURODA", "YUKI"),
    "高橋誠司": ("タカハシセイジ", "TAKAHASHI", "SEIJI"),
    "大岩蓮": ("オオイワレン", "OIWA", "REN"),
    "遠藤慶介": ("エンドウケイスケ", "ENDO", "KEISUKE"),
    "佐藤涼介": ("サトウリョウスケ", "SATO", "RYOSUKE"),
}
ORIGINAL_PLAYER_NAMES = list(ORIGINAL_PLAYER_READINGS)


@dataclass
class _TeamTally:
    """1球団の投入の集計（報告用）。"""

    name: str
    existing: Counter[ContractStatus] = field(default_factory=Counter)
    added: Counter[ContractStatus] = field(default_factory=Counter)
    # 足した助っ人の人数
    foreign: int = 0


class Command(BaseCommand):
    help = "各チームに仮想の選手データを投入する（既存の選手・在籍は残す）"

    def add_arguments(self, parser):
        parser.add_argument("--seed", type=int, default=None, help="乱数シード（再現用）")
        parser.add_argument("--dry-run", action="store_true", help="投入せず件数だけ表示する")
        parser.add_argument(
            "--rename-existing",
            action="store_true",
            help=(
                "このコマンドで過去に投入した架空の日本人選手（助っ人を除く）の"
                "氏名を、珍しい苗字・名前を混ぜたプールで選び直す。新規投入はしない。"
            ),
        )
        parser.add_argument(
            "--assign-readings",
            action="store_true",
            help=("全選手（実在・架空とも）によみがなと背ネームを付与する。氏名や新規投入はしない。"),
        )
        parser.add_argument(
            "--refresh-schools",
            action="store_true",
            help=(
                "このコマンドで過去に投入した架空の日本人選手（助っ人を除く）の"
                "出身高校・大学・社会人チームを選び直す。ドラフトなど他の項目は変えない。"
            ),
        )

    def _real_scope(self) -> WorldScope:
        """投入・補正の対象（実データだけ。ペナントの世界の球団・選手には触れない）。

        暫定（W1b T2）: 今のワークスペース（id が最小のもの）。1回の実行で1回だけ決めて覚える。
        T5 で `--workspace` を受け取る形に替える。
        """
        scope: WorldScope | None = getattr(self, "_scope", None)
        if scope is None:
            scope = self._scope = legacy_real_scope()
        return scope

    def handle(self, *args, **options):
        # 乱数源はこのコマンドの中だけで持つ（生成の関数は rng を引数に取る）
        rng = random.Random(options["seed"])

        if options["rename_existing"]:
            self._rename_existing(rng, dry_run=options["dry_run"])
            return

        if options["assign_readings"]:
            self._assign_readings(dry_run=options["dry_run"])
            return

        if options["refresh_schools"]:
            self._refresh_schools(rng, dry_run=options["dry_run"])
            return

        self._fill_rosters(rng, dry_run=options["dry_run"])

    def _fill_rosters(self, rng, *, dry_run):
        today = date.today()
        used_names = set(players_in(self._real_scope()).values_list("name", flat=True))

        with transaction.atomic():
            tallies = [
                self._fill_team(rng, team, used_names=used_names, today=today, dry_run=dry_run)
                for team in teams_in(self._real_scope()).select_related("league")
            ]
            if dry_run:
                transaction.set_rollback(True)

        registered, developmental = ContractStatus.REGISTERED, ContractStatus.DEVELOPMENTAL
        for tally in tallies:
            self.stdout.write(
                f"{tally.name}: 支配下 既存{tally.existing[registered]}人 + 新規{tally.added[registered]}人"
                f" = {tally.existing[registered] + tally.added[registered]}人 / "
                f"育成 既存{tally.existing[developmental]}人 + 新規{tally.added[developmental]}人"
                f" = {tally.existing[developmental] + tally.added[developmental]}人"
            )

        added_registered = sum(t.added[registered] for t in tallies)
        added_developmental = sum(t.added[developmental] for t in tallies)
        total = added_registered + added_developmental
        prefix = "[dry-run] " if dry_run else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}選手 {total}人 / 在籍 {total}件 を作成しました"
                f"（支配下 {added_registered}人・育成 {added_developmental}人。"
                f"うち海外出身 {sum(t.foreign for t in tallies)}人）"
            )
        )

    def _fill_team(self, rng, team, *, used_names, today, dry_run):
        """1球団の支配下・育成を目標まで埋める。既存と足した人数の集計を返す。"""
        active_stints = list(PlayerStint.objects.filter(team=team, to_year__isnull=True).select_related("player"))
        tally = _TeamTally(team.name)
        used_numbers = {s.number for s in active_stints}
        # 外国人の人数（育成も数える）。協約は保有の人数を縛らないので、上限は生成の目安
        # （`generator.FOREIGN_PLAYERS_PER_TEAM_MAX`。リーグの外国人枠は1軍の登録にかかる）
        foreign_on_team = sum(1 for s in active_stints if s.player.is_foreign_player)
        existing_by_position: dict[ContractStatus, Counter[Position]] = defaultdict(Counter)
        for s in active_stints:
            contract = ContractStatus.now(ContractStatus.from_label(s.signed_as), s.promoted_year)
            tally.existing[contract] += 1
            existing_by_position[contract][Position.from_label(s.player.position)] += 1

        # 既に上限を超えている球団は、このコマンドでは直さない（追加専用で、減らすのは別の責務）。
        # 黙って素通りせず、足さない理由を出す（支配下の目標は上限までなので、足すことは無い）
        registered_limit = team.league.registered_player_limit
        registered_before = tally.existing[ContractStatus.REGISTERED]
        already_over = registered_limit is not None and registered_before > registered_limit
        if already_over:
            self.stdout.write(
                self.style.WARNING(
                    f"{team.name}: 支配下が既に上限（{registered_limit}人）を超えています（{registered_before}人）。"
                    "支配下の選手は足しません（このコマンドは追加専用で、減らしません）。"
                )
            )

        targets = {
            ContractStatus.REGISTERED: self._registered_target(rng, registered_limit),
            ContractStatus.DEVELOPMENTAL: rng.randint(*generator.DEVELOPMENTAL_RANGE),
        }
        numbers = {
            ContractStatus.REGISTERED: self._shuffled(rng, REGISTERED_NUMBERS, used_numbers),
            # pop() は末尾から取るので、先に配る100番台を後ろに置く
            ContractStatus.DEVELOPMENTAL: self._shuffled(rng, DEVELOPMENTAL_SPARE_NUMBERS, used_numbers)
            + self._shuffled(rng, DEVELOPMENTAL_FIRST_NUMBERS, used_numbers),
        }

        # ドラフトの順位の次の番号（球団 × ドラフト年 × 本指名/育成）
        next_rounds = self._next_rounds(team)
        new_players = []
        for contract, target in targets.items():
            additions = generator.additions_by_position(target, existing_by_position[contract])
            for position in [p for p, count in additions.items() for _ in range(count)]:
                if not numbers[contract]:
                    # 支配下の番号（101個）は帯の上端より多いので、尽きるのは帯か既存のデータが崩れているとき。
                    # 3桁を支配下に配ると契約区分の規則（`ensure_number_fits`）を破るので、止める
                    raise CommandError(
                        f"{team.name}: {contract.label}の背番号が足りません（使用中 {len(used_numbers)}個）。"
                    )
                number = numbers[contract].pop()
                used_numbers.add(number)
                # 助っ人は支配下だけ。生成の上限に達していれば日本人選手にする
                is_foreign = (
                    contract is ContractStatus.REGISTERED
                    and rng.random() < generator.FOREIGN_PLAYER_RATIO
                    and foreign_on_team < generator.FOREIGN_PLAYERS_PER_TEAM_MAX
                )
                if is_foreign:
                    foreign_on_team += 1
                    tally.foreign += 1
                player, stint, surname_romaji, given_romaji = self._new_player(
                    rng,
                    position=position,
                    contract=contract,
                    is_foreign=is_foreign,
                    number=number,
                    used_names=used_names,
                    today=today,
                    next_rounds=next_rounds,
                )
                tally.added[contract] += 1
                if dry_run:
                    continue
                player.save()
                stint.player = player
                stint.team = team
                stint.save()
                new_players.append((player, surname_romaji, given_romaji))

        # ORM に直接書いたので、集約と同じ規則（`ensure_quota_not_exceeded`）で投入後の支配下の人数を確かめる。
        # 投入前から超えていた球団は上で理由を出したので、ここでは見ない
        registered_after = registered_before + tally.added[ContractStatus.REGISTERED]
        if not already_over:
            try:
                ensure_quota_not_exceeded(
                    registered_after,
                    registered_limit,
                    f"{team.name}: 投入後の支配下選手（{registered_after}人）が"
                    f"上限（{registered_limit}人）を超えます。",
                    RegisteredPlayerLimitExceeded,
                )
            except DomainError as exc:
                raise CommandError(str(exc)) from exc

        if not dry_run:
            self._apply_back_names(team, new_players)
        return tally

    @staticmethod
    def _registered_target(rng, limit):
        """支配下の目標人数。帯の中から引く。リーグの支配下上限が上端より低ければ上限が天井になる。"""
        band = OPENING_REGISTERED_BAND
        ceiling = band.high if limit is None else min(band.high, limit)
        return rng.randint(min(band.low, ceiling), ceiling)

    @staticmethod
    def _shuffled(rng, numbers, used_numbers):
        available = [n for n in numbers if n not in used_numbers]
        rng.shuffle(available)
        return available

    @staticmethod
    def _next_rounds(team) -> Counter[tuple[int, bool]]:
        """球団の既存の選手の、ドラフト年 × 本指名/育成ごとの最大の順位（次に振る順位はこれ＋1）。

        指名した球団は最初の在籍で分かるが、ここではこの球団に在籍したことのある選手をすべて数える
        （移籍してきた選手の順位まで避けるので、番号が飛ぶことはあっても重複はしない）。
        """
        rows = (
            players_in(WorldScope.real(team.league.workspace_id))
            .filter(stints__team=team, draft_year__isnull=False, draft_round__isnull=False)
            .values("draft_year", "is_development_draft")
            .annotate(last=Max("draft_round"))
            .order_by()
        )
        last_rounds: Counter[tuple[int, bool]] = Counter()
        for row in rows:
            if row["draft_year"] is not None:  # 絞り込み済み（型のため）
                last_rounds[(row["draft_year"], row["is_development_draft"])] = row["last"]
        return last_rounds

    @staticmethod
    def _new_player(rng, *, position, contract, is_foreign, number, used_names, today, next_rounds):
        """未保存の選手と在籍を作る。(選手, 在籍, 苗字のローマ字, 名前のローマ字)。"""
        if is_foreign:
            name, name_kana, country, surname_romaji, given_romaji = generator.foreign_name(rng, used_names)
            birthplace = country
        else:
            name, name_kana, surname_romaji, given_romaji = generator.japanese_name(rng, used_names)
            birthplace = generator.prefecture(rng)

        mean, sd, youngest, oldest = (
            generator.DEVELOPMENTAL_AGE if contract is ContractStatus.DEVELOPMENTAL else generator.REGISTERED_AGE
        )
        age = max(youngest, min(oldest, round(rng.gauss(mean, sd))))
        birth_date = generator.birth_date_for(rng, age=age, as_of=today)
        career = generator.amateur_career(rng, is_foreign=is_foreign, birthplace=birthplace)
        # プロ入りの年（加入年）。経歴のプロ入りの年齢で、今の年齢を超えない
        joined_year = birth_date.year + min(career.debut_age, age)
        draft = None
        route = AcquisitionRoute.NEW_FOREIGN
        if not is_foreign:
            # 日本人はドラフトを経て入る。プロ入りの前年の秋に指名され、加入年はその翌年（`DraftPick.entry_year`）。
            # 順位は球団 × 年 × 本指名/育成ごとに次の番号
            is_development = contract is ContractStatus.DEVELOPMENTAL
            draft_year = joined_year - 1
            next_rounds[(draft_year, is_development)] += 1
            draft = DraftPick(
                year=draft_year, round=next_rounds[(draft_year, is_development)], is_development=is_development
            )
            joined_year = draft.entry_year
            route = draft.acquisition_route
        # ORM に直接書くので、集約が守る区分の規則（背番号の桁・経路と区分の対応）をここで確かめる
        contract.ensure_number_fits(JerseyNumber(number))
        route.ensure_fits_contract(contract)
        throws, bats = generator.handedness(rng, position)
        height_cm, weight_kg = generator.physique(rng, position)

        player = Player(
            name=name,
            name_kana=name_kana,
            position=position.value,
            birth_date=birth_date,
            throws=throws.value,
            bats=bats.value,
            height_cm=height_cm,
            weight_kg=weight_kg,
            birthplace=birthplace,
            **draft_columns(draft),
            high_school=career.high_school,
            university=career.university,
            corporate_team=career.corporate_team,
            nationality=birthplace if is_foreign else "",
            is_foreign_player=is_foreign,
        )
        stint = PlayerStint(
            number=number,
            from_year=joined_year,
            to_year=None,
            signed_as=contract.value,
            acquired_via=route.value,
        )
        return player, stint, surname_romaji, given_romaji

    def _apply_back_names(self, team, new_players):
        """新規追加分を、既存の在籍者も含めたチーム内の同姓関係で背ネーム付けする。"""
        if not new_players:
            return

        existing_players = [
            s.player
            for s in PlayerStint.objects.filter(team=team, to_year__isnull=True)
            .select_related("player")
            .exclude(player_id__in=[p.id for p, _, _ in new_players])
        ]

        # 既存選手の苗字ローマ字は、保存済みの背ネームから復元する
        # （末尾側が苗字。ピリオドが付いていれば取り除く）。
        surname_romaji_by_id = {}
        given_initial_by_id = {}
        for p in existing_players:
            base = p.back_name.split(".")[-1] if p.back_name else ""
            surname_romaji_by_id[p.id] = base
            given_initial_by_id[p.id] = p.back_name[0] if "." in (p.back_name or "") else ""
        for player, surname_romaji, given_romaji in new_players:
            surname_romaji_by_id[player.id] = surname_romaji
            given_initial_by_id[player.id] = given_romaji[0] if given_romaji else ""

        counts = Counter(surname_romaji_by_id.values())

        to_update = []
        for player, _, _given_romaji in new_players:
            surname_romaji = surname_romaji_by_id[player.id]
            if counts[surname_romaji] > 1:
                player.back_name = f"{given_initial_by_id[player.id]}.{surname_romaji}"
            else:
                player.back_name = surname_romaji
            to_update.append(player)
        Player.objects.bulk_update(to_update, ["back_name"])

        # 同姓が新たに発生した既存選手側の背ネームも、頭文字付きに揃え直す
        stale = [
            p for p in existing_players if counts[surname_romaji_by_id[p.id]] > 1 and "." not in (p.back_name or "")
        ]
        for p in stale:
            p.back_name = f"{given_initial_by_id[p.id]}.{surname_romaji_by_id[p.id]}"
        if stale:
            Player.objects.bulk_update(stale, ["back_name"])

    def _refresh_schools(self, rng, *, dry_run):
        targets = list(
            players_in(self._real_scope()).filter(is_foreign_player=False).exclude(name__in=ORIGINAL_PLAYER_NAMES)
        )
        to_update = []
        for player in targets:
            career = generator.amateur_career(rng, is_foreign=False, birthplace=player.birthplace)
            player.high_school = career.high_school
            player.university = career.university
            player.corporate_team = career.corporate_team
            to_update.append(player)

        if not dry_run:
            with transaction.atomic():
                Player.objects.bulk_update(to_update, ["high_school", "university", "corporate_team"], batch_size=500)

        prefix = "[dry-run] " if dry_run else ""
        self.stdout.write(
            self.style.SUCCESS(f"{prefix}選手 {len(to_update)}人の出身高校・大学・社会人チームを選び直しました")
        )

    def _rename_existing(self, rng, *, dry_run):
        targets = (
            players_in(self._real_scope()).filter(is_foreign_player=False).exclude(name__in=ORIGINAL_PLAYER_NAMES)
        )
        used_names = set(players_in(self._real_scope()).values_list("name", flat=True))
        renamed = 0

        with transaction.atomic():
            for player in targets:
                used_names.discard(player.name)
                new_name, name_kana, surname_romaji, given_romaji = generator.japanese_name(rng, used_names)
                if not dry_run:
                    player.name = new_name
                    player.name_kana = name_kana
                    player.save(update_fields=["name", "name_kana"])
                renamed += 1

            if dry_run:
                transaction.set_rollback(True)

        prefix = "[dry-run] " if dry_run else ""
        self.stdout.write(self.style.SUCCESS(f"{prefix}選手 {renamed}人の氏名を選び直しました"))
        if not dry_run:
            self.stdout.write("よみがな・背ネームは --assign-readings で付け直してください。")

    def _assign_readings(self, *, dry_run):
        """既存の氏名からよみがな・苗字ローマ字・名前ローマ字を復元し、背ネームを決める。

        新規に選び直すわけではなく、すでに確定している漢字名に対応する読みを
        プールから逆引きする。プールにない氏名（実在の元選手）は
        ORIGINAL_PLAYER_READINGS を使う。
        """
        surname_lookup = {kanji: (kana, romaji) for kanji, kana, romaji in pools.JP_SURNAMES + pools.RARE_JP_SURNAMES}
        given_lookup = {
            kanji: (kana, romaji) for kanji, kana, romaji in pools.JP_GIVEN_NAMES + pools.RARE_JP_GIVEN_NAMES
        }
        surnames_by_length = sorted(surname_lookup, key=len, reverse=True)

        foreign_given_lookup = {kana: romaji for group in pools.FOREIGN_GROUPS for kana, romaji in group["given"]}
        foreign_surname_lookup = {kana: romaji for group in pools.FOREIGN_GROUPS for kana, romaji in group["surname"]}

        players = list(players_in(self._real_scope()))
        surname_romaji_by_id = {}
        given_romaji_by_id = {}
        name_kana_by_id = {}
        unresolved = []

        for player in players:
            if player.name in ORIGINAL_PLAYER_READINGS:
                kana, surname_romaji, given_romaji = ORIGINAL_PLAYER_READINGS[player.name]
            elif player.is_foreign_player and "・" in player.name:
                given_kana, surname_kana = player.name.split("・", 1)
                given_romaji = foreign_given_lookup.get(given_kana)
                surname_romaji = foreign_surname_lookup.get(surname_kana)
                kana = player.name
                if given_romaji is None or surname_romaji is None:
                    unresolved.append(player.name)
                    continue
            else:
                match = None
                for surname_kanji in surnames_by_length:
                    if player.name.startswith(surname_kanji):
                        given_kanji = player.name[len(surname_kanji) :]
                        if given_kanji in given_lookup:
                            match = (surname_kanji, given_kanji)
                            break
                if match is None:
                    unresolved.append(player.name)
                    continue
                surname_kanji, given_kanji = match
                surname_kana, surname_romaji = surname_lookup[surname_kanji]
                given_kana, given_romaji = given_lookup[given_kanji]
                kana = surname_kana + given_kana

            name_kana_by_id[player.id] = kana
            surname_romaji_by_id[player.id] = surname_romaji
            given_romaji_by_id[player.id] = given_romaji

        # チームごとに同姓を数え、背ネームを決める（在籍していない選手は苗字のみ）。
        team_by_player_id = {
            s.player_id: s.team_id for s in stints_in(self._real_scope()).filter(to_year__isnull=True)
        }
        counts_by_team = defaultdict(Counter)
        for player_id, surname_romaji in surname_romaji_by_id.items():
            team_id = team_by_player_id.get(player_id)
            counts_by_team[team_id][surname_romaji] += 1

        to_update = []
        for player in players:
            if player.id not in name_kana_by_id:
                continue
            team_id = team_by_player_id.get(player.id)
            surname_romaji = surname_romaji_by_id[player.id]
            given_romaji = given_romaji_by_id[player.id]
            if counts_by_team[team_id][surname_romaji] > 1:
                back_name = f"{given_romaji[0]}.{surname_romaji}"
            else:
                back_name = surname_romaji

            player.name_kana = name_kana_by_id[player.id]
            player.back_name = back_name
            to_update.append(player)

        if not dry_run:
            with transaction.atomic():
                Player.objects.bulk_update(to_update, ["name_kana", "back_name"], batch_size=500)

        prefix = "[dry-run] " if dry_run else ""
        self.stdout.write(self.style.SUCCESS(f"{prefix}選手 {len(to_update)}人によみがな・背ネームを付与しました"))
        if unresolved:
            self.stdout.write(
                self.style.WARNING(f"読みを特定できず未対応のまま: {len(unresolved)}人 {unresolved[:10]}")
            )
