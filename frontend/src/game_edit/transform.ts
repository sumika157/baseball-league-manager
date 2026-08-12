// game_edit/transform.ts
// payload ⇄ 編集状態 ⇄ リクエスト body の変換と、入力補助の計算。
//
// **ここでやるのは入力補助まで。** 確定はサーバー（フォームとドメイン層）が行う。
// 塁の再生も既定の進塁も、画面を先回りさせるためのもので、判定の出典ではない。

import {
  BASE_BATTER,
  BASE_HOME,
  BASE_OUT,
  BASE_THIRD,
  LINEUP_SIZE,
  OCCUPIABLE_BASES,
  OUTS_PER_INNING,
} from "./types";
import type {
  AdvancePayload,
  GameEditPayload,
  HalfState,
  LineupRequestRow,
  PlateAppearancePayload,
  ResultVocabulary,
  ScorebookRequest,
  ScorebookState,
} from "./types";

export function buildInitialState(payload: GameEditPayload): ScorebookState {
  const lineups: Record<number, ScorebookState["lineups"][number]> = {};
  for (const team of payload.teams) {
    lineups[team.team_id] = [...team.lineup].sort(
      (a, b) => a.batting_order - b.batting_order || a.slot_sequence - b.slot_sequence,
    );
  }
  return {
    year: String(payload.game.year),
    played_on: payload.game.played_on,
    lineups,
    plate_appearances: [...payload.plate_appearances].sort((a, b) => a.sequence - b.sequence),
  };
}

export function buildRequestBody(state: ScorebookState, payload: GameEditPayload): ScorebookRequest {
  const lineup: LineupRequestRow[] = [];
  for (const team of payload.teams) {
    for (const slot of state.lineups[team.team_id] ?? []) {
      lineup.push({ ...slot, team_id: team.team_id });
    }
  }
  return {
    year: state.year,
    played_on: state.played_on,
    home_team: payload.game.home_team,
    away_team: payload.game.away_team,
    lineup,
    plate_appearances: state.plate_appearances,
  };
}

// --- 塁の再生 -------------------------------------------------------------

const EMPTY_HALF: HalfState = { inning: 1, is_bottom: false, outs: 0, occupied: {} };

/** 打席を順に適用し、「各打席の直前の状態」と「最後の打席のあとの状態」を返す。
 *
 *  半回の切れ目は打席が持つ回・表裏の変わり目で判断する（ドメインの再生と同じ）。 */
export function replay(entries: PlateAppearancePayload[]): { before: HalfState[]; next: HalfState } {
  const before: HalfState[] = [];
  let current: HalfState = { ...EMPTY_HALF, occupied: {} };

  for (const entry of entries) {
    if (entry.inning !== current.inning || entry.is_bottom !== current.is_bottom) {
      current = { inning: entry.inning, is_bottom: entry.is_bottom, outs: 0, occupied: {} };
    }
    before.push({ ...current, occupied: { ...current.occupied } });

    const occupied = { ...current.occupied };
    let outs = current.outs;
    // 先の塁の走者から動かす（一塁走者を先に動かすと二塁走者に塞がれて見える）
    for (const advance of [...entry.advances].sort((a, b) => b.from_base - a.from_base)) {
      if (advance.from_base !== BASE_BATTER) delete occupied[advance.from_base];
      if (advance.to_base === BASE_OUT) outs += 1;
      else if (advance.to_base !== BASE_HOME) occupied[advance.to_base] = advance.runner_id;
    }
    current = { ...current, outs, occupied };
  }

  return { before, next: advanceHalfIfOver(current) };
}

function advanceHalfIfOver(state: HalfState): HalfState {
  if (state.outs < OUTS_PER_INNING) return state;
  return state.is_bottom
    ? { inning: state.inning + 1, is_bottom: false, outs: 0, occupied: {} }
    : { inning: state.inning, is_bottom: true, outs: 0, occupied: {} };
}

