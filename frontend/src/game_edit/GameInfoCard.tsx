// 「試合の情報」カード。シーズン・試合日と、打席から導いた得点の表示。
// 得点は入力欄ではない（導出できるものは入力させない）。確定はサーバーが行う。
import type { Dispatch, SetStateAction } from "react";
import { deriveLineScore, totalRuns } from "./transform";
import type { GameEditPayload, ScorebookState } from "./types";

interface Props {
  state: ScorebookState;
  setState: Dispatch<SetStateAction<ScorebookState>>;
  payload: GameEditPayload;
}

export function GameInfoCard({ state, setState, payload }: Props) {
  const score = deriveLineScore(state.plate_appearances);
  const home = payload.teams.find((team) => team.is_home);
  const away = payload.teams.find((team) => !team.is_home);

  return (
    <div className="card mb-3">
      <div className="card-header">試合の情報</div>
      <div className="card-body">
        <div className="row g-3 align-items-end">
          <div className="col-md-3">
            <label className="form-label" htmlFor="scorebook-year">
              シーズン
            </label>
            <input
              id="scorebook-year"
              type="number"
              className="form-control"
              value={state.year}
              onChange={(event) => {
                const year = event.target.value;
                setState((current) => ({ ...current, year }));
              }}
              required
            />
          </div>
          <div className="col-md-3">
            <label className="form-label" htmlFor="scorebook-played-on">
              試合日
            </label>
            <input
              id="scorebook-played-on"
              type="date"
              className="form-control"
              value={state.played_on}
              onChange={(event) => {
                const played_on = event.target.value;
                setState((current) => ({ ...current, played_on }));
              }}
              required
            />
          </div>
          <div className="col-md-6">
            <p className="form-label mb-1">得点（打席から導きます）</p>
            <p className="mb-0 fs-4 tabular-nums" aria-label="導出した得点">
              {away?.team_name} {totalRuns(score.away)} - {totalRuns(score.home)} {home?.team_name}
            </p>
          </div>
        </div>
        {/* 打席から導けない手入力の事実。試合時間は「終了 − 開始 − 中断」で、保存後に試合詳細で見る */}
        <div className="row g-3 align-items-end mt-1">
          <div className="col-md-3">
            <label className="form-label" htmlFor="scorebook-start-time">
              開始時刻
            </label>
            <input
              id="scorebook-start-time"
              type="time"
              className="form-control"
              value={state.start_time}
              onChange={(event) => {
                const start_time = event.target.value;
                setState((current) => ({ ...current, start_time }));
              }}
            />
          </div>
          <div className="col-md-3">
            <label className="form-label" htmlFor="scorebook-end-time">
              終了時刻
            </label>
            <input
              id="scorebook-end-time"
              type="time"
              className="form-control"
              value={state.end_time}
              onChange={(event) => {
                const end_time = event.target.value;
                setState((current) => ({ ...current, end_time }));
              }}
            />
          </div>
          <div className="col-md-2">
            <div className="form-check">
              <input
                id="scorebook-ends-next-day"
                type="checkbox"
                className="form-check-input"
                checked={state.ends_next_day}
                onChange={(event) => {
                  const ends_next_day = event.target.checked;
                  setState((current) => ({ ...current, ends_next_day }));
                }}
              />
              <label className="form-check-label" htmlFor="scorebook-ends-next-day">
                終了が翌日
              </label>
            </div>
          </div>
          <div className="col-md-2">
            <label className="form-label" htmlFor="scorebook-interruption">
              中断（分）
            </label>
            <input
              id="scorebook-interruption"
              type="number"
              min={0}
              className="form-control"
              value={state.interruption_minutes}
              onChange={(event) => {
                const interruption_minutes = event.target.value;
                setState((current) => ({ ...current, interruption_minutes }));
              }}
            />
          </div>
          <div className="col-md-2">
            <label className="form-label" htmlFor="scorebook-attendance">
              観客数
            </label>
            <input
              id="scorebook-attendance"
              type="number"
              min={0}
              className="form-control"
              value={state.attendance}
              onChange={(event) => {
                const attendance = event.target.value;
                setState((current) => ({ ...current, attendance }));
              }}
            />
          </div>
        </div>
      </div>
    </div>
  );
}
