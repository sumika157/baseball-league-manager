"""SQLite のデータベースを、動かしたまま一貫した1ファイルに写す。

Python の `sqlite3` のバックアップ API を使うので、WAL モードで書き込みがあっても、
コミット済みの状態を一貫した形で取れる（`cp db.sqlite3` は `-wal` に残った分を取りこぼし、
書き込み中だと壊れた写しになる）。

出力先はディレクトリで、`db-YYYYmmdd-HHMMSS.sqlite3` の名前で書く。`--keep N` を付けると、
新しい順に N 世代だけ残して古い写しを消す（cron から毎日呼ぶ想定）。
写しは一時ファイルに書いて整合性を確かめてから名前を付けるので、途中で失敗しても
壊れた写しは残らない。
"""

import sqlite3
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

PREFIX = "db-"
SUFFIX = ".sqlite3"


class Command(BaseCommand):
    help = "データベースの写しを出力先のディレクトリに作る（実行中でも取れる）"

    def add_arguments(self, parser):
        parser.add_argument("destination", help="写しを置くディレクトリ（無ければ作る）")
        parser.add_argument(
            "--keep",
            type=int,
            default=0,
            help="残す世代数。新しい順に数えて、超えた古い写しを消す（0 は消さない）",
        )

    def handle(self, *args, **options):
        keep: int = options["keep"]
        if keep < 0:
            raise CommandError("--keep には 0 以上を指定してください。")
        if settings.DATABASES["default"]["ENGINE"] != "django.db.backends.sqlite3":
            raise CommandError("backup_db は SQLite 専用です。")

        destination = Path(options["destination"])
        destination.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        final = destination / f"{PREFIX}{stamp}{SUFFIX}"
        partial = final.with_suffix(final.suffix + ".partial")
        try:
            self._copy(partial)
            partial.rename(final)
        finally:
            partial.unlink(missing_ok=True)
        self.stdout.write(f"バックアップを作りました: {final}")

        if keep:
            for old in self._generations(destination)[keep:]:
                old.unlink()
                self.stdout.write(f"古い世代を削除しました: {old}")

    def _copy(self, target: Path) -> None:
        """接続中のデータベースを `target` へ写し、読み戻せることを確かめる。"""
        connection.ensure_connection()
        source = connection.connection
        copy = sqlite3.connect(target)
        try:
            source.backup(copy)
            result = copy.execute("PRAGMA integrity_check").fetchone()
        finally:
            copy.close()
        if result is None or result[0] != "ok":
            raise CommandError(f"写しの整合性検査に失敗しました: {result}")

    @staticmethod
    def _generations(directory: Path) -> list[Path]:
        """この名前の写しを新しい順に返す（名前の日時の降順。`.partial` は含めない）。"""
        return sorted(directory.glob(f"{PREFIX}*{SUFFIX}"), reverse=True)