/** 次に打席に立つ枠。打順はチーム（表裏）ごとに1〜9を巡回する。 */
export function nextBattingOrder(entries: PlateAppearancePayload[], isBottom: boolean): number {
  const own = entries.filter((entry) => entry.is_bottom === isBottom);
  const last = own[own.length - 1];
  return last ? (last.batting_order % LINEUP_SIZE) + 1 : 1;
}

/** その守備側が最後に投げさせた投手。継投していなければそのまま次の打席も投げる。 */
export function currentPitcherId(entries: PlateAppearancePayload[], isBottom: boolean): number | null {
  const own = entries.filter((entry) => entry.is_bottom === isBottom);
  const last = own[own.length - 1];
  return last ? last.pitcher_id : null;
}

// --- 既定の進塁 -----------------------------------------------------------

/** 結果を選んだ時点で埋める進塁。**対応表はサーバー（ドメイン）から届く。**
 *
 *  素朴に作ると1打席あたり3〜5操作になるので、大半の打席が「結果を選ぶだけ」で
 *  終わるようにする。既定と違うときだけ触る。 */
export function defaultAdvances(
  result: ResultVocabulary,
  occupied: Record<number, number>,
  batterId: number,
): AdvancePayload[] {
  const advances: AdvancePayload[] = [];
  const taken = { ...occupied };
  const errorIndex = result.requires_error ? 0 : null;
  const reason = result.default_runner_reason;

  const place = (from: number, wanted: number) => {
    let target = wanted;
    while (target !== BASE_HOME && target !== BASE_OUT && taken[target] !== undefined) {
      if (target <= from + 1) return; // 隣も塞がっている。この走者は動かない
      target -= 1;
    }
    const runner = taken[from];
    delete taken[from];
    if (target !== BASE_HOME) taken[target] = runner;
    advances.push({ runner_id: runner, from_base: from, to_base: target, reason, error_index: errorIndex });
  };

  const step = STEPS[result.default_runner_advance] ?? 0;
  for (const base of OCCUPIABLE_BASES) {
    if (taken[base] === undefined) continue;
    if (result.default_runner_advance === "all_home") place(base, BASE_HOME);
    else if (result.default_runner_advance === "third_scores") {
      if (base === BASE_THIRD) place(base, BASE_HOME);
    } else if (result.default_runner_advance === "forced_only") {
      if (isForced(base, occupied)) place(base, base + 1);
    } else if (step > 0) place(base, Math.min(base + step, BASE_HOME));
  }

  advances.push({
    runner_id: batterId,
    from_base: BASE_BATTER,
    to_base: result.default_batter_base,
    reason: result.default_batter_reason,
    error_index: errorIndex,
  });
  return advances;
}

const STEPS: Record<string, number> = { one_base: 1, two_bases: 2, three_bases: 3 };

/** 打者が一塁を与えられたときに押し出されるか。一塁から詰まっている塁だけ。 */
function isForced(base: number, occupied: Record<number, number>): boolean {
  for (let each = 1; each <= base; each += 1) {
    if (occupied[each] === undefined) return false;
  }
  return true;
}

// --- 導出値の目安 ---------------------------------------------------------

/** 回ごとの得点。サーバーが打席から導く値と同じものを画面にも出す（確定は保存時）。 */
export function deriveLineScore(entries: PlateAppearancePayload[]): { away: number[]; home: number[] } {
  const halves: { away: number[]; home: number[] } = { away: [], home: [] };
  for (const entry of entries) {
    const side = entry.is_bottom ? halves.home : halves.away;
    while (side.length < entry.inning) side.push(0);
    side[entry.inning - 1] += entry.advances.filter((advance) => advance.to_base === BASE_HOME).length;
  }
  return halves;
}

export function totalRuns(values: number[]): number {
  return values.reduce((sum, runs) => sum + runs, 0);
}
