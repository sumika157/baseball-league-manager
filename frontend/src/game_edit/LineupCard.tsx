// 「ラインアップ」カード。両チームの打順1〜9をロスターから選ぶ。
//
// **これだけで「34人ぶんの行が並ぶ」問題が解消する。** 以前はロスター全員に
// 成績の入力欄が並んでいたが、実際に出場するのは打者19人・投手10人ほどだった。
import type { Dispatch, SetStateAction } from "react";
import { LINEUP_SIZE } from "./types";
import type { GameEditPayload, LineupSlotPayload, ScorebookState, TeamPayload } from "./types";

interface Props {
  state: ScorebookState;
  setState: Dispatch<SetStateAction<ScorebookState>>;
  payload: GameEditPayload;
}

export function LineupCard({ state, setState, payload }: Props) {
  return (
    <div className="card mb-3">
      <div className="card-header">ラインアップ</div>
      <div className="card-body">
        <div className="row g-4">
          {payload.teams.map((team) => (
            <div className="col-lg-6" key={team.team_id}>
              <TeamLineup team={team} state={state} setState={setState} payload={payload} />
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function TeamLineup({ team, state, setState, payload }: Props & { team: TeamPayload }) {
  const slots = state.lineups[team.team_id] ?? [];
  const taken = new Set(slots.map((slot) => slot.player_id));

  function update(order: number, changes: Partial<LineupSlotPayload>) {
    setState((current) => {
      const own = [...(current.lineups[team.team_id] ?? [])];
      const index = own.findIndex((slot) => slot.batting_order === order);
      const base: LineupSlotPayload = own[index] ?? {
        player_id: 0,
        batting_order: order,
        slot_sequence: 0,
        fielding_position: "",
      };
      const next = { ...base, ...changes };
      if (index >= 0) own[index] = next;
      else own.push(next);
      // 選手を「未選択」に戻したらその枠ごと外す（空の枠を送らない）
      const kept = own.filter((slot) => slot.player_id > 0).sort((a, b) => a.batting_order - b.batting_order);
      return { ...current, lineups: { ...current.lineups, [team.team_id]: kept } };
    });
  }

  return (
    <>
      <h2 className="h6 mb-2">
        {team.team_name}
        <span className="text-body-secondary ms-2">{team.is_home ? "ホーム" : "ビジター"}</span>
      </h2>
      <table className="table table-sm entry-table align-middle">
        <thead>
          <tr>
            <th scope="col" style={{ width: "3rem" }}>
              打順
            </th>
            <th scope="col">選手</th>
            <th scope="col" style={{ width: "6rem" }}>
              守備
            </th>
          </tr>
        </thead>
        <tbody>
          {Array.from({ length: LINEUP_SIZE }, (_, index) => index + 1).map((order) => {
            const slot = slots.find((each) => each.batting_order === order);
            return (
              <tr key={order}>
                <th scope="row" className="tabular-nums">
                  {order}
                </th>
                <td>
                  <select
                    className="form-select form-select-sm"
                    aria-label={`${team.team_name} ${order}番の選手`}
                    value={slot?.player_id ?? ""}
                    onChange={(event) => update(order, { player_id: Number(event.target.value) || 0 })}
                  >
                    <option value="">—</option>
                    {team.players.map((player) => (
                      <option
                        key={player.id}
                        value={player.id}
                        disabled={taken.has(player.id) && slot?.player_id !== player.id}
                      >
                        {player.number} {player.name}
                      </option>
                    ))}
                  </select>
                </td>
                <td>
                  <select
                    className="form-select form-select-sm"
                    aria-label={`${team.team_name} ${order}番の守備位置`}
                    value={slot?.fielding_position ?? ""}
                    onChange={(event) => update(order, { fielding_position: event.target.value })}
                  >
                    <option value="">—</option>
                    {payload.vocabulary.fielding_positions.map((position) => (
                      <option key={position} value={position}>
                        {position}
                      </option>
                    ))}
                  </select>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </>
  );
}
