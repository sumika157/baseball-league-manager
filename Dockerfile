# 開発用イメージ（Python は WSL 上の venv と同じ 3.10 系に合わせています）
FROM python:3.10-slim

# .pyc を作らない / ログを即座に出力する
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 依存関係を先にインストールしてレイヤーキャッシュを効かせる
COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-dev.txt

# Playwright（E2Eテスト用ブラウザ）は別レイヤーにして、
# Pythonパッケージだけ変わった時に再ダウンロードされないようにする
RUN playwright install --with-deps chromium

# アプリ本体をコピー（実際の開発時は compose のボリュームマウントで上書きされます）
COPY . .

EXPOSE 8000

# コンテナ外からアクセスするため 0.0.0.0 で待ち受ける
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
