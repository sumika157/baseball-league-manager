// 「スコアブック」カード。打順9行 × イニング列のマス目と、打席の入力パネル。
//
// **入力は順番に積む。** 打席の通し番号は1から欠けずに続き、打順は1〜9を巡回する
// という決まりがあるので、任意のマスに後から差し込む形にはしない。マス目は
// 記録済みの打席を読むためのもので、クリックするとその打席を直せる。
import { useState } from "react";
import type { Dispatch, SetStateAction } from "react";
import { PlateAppearancePanel } from "./PlateAppearancePanel";
import { currentPitcherId, nextBattingOrder, replay } from "./transform";
import { LINEUP_SIZE } from "./types";
import type { GameEditPayload, HalfState, PlateAppearancePayload, ScorebookState, TeamPayload } from "./types";

interface Props {
  state: ScorebookState;
  setState: Dispatch<SetStateAction<ScorebookState>>;
  payload: GameEditPayload;
}

export function ScorebookCard({ state, setState, payload }: Props) {
  const [editing, setEditing] = useState<PlateAppearancePayload | null>(null);
  const [recording, setRecording] = useState(false);

  const entries = state.plate_appearances;
  const { before, next } = replay(entries);
  const nameOf = buildNameIndex(payload);
  const home = payload.teams.find((team) => team.is_home);
  const away = payload.teams.find((team) => !team.is_home);
  const battingTeam = next.is_bottom ? home : away;
  const fieldingTeam = next.is_bottom ? away : home;

  const draft = recording ? (editing ?? buildNextEntry(state, payload, next)) : editing;
  const draftBefore = draft && draft.sequence <= entries.length ? before[draft.sequence - 1] : next;

  function commit(entry: PlateAppearancePayload) {
    setState((current) => {
      const kept = current.plate_appearances.filter((each) => each.sequence !== entry.sequence);
      return {
        ...current,
        plate_appearances: [...kept, entry].sort((a, b) => a.sequence - b.sequence),
      };
    });
    setEditing(null);
    setRecording(false);
  }

  function removeLast() {
    setState((current) => ({ ...current, plate_appearances: current.plate_appearances.slice(0, -1) }));
    setEditing(null);
    setRecording(false);
  }

  return (
    <div className="card mb-3">
      <div className="card-header d-flex justify-content-between align-items-center">
        <span>スコアブック</span>
        <span className="text-body-secondary">{entries.length}打席</span>
      </div>
      <div className="card-body">
        {payload.teams.map((team) => (
          <Grid
            key={team.team_id}
            team={team}
            entries={entries.filter((entry) => entry.is_bottom === team.is_home)}
            nameOf={nameOf}
            onPick={(entry) => {
              setEditing(entry);
              setRecording(false);
            }}
          />
        ))}

        {draft && draftBefore ? (
          <PlateAppearancePanel
            payload={payload}
            entry={draft}
            before={draftBefore}
            nameOf={nameOf}
            pitchers={fieldingTeam?.players ?? []}
            fielders={(fieldingTeam?.players ?? []).filter((player) => !player.is_pitcher)}
            onChange={(entry) => {
              setEditing(entry);
              if (entry.sequence > entries.length) setRecording(true);
            }}
            onSubmit={() => commit(draft)}
            onCancel={() => {
              setEditing(null);
              setRecording(false);
            }}
            submitLabel={draft.sequence > entries.length ? "この打席を記録する" : "この打席を直す"}
          />
        ) : (
          <div className="d-flex gap-2 align-items-center">
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => {
                setEditing(null);
                setRecording(true);
              }}
              disabled={!battingTeam || (state.lineups[battingTeam.team_id] ?? []).length === 0}
            >
              次の打席を記録する
            </button>
            <span className="text-body-secondary">
              {next.inning}回{next.is_bottom ? "裏" : "表"} {battingTeam?.team_name}の攻撃・{next.outs}アウト
            </span>
            {entries.length > 0 && (
              <button type="button" className="btn btn-outline-secondary ms-auto" onClick={removeLast}>
                最後の打席を取り消す
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

interface GridProps {
  team: TeamPayload;
  entries: PlateAppearancePayload[];
  nameOf: (playerId: number) => string;
  onPick: (entry: PlateAppearancePayload) => void;
}

/** 打順9行 × イニング列のマス目。紙のスコアブックと同じ並び。 */
function Grid({ team, entries, nameOf, onPick }: GridProps) {
  const innings = Math.max(1, ...entries.map((entry) => entry.inning));
  const columns = Array.from({ length: innings }, (_, index) => index + 1);

  return (
    <div className="table-responsive mb-4">
      <h2 className="h6 mb-2">{team.team_name}</h2>
      <table className="table table-sm entry-table align-middle">
        <thead>
          <tr>
            <th scope="col" style={{ width: "3rem" }}>
              打順
            </th>
            {columns.map((inning) => (
              <th scope="col" key={inning} className="text-center">
                {inning}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {Array.from({ length: LINEUP_SIZE }, (_, index) => index + 1).map((order) => (
            <tr key={order}>
              <th scope="row" className="tabular-nums">
                {order}
              </th>
              {columns.map((inning) => {
                const cell = entries.filter((entry) => entry.batting_order === order && entry.inning === inning);
                return (
                  <td key={inning} className="text-center">
                    {cell.map((entry) => (
                      <button
                        key={entry.sequence}
                        type="button"
                        className="btn btn-sm btn-link p-0 d-block mx-auto"
                        title={nameOf(entry.batter_id)}
                        onClick={() => onPick(entry)}
                      >
                        {entry.result}
                      </button>
                    ))}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function buildNameIndex(payload: GameEditPayload): (playerId: number) => string {
  const names = new Map<number, string>();
  for (const team of payload.teams) {
    for (const player of team.players) names.set(player.id, player.name);
  }
  return (playerId: number) => names.get(playerId) ?? `選手${playerId}`;
}

/** 次に記録する打席の下書き。打順・打者・投手は積み上げから決まる。 */
function buildNextEntry(state: ScorebookState, payload: GameEditPayload, next: HalfState): PlateAppearancePayload {
  const entries = state.plate_appearances;
  const battingTeam = payload.teams.find((team) => team.is_home === next.is_bottom);
  const order = nextBattingOrder(entries, next.is_bottom);
  const slot = (state.lineups[battingTeam?.team_id ?? 0] ?? []).find((each) => each.batting_order === order);
  const result = payload.vocabulary.results[0];

  return {
    sequence: entries.length + 1,
    inning: next.inning,
    is_bottom: next.is_bottom,
    batter_id: slot?.player_id ?? 0,
    pitcher_id: currentPitcherId(entries, next.is_bottom) ?? 0,
    batting_order: order,
    slot_sequence: slot?.slot_sequence ?? 0,
    result: result.label,
    fielded_by: "",
    advances: [
      {
        runner_id: slot?.player_id ?? 0,
        from_base: 0,
        to_base: result.default_batter_base,
        reason: result.default_batter_reason,
        error_index: null,
      },
    ],
    errors: [],
  };
}
