// 「投球成績」カード。rosters の順にチームごとのセクションを分けて表示する。
// 被本塁打 > 被安打 の行には、保存前に気づけるよう警告文を出す（送信は妨げない）。
import { Fragment } from "react";
import type { Dispatch, SetStateAction } from "react";
import { PITCHING_COUNT_FIELDS } from "./types";
import type { GameEditFormState, PitcherFormRow } from "./types";
import { hasHomeRunWarning, isPitcherActive } from "./transform";

interface Props {
  state: GameEditFormState;
  setState: Dispatch<SetStateAction<GameEditFormState>>;
}

const COUNT_LABELS: Record<(typeof PITCHING_COUNT_FIELDS)[number], string> = {
  earned_runs: "自責点",
  strikeouts: "奪三振",
  hits_allowed: "被安打",
  walks_allowed: "与四球",
  home_runs_allowed: "被本塁打",
  hit_by_pitch_allowed: "与死球",
};

// 選手・登板・投球回 + カウント列の数。警告行の colSpan に使う。
const COLUMN_COUNT = 3 + PITCHING_COUNT_FIELDS.length;

export function PitchingCard({ state, setState }: Props) {
  const totalPitchers = state.rosters.reduce((sum, roster) => sum + roster.pitchers.length, 0);

  function updatePitcher(teamId: number, playerId: number, patch: Partial<PitcherFormRow>) {
    setState((current) => ({
      ...current,
      rosters: current.rosters.map((roster) =>
        roster.team_id !== teamId
          ? roster
          : {
              ...roster,
              pitchers: roster.pitchers.map((row) => (row.player_id === playerId ? { ...row, ...patch } : row)),
            },
      ),
    }));
  }

  return (
    <div className="card mb-3">
      <div className="card-header">投球成績</div>
      <div className="card-body p-0">
        {totalPitchers === 0 ? (
          <div className="empty-state" style={{ padding: "2rem 1rem", fontSize: "0.875rem" }}>
            両チームに投手が登録されていません。
          </div>
        ) : (
          <>
            <p className="page-subtitle mb-0" style={{ fontSize: "0.8125rem", padding: "0.875rem 1.25rem 0" }}>
              被本塁打は被安打の内数です（被安打より多い値は保存できません）。
              勝敗・セーブ・ホールドは入力しません。イニングスコアと「登板」した回から
              日本プロ野球の規則で決まります。
            </p>
            {state.rosters.map((roster) => (
              <div key={roster.team_id}>
                <div className="card-header">{roster.team_name}</div>
                {roster.pitchers.length === 0 ? (
                  <div className="empty-state" style={{ padding: "1rem 1.25rem", fontSize: "0.875rem" }}>
                    投手が登録されていません。
                  </div>
                ) : (
                  <div className="table-responsive">
                    <table className="table entry-table mb-0">
                      <thead>
                        <tr>
                          <th>選手</th>
                          <th className="text-center">登板</th>
                          <th className="text-end">投球回</th>
                          {PITCHING_COUNT_FIELDS.map((field) => (
                            <th key={field} className="text-end">
                              {COUNT_LABELS[field]}
                            </th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {roster.pitchers.map((row) => (
                          <Fragment key={row.player_id}>
                            <tr className={isPitcherActive(row) ? "table-active" : undefined}>
                              <td>
                                <span className="jersey-number me-1">{row.number}</span>
                                <span className="player-name">{row.name}</span>
                              </td>
                              <td>
                                <input
                                  type="number"
                                  min={1}
                                  max={12}
                                  aria-label={`${row.name} 登板`}
                                  value={row.entered_inning}
                                  onChange={(event) =>
                                    updatePitcher(roster.team_id, row.player_id, {
                                      entered_inning: event.target.value,
                                    })
                                  }
                                />
                              </td>
                              <td>
                                <input
                                  type="number"
                                  min={0}
                                  step="0.1"
                                  aria-label={`${row.name} 投球回`}
                                  value={row.innings_pitched}
                                  onChange={(event) =>
                                    updatePitcher(roster.team_id, row.player_id, {
                                      innings_pitched: event.target.value,
                                    })
                                  }
                                />
                              </td>
                              {PITCHING_COUNT_FIELDS.map((field) => (
                                <td key={field}>
                                  <input
                                    type="number"
                                    min={0}
                                    aria-label={`${row.name} ${COUNT_LABELS[field]}`}
                                    value={row[field]}
                                    onChange={(event) => {
                                      const value = event.target.value;
                                      updatePitcher(roster.team_id, row.player_id, { [field]: value } as Partial<
                                        Pick<PitcherFormRow, typeof field>
                                      >);
                                    }}
                                  />
                                </td>
                              ))}
                            </tr>
                            {hasHomeRunWarning(row) && (
                              <tr>
                                <td colSpan={COLUMN_COUNT} className="text-danger">
                                  被本塁打が被安打を上回っています（被本塁打は被安打の内数です）
                                </td>
                              </tr>
                            )}
                          </Fragment>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            ))}
          </>
        )}
      </div>
    </div>
  );
}
