// 1打席の入力パネル。結果を選ぶと進塁が既定値で埋まり、違うときだけ触る。
//
// **既定値の対応表は持たない。** payload の vocabulary（ドメインの値オブジェクト
// から払い出したもの）を見て組み立てる。確定はサーバーが行う。
import { defaultAdvances } from "./transform";
import { BASE_BATTER, BASE_HOME, BASE_OUT, OCCUPIABLE_BASES } from "./types";
import type {
  AdvancePayload,
  GameEditPayload,
  HalfState,
  PlateAppearancePayload,
  PlayerPayload,
} from "./types";

interface Props {
  payload: GameEditPayload;
  entry: PlateAppearancePayload;
  before: HalfState;
  nameOf: (playerId: number) => string;
  pitchers: PlayerPayload[];
  fielders: PlayerPayload[];
  onChange: (entry: PlateAppearancePayload) => void;
  onSubmit: () => void;
  onCancel: () => void;
  submitLabel: string;
}

export function PlateAppearancePanel({
  payload,
  entry,
  before,
  nameOf,
  pitchers,
  fielders,
  onChange,
  onSubmit,
  onCancel,
  submitLabel,
}: Props) {
  const vocabulary = payload.vocabulary;
  const result = vocabulary.results.find((each) => each.label === entry.result);
  const baseLabel = (value: number) => vocabulary.bases.find((base) => base.value === value)?.label ?? String(value);

  function chooseResult(label: string) {
    const chosen = vocabulary.results.find((each) => each.label === label);
    if (!chosen) return;
    onChange({
      ...entry,
      result: label,
      advances: defaultAdvances(chosen, before.occupied, entry.batter_id),
      errors: chosen.requires_error && entry.errors.length === 0 ? [blankError(vocabulary)] : entry.errors,
    });
  }

  function updateAdvance(index: number, changes: Partial<AdvancePayload>) {
    const advances = entry.advances.map((advance, at) => (at === index ? { ...advance, ...changes } : advance));
    onChange({ ...entry, advances });
  }

  const onBase = OCCUPIABLE_BASES.filter((base) => before.occupied[base] !== undefined);
  const unmoved = onBase.filter((base) => !entry.advances.some((advance) => advance.from_base === base));

  return (
    <div className="card mb-3">
      <div className="card-header">
        {entry.inning}回{entry.is_bottom ? "裏" : "表"} {entry.batting_order}番 {nameOf(entry.batter_id)}
        <span className="text-body-secondary ms-3">
          {before.outs}アウト・
          {onBase.length ? onBase.map((base) => baseLabel(base)).join("・") : "走者なし"}
        </span>
      </div>
      <div className="card-body">
        <div className="row g-3 mb-3">
          <div className="col-md-4">
            <label className="form-label" htmlFor="pa-result">
              結果
            </label>
            <select
              id="pa-result"
              className="form-select"
              value={entry.result}
              onChange={(event) => chooseResult(event.target.value)}
            >
              {vocabulary.results.map((each) => (
                <option key={each.label} value={each.label}>
                  {each.label}
                </option>
              ))}
            </select>
          </div>
          <div className="col-md-4">
            <label className="form-label" htmlFor="pa-pitcher">
              投手
            </label>
            <select
              id="pa-pitcher"
              className="form-select"
              value={entry.pitcher_id || ""}
              onChange={(event) => onChange({ ...entry, pitcher_id: Number(event.target.value) || 0 })}
            >
              <option value="">—</option>
              {pitchers.map((player) => (
                <option key={player.id} value={player.id}>
                  {player.number} {player.name}
                </option>
              ))}
            </select>
          </div>
          <div className="col-md-4">
            <label className="form-label" htmlFor="pa-fielded-by">
              打球の処理
            </label>
            <input
              id="pa-fielded-by"
              type="text"
              className="form-control"
              placeholder="遊-一"
              value={entry.fielded_by}
              onChange={(event) => onChange({ ...entry, fielded_by: event.target.value })}
            />
          </div>
        </div>

        {result?.requires_error && (
          <ErrorRows payload={payload} entry={entry} fielders={fielders} onChange={onChange} />
        )}

        <h3 className="h6">走者の動き</h3>
        <table className="table table-sm entry-table align-middle">
          <thead>
            <tr>
              <th scope="col">走者</th>
              <th scope="col" style={{ width: "6rem" }}>
                進塁前
              </th>
              <th scope="col" style={{ width: "8rem" }}>
                進塁後
              </th>
              <th scope="col" style={{ width: "10rem" }}>
                理由
              </th>
              <th scope="col" style={{ width: "4rem" }} />
            </tr>
          </thead>
          <tbody>
            {entry.advances.map((advance, index) => (
              <tr key={`${advance.runner_id}-${advance.from_base}`}>
                <td>{nameOf(advance.runner_id)}</td>
                <td>{baseLabel(advance.from_base)}</td>
                <td>
                  <select
                    className="form-select form-select-sm"
                    aria-label={`${nameOf(advance.runner_id)}の進塁後`}
                    value={advance.to_base}
                    onChange={(event) => updateAdvance(index, { to_base: Number(event.target.value) })}
                  >
                    {vocabulary.bases
                      .filter((base) => base.value > advance.from_base || base.value === BASE_OUT)
                      .map((base) => (
                        <option key={base.value} value={base.value}>
                          {base.label}
                        </option>
                      ))}
                  </select>
                </td>
                <td>
                  <select
                    className="form-select form-select-sm"
                    aria-label={`${nameOf(advance.runner_id)}の理由`}
                    value={advance.reason}
                    onChange={(event) => updateAdvance(index, { reason: event.target.value })}
                  >
                    {vocabulary.reasons
                      .filter((reason) => reason.is_out === (advance.to_base === BASE_OUT))
                      .map((reason) => (
                        <option key={reason.label} value={reason.label}>
                          {reason.label}
                        </option>
                      ))}
                  </select>
                </td>
                <td>
                  {advance.from_base !== BASE_BATTER && (
                    <button
                      type="button"
                      className="btn btn-sm btn-outline-secondary"
                      onClick={() =>
                        onChange({ ...entry, advances: entry.advances.filter((_, at) => at !== index) })
                      }
                    >
                      外す
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>

        {unmoved.length > 0 && (
          <div className="mb-3">
            {unmoved.map((base) => (
              <button
                key={base}
                type="button"
                className="btn btn-sm btn-outline-secondary me-2"
                onClick={() =>
                  onChange({
                    ...entry,
                    advances: [
                      {
                        runner_id: before.occupied[base],
                        from_base: base,
                        to_base: Math.min(base + 1, BASE_HOME),
                        reason: vocabulary.reasons[0].label,
                        error_index: null,
                      },
                      ...entry.advances,
                    ],
                  })
                }
              >
                {baseLabel(base)}の走者を動かす
              </button>
            ))}
          </div>
        )}

        <div className="d-flex gap-2">
          <button type="button" className="btn btn-primary" onClick={onSubmit}>
            {submitLabel}
          </button>
          <button type="button" className="btn btn-secondary" onClick={onCancel}>
            やめる
          </button>
        </div>
      </div>
    </div>
  );
}

function blankError(vocabulary: GameEditPayload["vocabulary"]) {
  return { player_id: 0, position: vocabulary.defensive_positions[0], kind: vocabulary.error_kinds[0] };
}

interface ErrorProps {
  payload: GameEditPayload;
  entry: PlateAppearancePayload;
  fielders: PlayerPayload[];
  onChange: (entry: PlateAppearancePayload) => void;
}

function ErrorRows({ payload, entry, fielders, onChange }: ErrorProps) {
  const errors = entry.errors.length > 0 ? entry.errors : [blankError(payload.vocabulary)];

  function update(index: number, changes: Partial<(typeof errors)[number]>) {
    onChange({ ...entry, errors: errors.map((each, at) => (at === index ? { ...each, ...changes } : each)) });
  }

  return (
    <div className="row g-3 mb-3">
      {errors.map((error, index) => (
        <div className="col-md-8" key={index}>
          <label className="form-label" htmlFor={`pa-error-${index}`}>
            失策
          </label>
          <div className="d-flex gap-2">
            <select
              id={`pa-error-${index}`}
              className="form-select"
              value={error.player_id || ""}
              onChange={(event) => update(index, { player_id: Number(event.target.value) || 0 })}
            >
              <option value="">守備者を選ぶ</option>
              {fielders.map((player) => (
                <option key={player.id} value={player.id}>
                  {player.number} {player.name}
                </option>
              ))}
            </select>
            <select
              className="form-select"
              aria-label="失策の守備位置"
              value={error.position}
              onChange={(event) => update(index, { position: event.target.value })}
            >
              {payload.vocabulary.defensive_positions.map((position) => (
                <option key={position} value={position}>
                  {position}
                </option>
              ))}
            </select>
            <select
              className="form-select"
              aria-label="失策の種類"
              value={error.kind}
              onChange={(event) => update(index, { kind: event.target.value })}
            >
              {payload.vocabulary.error_kinds.map((kind) => (
                <option key={kind} value={kind}>
                  {kind}
                </option>
              ))}
            </select>
          </div>
        </div>
      ))}
    </div>
  );
}
