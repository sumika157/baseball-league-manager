// 「打撃成績」カード。rosters の順にチームごとのセクションを分けて表示する。
import type { Dispatch, SetStateAction } from "react";
import { BATTING_STAT_FIELDS } from "./types";
import type { BatterFormRow, GameEditFormState } from "./types";
import { isBatterActive } from "./transform";

interface Props {
  state: GameEditFormState;
  setState: Dispatch<SetStateAction<GameEditFormState>>;
  fieldingPositions: string[];
}

const STAT_LABELS: Record<(typeof BATTING_STAT_FIELDS)[number], string> = {
  at_bats: "打数",
  singles: "単打",
  doubles: "二塁打",
  triples: "三塁打",
  home_runs: "本塁打",
  runs_batted_in: "打点",
  walks: "四球",
  hit_by_pitch: "死球",
  sacrifice_flies: "犠飛",
};

export function BattingCard({ state, setState, fieldingPositions }: Props) {
  const totalBatters = state.rosters.reduce((sum, roster) => sum + roster.batters.length, 0);

  function updateBatter(teamId: number, playerId: number, patch: Partial<BatterFormRow>) {
    setState((current) => ({
      ...current,
      rosters: current.rosters.map((roster) =>
        roster.team_id !== teamId
          ? roster
          : {
              ...roster,
              batters: roster.batters.map((row) => (row.player_id === playerId ? { ...row, ...patch } : row)),
            },
      ),
    }));
  }

  return (
    <div className="card mb-3">
      <div className="card-header">打撃成績</div>
      <div className="card-body p-0">
        {totalBatters === 0 ? (
          <div className="empty-state" style={{ padding: "2rem 1rem", fontSize: "0.875rem" }}>
            両チームに野手が登録されていません。
          </div>
        ) : (
          state.rosters.map((roster) => (
            <div key={roster.team_id}>
              <div className="card-header">{roster.team_name}</div>
              {roster.batters.length === 0 ? (
                <div className="empty-state" style={{ padding: "1rem 1.25rem", fontSize: "0.875rem" }}>
                  野手が登録されていません。
                </div>
              ) : (
                <div className="table-responsive">
                  <table className="table entry-table mb-0">
                    <thead>
                      <tr>
                        <th>選手</th>
                        <th className="text-center">打順</th>
                        <th className="text-center">交代</th>
                        <th className="text-center">守備</th>
                        {BATTING_STAT_FIELDS.map((field) => (
                          <th key={field} className="text-end">
                            {STAT_LABELS[field]}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {roster.batters.map((row) => (
                        <tr key={row.player_id} className={isBatterActive(row) ? "table-active" : undefined}>
                          <td>
                            <span className="jersey-number me-1">{row.number}</span>
                            <span className="player-name">{row.name}</span>
                          </td>
                          <td>
                            <input
                              type="number"
                              min={1}
                              max={9}
                              aria-label={`${row.name} 打順`}
                              value={row.batting_order}
                              onChange={(event) =>
                                updateBatter(roster.team_id, row.player_id, { batting_order: event.target.value })
                              }
                            />
                          </td>
                          <td>
                            <input
                              type="number"
                              min={0}
                              max={9}
                              aria-label={`${row.name} 交代`}
                              value={row.slot_sequence}
                              onChange={(event) =>
                                updateBatter(roster.team_id, row.player_id, { slot_sequence: event.target.value })
                              }
                            />
                          </td>
                          <td>
                            <select
                              aria-label={`${row.name} 守備位置`}
                              value={row.fielding_position}
                              onChange={(event) =>
                                updateBatter(roster.team_id, row.player_id, {
                                  fielding_position: event.target.value,
                                })
                              }
                            >
                              <option value="">—</option>
                              {fieldingPositions.map((position) => (
                                <option key={position} value={position}>
                                  {position}
                                </option>
                              ))}
                            </select>
                          </td>
                          {BATTING_STAT_FIELDS.map((field) => (
                            <td key={field}>
                              <input
                                type="number"
                                min={0}
                                aria-label={`${row.name} ${STAT_LABELS[field]}`}
                                value={row[field]}
                                onChange={(event) => {
                                  const value = event.target.value;
                                  updateBatter(roster.team_id, row.player_id, { [field]: value } as Partial<
                                    Pick<BatterFormRow, typeof field>
                                  >);
                                }}
                              />
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          ))
        )}
      </div>
    </div>
  );
}
