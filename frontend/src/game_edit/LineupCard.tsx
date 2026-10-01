// 「ラインアップ」カード。両チームの打順1〜9をロスターから選ぶ。
//
// **これだけで「34人ぶんの行が並ぶ」問題が解消する。** 以前はロスター全員に
// 成績の入力欄が並んでいたが、実際に出場するのは打者19人・投手10人ほどだった。
//
// 途中出場（代打・代走・守備固め）は、打順の枠の下に交代の行として足す。守備固めなど
// 打席から分からない出場だけ「出場した半回」を入力する（代打・代走は打席から決まる）。
import type { Dispatch, ReactNode, SetStateAction } from "react";
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

function blankSlot(order: number, slotSequence: number): LineupSlotPayload {
  return {
    player_id: 0,
    batting_order: order,
    slot_sequence: slotSequence,
    fielding_position: "",
    entered_inning: null,
    entered_is_bottom: false,
    entered_batter: 1,
  };
}

function TeamLineup({ team, state, setState, payload }: Props & { team: TeamPayload }) {
  const slots = state.lineups[team.team_id] ?? [];
  const taken = new Set(slots.filter((slot) => slot.player_id > 0).map((slot) => slot.player_id));

  /** 打順の枠（打順, 交代の順）の1行を書き換える。無ければ足す。 */
  function update(order: number, slotSequence: number, changes: Partial<LineupSlotPayload>) {
    setState((current) => {
      const own = [...(current.lineups[team.team_id] ?? [])];
      const index = own.findIndex((slot) => slot.batting_order === order && slot.slot_sequence === slotSequence);
      const next = { ...(own[index] ?? blankSlot(order, slotSequence)), ...changes };
      if (index >= 0) own[index] = next;
      else own.push(next);
      // スタメンを「未選択」に戻したらその枠ごと外す（空の枠を送らない）。
      // 途中出場の行は、選手を選ぶ前の下書きとして残す（送るときに選手のいない行は除く）
      const kept = own
        .filter((slot) => slot.player_id > 0 || slot.slot_sequence > 0)
        .sort((a, b) => a.batting_order - b.batting_order || a.slot_sequence - b.slot_sequence);
      return { ...current, lineups: { ...current.lineups, [team.team_id]: kept } };
    });
  }

  function addSubstitute(order: number) {
    const sequences = slots.filter((slot) => slot.batting_order === order).map((slot) => slot.slot_sequence);
    update(order, Math.max(0, ...sequences) + 1, {});
  }

  function removeSubstitute(order: number, slotSequence: number) {
    setState((current) => ({
      ...current,
      lineups: {
        ...current.lineups,
        [team.team_id]: (current.lineups[team.team_id] ?? []).filter(
          (slot) => !(slot.batting_order === order && slot.slot_sequence === slotSequence),
        ),
      },
    }));
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
            <th scope="col" style={{ width: "14rem" }}>
              出場した半回
            </th>
            <th scope="col" style={{ width: "6rem" }}>
              <span className="visually-hidden">操作</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {Array.from({ length: LINEUP_SIZE }, (_, index) => index + 1).flatMap((order) => {
            const rows = slots.filter((each) => each.batting_order === order);
            const starter = rows.find((each) => each.slot_sequence === 0);
            const substitutes = rows.filter((each) => each.slot_sequence > 0);
            return [
              <SlotRow
                key={`${order}-0`}
                team={team}
                payload={payload}
                slot={starter}
                order={order}
                slotSequence={0}
                taken={taken}
                update={update}
                action={
                  starter ? (
                    <button
                      type="button"
                      className="btn btn-sm btn-outline-secondary"
                      onClick={() => addSubstitute(order)}
                    >
                      交代を追加
                    </button>
                  ) : null
                }
              />,
              ...substitutes.map((slot) => (
                <SlotRow
                  key={`${order}-${slot.slot_sequence}`}
                  team={team}
                  payload={payload}
                  slot={slot}
                  order={order}
                  slotSequence={slot.slot_sequence}
                  taken={taken}
                  update={update}
                  action={
                    <button
                      type="button"
                      className="btn btn-sm btn-outline-danger"
                      onClick={() => removeSubstitute(order, slot.slot_sequence)}
                    >
                      削除
                    </button>
                  }
                />
              )),
            ];
          })}
        </tbody>
      </table>
      <p className="form-text">
        途中出場の「出場した半回」は、守備固めなど打席から分からない選手だけ入力します（代打・代走・投手は打席の記録から決まります）。代打からそのまま守備に就いた選手は、その代打の打席を指します。
      </p>
    </>
  );
}

interface SlotRowProps {
  team: TeamPayload;
  payload: GameEditPayload;
  slot: LineupSlotPayload | undefined;
  order: number;
  slotSequence: number;
  taken: Set<number>;
  update: (order: number, slotSequence: number, changes: Partial<LineupSlotPayload>) => void;
  action: ReactNode;
}

function SlotRow({ team, payload, slot, order, slotSequence, taken, update, action }: SlotRowProps) {
  const label = slotSequence === 0 ? `${order}番` : `${order}番の交代${slotSequence}`;
  return (
    <tr className={slotSequence > 0 ? "boxscore-substitute" : undefined}>
      <th scope="row" className="tabular-nums">
        {slotSequence === 0 ? order : ""}
      </th>
      <td>
        <select
          className="form-select form-select-sm"
          aria-label={`${team.team_name} ${label}の選手`}
          value={slot?.player_id || ""}
          onChange={(event) => update(order, slotSequence, { player_id: Number(event.target.value) || 0 })}
        >
          <option value="">—</option>
          {team.players.map((player) => (
            <option key={player.id} value={player.id} disabled={taken.has(player.id) && slot?.player_id !== player.id}>
              {player.number} {player.name}
            </option>
          ))}
        </select>
      </td>
      <td>
        <select
          className="form-select form-select-sm"
          aria-label={`${team.team_name} ${label}の守備位置`}
          value={slot?.fielding_position ?? ""}
          onChange={(event) => update(order, slotSequence, { fielding_position: event.target.value })}
        >
          <option value="">—</option>
          {payload.vocabulary.fielding_positions.map((position) => (
            <option key={position} value={position}>
              {position}
            </option>
          ))}
        </select>
      </td>
      <td>
        {slotSequence > 0 && !payload.vocabulary.entry_derived_positions.includes(slot?.fielding_position ?? "") && (
          <div className="input-group input-group-sm">
            <input
              type="number"
              min={1}
              max={payload.max_innings}
              className="form-control"
              aria-label={`${team.team_name} ${label}が出場した回`}
              value={slot?.entered_inning ?? ""}
              onChange={(event) => update(order, slotSequence, { entered_inning: Number(event.target.value) || null })}
            />
            <select
              className="form-select"
              aria-label={`${team.team_name} ${label}が出場したのは表か裏か`}
              value={slot?.entered_is_bottom ? "bottom" : "top"}
              onChange={(event) =>
                update(order, slotSequence, { entered_is_bottom: event.target.value === "bottom" })
              }
            >
              <option value="top">回表</option>
              <option value="bottom">回裏</option>
            </select>
            <input
              type="number"
              min={1}
              className="form-control"
              aria-label={`${team.team_name} ${label}が出場した打者番号`}
              title="その半回の何人目の打者から出場したか"
              value={slot?.entered_batter ?? 1}
              onChange={(event) => update(order, slotSequence, { entered_batter: Number(event.target.value) || 1 })}
            />
            <span className="input-group-text">人目</span>
          </div>
        )}
      </td>
      <td>{action}</td>
    </tr>
  );
}
