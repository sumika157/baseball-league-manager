"""今日の日付をずらしてテストを流す（年をまたいでも通るかの確認用）。

    docker compose exec -e FAKE_YEAR=2027 web python .claude/skills/run-tests/fake_today.py \
        myapp.tests.domain myapp.tests.integration --parallel 4

`date.today()` だけを `FAKE_YEAR` 年の1月5日に差し替えてから `manage.py test` を起動する（既定は今年の翌年）。
Django とアプリが `date` を import する前に差し替えるので、`from datetime import date` で取り込んだ先にも効く。
本物の `date` の値も `isinstance(x, date)` を通るようにしてある（通らないと DB の日付の変換が壊れる）。
"""

import datetime as _dt
import os
import sys

_REAL = _dt.date


class _Meta(type):
    def __instancecheck__(cls, obj):
        return isinstance(obj, _REAL)

    def __subclasscheck__(cls, sub):
        return issubclass(sub, _REAL)


class _FakeDate(_REAL, metaclass=_Meta):
    @classmethod
    def today(cls):
        return _REAL(int(os.environ.get("FAKE_YEAR", _REAL.today().year + 1)), 1, 5)


_dt.date = _FakeDate  # type: ignore[misc]
sys.path.insert(0, os.getcwd())
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

from django.core.management import execute_from_command_line  # noqa: E402  差し替えより後に読み込むため

execute_from_command_line(["manage.py", "test", *sys.argv[1:]])
