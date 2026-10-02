// 試合編集画面のルートコンポーネント。
// 保存は payload.urls.save への POST のみ（GET 用 API は無く、初期データはこの payload がすべて）。
//
// 入力するのは試合日・ラインアップ・打席だけ。得点・イニングスコア・登板順・
// 勝敗はサーバーが打席から導く（導出できるものは入力させない）。
import { useState } from "react";
import type { FormEvent } from "react";
import { postJson } from "../lib/api";
import { DerivedCard } from "./DerivedCard";
import { GameInfoCard } from "./GameInfoCard";
import { LineupCard } from "./LineupCard";
import { ScorebookCard } from "./ScorebookCard";
import { buildInitialState, buildRequestBody } from "./transform";
import type { GameEditPayload, GameUpdateSuccess, ScorebookState } from "./types";

interface Props {
  payload: GameEditPayload;
}

export function App({ payload }: Props) {
  const [state, setState] = useState<ScorebookState>(() => buildInitialState(payload));
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSaving(true);
    setError(null);

    const body = buildRequestBody(state, payload);
    const result = await postJson<GameUpdateSuccess>(payload.urls.save, payload.csrf_token, body);

    if (result.ok) {
      window.location.assign(result.redirect_url);
      return;
    }

    setError(result.error);
    setSaving(false);
    window.scrollTo({ top: 0 });
  }

  return (
    <form onSubmit={handleSubmit}>
      {error && (
        <div className="alert alert-danger" role="alert">
          {error}
        </div>
      )}

      <GameInfoCard state={state} setState={setState} payload={payload} />
      <LineupCard state={state} setState={setState} payload={payload} />
      <ScorebookCard state={state} setState={setState} payload={payload} />
      <DerivedCard state={state} payload={payload} />

      <div className="d-flex justify-content-between">
        <a href={payload.urls.detail} className="btn btn-secondary">
          戻る
        </a>
        <button type="submit" className="btn btn-primary" disabled={saving}>
          {saving ? "保存中…" : "保存する"}
        </button>
      </div>
    </form>
  );
}
