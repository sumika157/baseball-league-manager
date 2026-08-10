// 試合編集画面のルートコンポーネント。
// 保存は payload.urls.save への POST のみ（GET 用 API は無く、初期データはこの payload がすべて）。
import { useMemo, useState } from "react";
import type { FormEvent } from "react";
import { postJson } from "../lib/api";
import { BattingCard } from "./BattingCard";
import { GameInfoCard } from "./GameInfoCard";
import { LineScoreCard } from "./LineScoreCard";
import { PitchingCard } from "./PitchingCard";
import { buildInitialState, buildRequestBody, deriveScores } from "./transform";
import type { GameEditFormState, GameEditPayload, GameUpdateSuccess } from "./types";

interface Props {
  payload: GameEditPayload;
}

export function App({ payload }: Props) {
  const [state, setState] = useState<GameEditFormState>(() => buildInitialState(payload));
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const scores = useMemo(() => deriveScores(state), [state]);
  const homeRoster = payload.rosters.find((roster) => roster.is_home);
  const awayRoster = payload.rosters.find((roster) => !roster.is_home);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSaving(true);
    setError(null);

    const body = buildRequestBody(state, payload, scores);
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

      <GameInfoCard
        state={state}
        setState={setState}
        scores={scores}
        homeRoster={homeRoster}
        awayRoster={awayRoster}
      />
      <LineScoreCard
        state={state}
        setState={setState}
        homeTeamName={homeRoster?.team_name ?? ""}
        awayTeamName={awayRoster?.team_name ?? ""}
      />
      <BattingCard state={state} setState={setState} fieldingPositions={payload.fielding_positions} />
      <PitchingCard state={state} setState={setState} />

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
