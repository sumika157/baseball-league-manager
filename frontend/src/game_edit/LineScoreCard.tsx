// 「イニングスコア」カード。表＝ビジター、裏＝ホームの順（旧テンプレートを踏襲）。
// 「計」列に表裏それぞれの合計を表示する。
import type { Dispatch, SetStateAction } from "react";
import { sumInningValues } from "./transform";
import type { GameEditFormState } from "./types";

interface Props {
  state: GameEditFormState;
  setState: Dispatch<SetStateAction<GameEditFormState>>;
  homeTeamName: string;
  awayTeamName: string;
}

export function LineScoreCard({ state, setState, homeTeamName, awayTeamName }: Props) {
  const awayTotal = sumInningValues(state.innings, "away");
  const homeTotal = sumInningValues(state.innings, "home");

  function updateInning(inning: number, key: "away" | "home", value: string) {
    setState((current) => ({
      ...current,
      innings: current.innings.map((row) => (row.inning === inning ? { ...row, [key]: value } : row)),
    }));
  }

  return (
    <div className="card mb-3">
      <div className="card-header">イニングスコア</div>
      <div className="card-body">
        <p className="page-subtitle mb-3" style={{ fontSize: "0.8125rem" }}>
          回ごとの得点を入れると、勝利投手・敗戦投手・セーブ・ホールドが
          日本プロ野球の規則で自動的に決まります。
          行われていない回は空欄にしてください（ホームが最終回を攻めていない場合は裏を空欄に）。
        </p>
        <div className="table-responsive">
          <table className="table linescore-table entry-table mb-0">
            <thead>
              <tr>
                <th></th>
                {state.innings.map((row) => (
                  <th key={row.inning} className="text-center">
                    {row.inning}
                  </th>
                ))}
                <th className="text-center linescore-total">計</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <th>{awayTeamName}（表）</th>
                {state.innings.map((row) => (
                  <td key={row.inning}>
                    <input
                      type="number"
                      min={0}
                      aria-label={`${row.inning}回表`}
                      value={row.away}
                      onChange={(event) => updateInning(row.inning, "away", event.target.value)}
                    />
                  </td>
                ))}
                <td className="text-center linescore-total">{awayTotal}</td>
              </tr>
              <tr>
                <th>{homeTeamName}（裏）</th>
                {state.innings.map((row) => (
                  <td key={row.inning}>
                    <input
                      type="number"
                      min={0}
                      aria-label={`${row.inning}回裏`}
                      value={row.home}
                      onChange={(event) => updateInning(row.inning, "home", event.target.value)}
                    />
                  </td>
                ))}
                <td className="text-center linescore-total">{homeTotal}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
