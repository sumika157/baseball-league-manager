"""本番構成（環境変数で切り替える設定と、バックアップのコマンド）の検査。

設定は `settings.py` 1本を環境変数で切り替える。本番の値は環境変数を明示したときだけ効き、
既定では開発・テストの動作が変わらないこと、本番の値を与えると `check --deploy` が
警告を出さないことを、別プロセスで `settings.py` を読み直して確かめる
（テストの実行プロセスの設定は変えられないため）。
"""

import os
import sqlite3
import subprocess
import sys
import tempfile
from io import StringIO
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TransactionTestCase

BASE_DIR = Path(settings.BASE_DIR)

# 本番の環境変数（docker-compose.prod.yml と Dockerfile.prod が渡すものと、.env に書くもの）
PRODUCTION_ENV = {
    "DJANGO_SECRET_KEY": "x7Qm2LpVn9sTz4Kd8RwYc1Hb6JfGa3EuNo5XiAtBvCyZlDkMrSgPe0WhUq",
    "DJANGO_DEBUG": "False",
    "DJANGO_ALLOWED_HOSTS": "example.com",
    "DJANGO_CSRF_TRUSTED_ORIGINS": "https://example.com",
    "DJANGO_PRODUCTION_STATIC": "True",
    "DJANGO_SQLITE_WAL": "True",
    "DJANGO_LOG_TO_STDOUT": "True",
    "DJANGO_BEHIND_PROXY": "True",
    "DJANGO_SSL_REDIRECT": "True",
    "DJANGO_SECURE_COOKIES": "True",
    "DJANGO_HSTS_SECONDS": "31536000",
}

# 既定（開発・テスト）に戻すため、親プロセスの環境から外す変数
SWITCHES = {key for key in PRODUCTION_ENV if key not in ("DJANGO_SECRET_KEY", "DJANGO_ALLOWED_HOSTS")}
# E2E テスト（Playwright）が親プロセスに足す変数。本番の検査（async.E001）に引っかかるので外す
SWITCHES.add("DJANGO_ALLOW_ASYNC_UNSAFE")


def run_python(code: str, extra_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """本番用の切り替えを外した環境に `extra_env` を足して、別プロセスで Python を実行する。"""
    env = {key: value for key, value in os.environ.items() if key not in SWITCHES}
    env.setdefault("DJANGO_SECRET_KEY", "test-only")
    env["DJANGO_SETTINGS_MODULE"] = "config.settings"
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-c", code], cwd=BASE_DIR, env=env, capture_output=True, text=True, timeout=120
    )


class ProductionSettingsTest(SimpleTestCase):
    def test_default_environment_keeps_development_behaviour(self):
        """本番の環境変数が無ければ、WhiteNoise・manifest・WAL・Secure クッキーは効かない。"""
        code = (
            "from django.conf import settings as s\n"
            "print(any('whitenoise' in m for m in s.MIDDLEWARE))\n"
            "print('OPTIONS' in s.DATABASES['default'])\n"
            "print(s.STORAGES['staticfiles']['BACKEND'])\n"
            "print(s.SESSION_COOKIE_SECURE, s.CSRF_COOKIE_SECURE, s.SECURE_SSL_REDIRECT, s.SECURE_HSTS_SECONDS)\n"
            "print(s.SECURE_PROXY_SSL_HEADER, s.CSRF_TRUSTED_ORIGINS, bool(s.LOGGING))\n"
        )
        result = run_python(code, {})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(),
            [
                "False",
                "False",
                "django.contrib.staticfiles.storage.StaticFilesStorage",
                "False False False 0",
                "None [] False",
            ],
        )

    def test_production_environment_passes_check_deploy(self):
        """本番の環境変数を与えると、`check --deploy` が警告を1つも出さない。"""
        with tempfile.TemporaryDirectory() as tmp:
            env = {**PRODUCTION_ENV, "DJANGO_DB_PATH": str(Path(tmp) / "db.sqlite3")}
            code = (
                "from django.core.management import execute_from_command_line\n"
                "execute_from_command_line(['manage.py', 'check', '--deploy', '--fail-level', 'WARNING'])"
            )
            result = run_python(code, env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("no issues", result.stdout)

    def test_production_environment_enables_wal_and_whitenoise(self):
        """本番の環境変数で、SQLite が WAL になり、WhiteNoise が SecurityMiddleware の直後に入る。"""
        with tempfile.TemporaryDirectory() as tmp:
            env = {**PRODUCTION_ENV, "DJANGO_DB_PATH": str(Path(tmp) / "db.sqlite3")}
            code = (
                "import django; django.setup()\n"
                "from django.conf import settings as s\n"
                "from django.db import connection\n"
                "c = connection.cursor()\n"
                "print(c.execute('PRAGMA journal_mode').fetchone()[0])\n"
                "print(c.execute('PRAGMA synchronous').fetchone()[0])\n"
                "print(s.MIDDLEWARE[:2])\n"
            )
            result = run_python(code, env)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[0], "wal")
        self.assertEqual(lines[1], "1")  # NORMAL
        self.assertIn("whitenoise.middleware.WhiteNoiseMiddleware", lines[2])


class BackupDbCommandTest(TransactionTestCase):
    """TestCase だと全体が未コミットのトランザクションの中になり、バックアップ API が書き込みロックを待ち続ける。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.destination = Path(self.tmp.name) / "backups"

    def _backup(self, *args: str) -> str:
        out = StringIO()
        call_command("backup_db", str(self.destination), *args, stdout=out)
        return out.getvalue()

    def test_creates_a_readable_copy(self):
        """写しは別の接続で開いて読み戻せ、その時点のデータを持つ。"""
        User.objects.create_user("backup-check", password="x")
        self._backup()

        copies = list(self.destination.glob("db-*.sqlite3"))
        self.assertEqual(len(copies), 1)
        self.assertEqual(list(self.destination.glob("*.partial")), [])
        with sqlite3.connect(copies[0]) as copy:
            names = [row[0] for row in copy.execute("SELECT username FROM auth_user")]
        copy.close()
        self.assertEqual(names, ["backup-check"])

    def test_keep_removes_oldest_generations(self):
        """--keep で、新しい順に残す世代数だけ残して古い写しを消す。"""
        self.destination.mkdir()
        for stamp in ("20200101-000000", "20200102-000000", "20200103-000000"):
            (self.destination / f"db-{stamp}.sqlite3").write_bytes(b"old")

        self._backup("--keep", "2")

        names = sorted(p.name for p in self.destination.glob("db-*.sqlite3"))
        self.assertEqual(len(names), 2)
        self.assertNotIn("db-20200101-000000.sqlite3", names)
        self.assertNotIn("db-20200102-000000.sqlite3", names)

    def test_negative_keep_is_rejected(self):
        with self.assertRaises(CommandError):
            self._backup("--keep", "-1")
