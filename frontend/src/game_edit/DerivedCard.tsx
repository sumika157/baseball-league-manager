// 「導出値の確認」カード。イニングスコアと検算の結果を読み取り専用で出す。
//
// **紙のスコアブックの縦計・横計にあたる。** 打点の合計と得点が食い違っていれば
// 記録のどこかが間違っている。ここに出すのは目安で、**確定はサーバー**が行う
// （保存すると集約が同じ検算をして、通らなければ日本語のメッセージが返る）。
import { deriveLineScore, totalRuns } from "./transform";
import { BASE_HOME } from "./types";
import type { GameEditPayload, ScorebookState } from "./types";

interface Props {
  state: ScorebookState;
  payload: GameEditPayload;
}

export function DerivedCard({ state, payload }: Props) {
  const entries = state.plate_appearances;
  const score = deriveLineScore(entries);
  const innings = Math.max(score.away.length, score.home.length);
  const home = payload.teams.find((team) => team.is_home);
  const away = payload.teams.find((team) => !team.is_home);

  const runs = totalRuns(score.away) + totalRuns(score.home);
  const battedIn = entries.reduce((sum, entry) => sum + runsBattedIn(entry, payload), 0);

  return (
    <div className="card mb-3">
      <div className="card-header">導出値の確認</div>
      <div className="card-body">
        <div className="table-responsive">
          <table className="table table-sm entry-table text-center align-middle">
            <thead>
              <tr>
                <th scope="col" className="text-start">
                  チーム
                </th>
                {Array.from({ length: innings }, (_, index) => index + 1).map((inning) => (
                  <th scope="col" key={inning}>
                    {inning}
                  </th>
                ))}
                <th scope="col">計</th>
              </tr>
            </thead>
            <tbody>
              {[
                { name: away?.team_name ?? "ビジター", values: score.away },
                { name: home?.team_name ?? "ホーム", values: score.home },
              ].map((row) => (
                <tr key={row.name}>
                  <th scope="row" className="text-start">
                    {row.name}
                  </th>
                  {Array.from({ length: innings }, (_, index) => (
                    <td key={index} className="tabular-nums">
                      {index < row.values.length ? row.values[index] : ""}
                    </td>
                  ))}
                  <td className="tabular-nums fw-bold">{totalRuns(row.values)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <p className="mb-0 text-body-secondary">
          得点 {runs} ／ 打点 {battedIn}
          {battedIn > runs && <span className="text-danger ms-2">打点が得点を超えています。記録を見直してください。</span>}
        </p>
      </div>
    </div>
  );
}

/** 打点の目安。失策・野選で還った得点には付かない（判定の出典は vocabulary）。 */
function runsBattedIn(entry: ScorebookState["plate_appearances"][number], payload: GameEditPayload): number {
  const outs = entry.advances.filter((advance) => advance.to_base === -1).length;
  if (outs >= 2) return 0; // 併殺の間の得点には付かない
  const earning = new Set(
    payload.vocabulary.reasons.filter((reason) => reason.earns_run_batted_in).map((reason) => reason.label),
  );
  return entry.advances.filter((advance) => advance.to_base === BASE_HOME && earning.has(advance.reason)).length;
}
