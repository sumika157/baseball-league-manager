// 「試合の情報」カード。シーズン・試合日・ホーム得点・ビジター得点。
// 得点はイニングスコアから自動計算できる間は読み取り専用にする（導出できるものは入力させない）。
import type { Dispatch, SetStateAction } from "react";
import type { DerivedScores } from "./transform";
import type { GameEditFormState, RosterPayload } from "./types";

interface Props {
  state: GameEditFormState;
  setState: Dispatch<SetStateAction<GameEditFormState>>;
  scores: DerivedScores;
  homeRoster: RosterPayload | undefined;
  awayRoster: RosterPayload | undefined;
}

export function GameInfoCard({ state, setState, scores, homeRoster, awayRoster }: Props) {
  return (
    <div className="card mb-3">
      <div className="card-header">試合の情報</div>
      <div className="card-body">
        <div className="row g-3">
          <div className="col-md-3">
            <label className="form-label">シーズン</label>
            <input
              type="number"
              className="form-control"
              aria-label="シーズン"
              value={state.year}
              onChange={(event) => {
                const year = event.target.value;
                setState((current) => ({ ...current, year }));
              }}
              required
            />
          </div>
          <div className="col-md-3">
            <label className="form-label">試合日</label>
            <input
              type="date"
              className="form-control"
              aria-label="試合日"
              value={state.played_on}
              onChange={(event) => {
                const played_on = event.target.value;
                setState((current) => ({ ...current, played_on }));
              }}
              required
            />
          </div>
          <div className="col-md-3">
            <label className="form-label">{homeRoster?.team_name}（ホーム）</label>
            <input
              type="number"
              className="form-control"
              min={0}
              aria-label="ホーム得点"
              value={scores.home}
              readOnly={scores.locked}
              onChange={(event) => {
                const home_score = event.target.value;
                setState((current) => ({ ...current, home_score }));
              }}
              required
            />
          </div>
          <div className="col-md-3">
            <label className="form-label">{awayRoster?.team_name}（ビジター）</label>
            <input
              type="number"
              className="form-control"
              min={0}
              aria-label="ビジター得点"
              value={scores.away}
              readOnly={scores.locked}
              onChange={(event) => {
                const away_score = event.target.value;
                setState((current) => ({ ...current, away_score }));
              }}
              required
            />
          </div>
        </div>
      </div>
    </div>
  );
}
