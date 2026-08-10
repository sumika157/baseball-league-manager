// game_edit/transform.ts
// payload ⇔ 編集状態 ⇔ 送信 body の変換と、画面側の入力補助（自動計算・警告・行のハイライト判定）。
// 業務ルールの確定判断はサーバー（ドメイン層）が持つ。ここでの判定はあくまで入力補助。

import {
  BATTING_STAT_FIELDS,
  PITCHING_COUNT_FIELDS,
  type BatterFormRow,
  type BatterPayload,
  type BattingRequestRow,
  type GameEditFormState,
  type GameEditPayload,
  type GameUpdateRequest,
  type InningFormRow,
  type PitcherFormRow,
  type PitcherPayload,
  type PitchingRequestRow,
} from "./types";

/** payload の null は編集状態では空文字列として表す。 */
function toInputValue(value: number | string | null): string {
  return value === null || value === undefined ? "" : String(value);
}

function toBatterFormRow(player: BatterPayload): BatterFormRow {
  const stats = {} as Record<(typeof BATTING_STAT_FIELDS)[number], string>;
  for (const field of BATTING_STAT_FIELDS) {
    stats[field] = toInputValue(player[field]);
  }
  return {
    player_id: player.player_id,
    name: player.name,
    number: player.number,
    batting_order: toInputValue(player.batting_order),
    slot_sequence: toInputValue(player.slot_sequence),
    fielding_position: toInputValue(player.fielding_position),
    ...stats,
  };
}

function toPitcherFormRow(player: PitcherPayload): PitcherFormRow {
  const counts = {} as Record<(typeof PITCHING_COUNT_FIELDS)[number], string>;
  for (const field of PITCHING_COUNT_FIELDS) {
    counts[field] = toInputValue(player[field]);
  }
  return {
    player_id: player.player_id,
    name: player.name,
    number: player.number,
    entered_inning: toInputValue(player.entered_inning),
    innings_pitched: toInputValue(player.innings_pitched),
    ...counts,
  };
}

/** payload から編集状態の初期値を組み立てる。 */
export function buildInitialState(payload: GameEditPayload): GameEditFormState {
  return {
    year: toInputValue(payload.game.year),
    played_on: payload.game.played_on,
    home_score: toInputValue(payload.game.home_score),
    away_score: toInputValue(payload.game.away_score),
    innings: payload.innings.map((row) => ({
      inning: row.inning,
      away: toInputValue(row.away),
      home: toInputValue(row.home),
    })),
    rosters: payload.rosters.map((roster) => ({
      team_id: roster.team_id,
      team_name: roster.team_name,
      is_home: roster.is_home,
      batters: roster.batters.map(toBatterFormRow),
      pitchers: roster.pitchers.map(toPitcherFormRow),
    })),
  };
}

/**
 * 文字列入力を数値に変換する。空欄・不正な値は 0 として扱う。
 * 表示上の合計・警告・行のハイライト判定にだけ使う（送信値そのものには使わない）。
 */
function parseOrZero(value: string): number {
  if (value.trim() === "") {
    return 0;
  }
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

export function sumInningValues(rows: InningFormRow[], key: "away" | "home"): number {
  return rows.reduce((total, row) => total + parseOrZero(row[key]), 0);
}

export function hasAnyInningInput(rows: InningFormRow[]): boolean {
  return rows.some((row) => row.away !== "" || row.home !== "");
}

export interface DerivedScores {
  home: string;
  away: string;
  /** true の間、得点欄は自動計算値を表示する読み取り専用になる。 */
  locked: boolean;
}

/** イニングスコアが1マスでも入力されていたら、得点は表裏の合計から自動計算する。 */
export function deriveScores(state: GameEditFormState): DerivedScores {
  const locked = hasAnyInningInput(state.innings);
  if (!locked) {
    return { home: state.home_score, away: state.away_score, locked };
  }
  return {
    home: String(sumInningValues(state.innings, "home")),
    away: String(sumInningValues(state.innings, "away")),
    locked,
  };
}

/** 統計のどれかが 0 より大きい、または打順か守備位置が入力されている行を出場扱いとする。 */
export function isBatterActive(row: BatterFormRow): boolean {
  const hasStat = BATTING_STAT_FIELDS.some((field) => parseOrZero(row[field]) > 0);
  return hasStat || row.batting_order !== "" || row.fielding_position !== "";
}

/** 投球回が 0 より大きい、またはカウントのどれかが 0 より大きい行を出場扱いとする。 */
export function isPitcherActive(row: PitcherFormRow): boolean {
  const hasInnings = parseOrZero(row.innings_pitched) > 0;
  const hasCount = PITCHING_COUNT_FIELDS.some((field) => parseOrZero(row[field]) > 0);
  return hasInnings || hasCount;
}

/** 被本塁打は被安打の内数。取り違えを保存前に気づけるようにする（送信は妨げない）。 */
export function hasHomeRunWarning(row: PitcherFormRow): boolean {
  return parseOrZero(row.home_runs_allowed) > parseOrZero(row.hits_allowed);
}

/** 編集状態から保存 API へ送る body を組み立てる。全行を送り、空行の間引きはサーバーに任せる。 */
export function buildRequestBody(
  state: GameEditFormState,
  payload: GameEditPayload,
  scores: DerivedScores,
): GameUpdateRequest {
  const batting: BattingRequestRow[] = state.rosters.flatMap((roster) =>
    roster.batters.map((row) => {
      const stats = {} as Record<(typeof BATTING_STAT_FIELDS)[number], string>;
      for (const field of BATTING_STAT_FIELDS) {
        stats[field] = row[field];
      }
      return {
        player_id: row.player_id,
        batting_order: row.batting_order,
        slot_sequence: row.slot_sequence,
        fielding_position: row.fielding_position,
        ...stats,
      };
    }),
  );

  const pitching: PitchingRequestRow[] = state.rosters.flatMap((roster) =>
    roster.pitchers.map((row) => {
      const counts = {} as Record<(typeof PITCHING_COUNT_FIELDS)[number], string>;
      for (const field of PITCHING_COUNT_FIELDS) {
        counts[field] = row[field];
      }
      return {
        player_id: row.player_id,
        entered_inning: row.entered_inning,
        innings_pitched: row.innings_pitched,
        ...counts,
      };
    }),
  );

  return {
    year: state.year,
    played_on: state.played_on,
    home_team: payload.game.home_team,
    away_team: payload.game.away_team,
    home_score: scores.home,
    away_score: scores.away,
    innings: state.innings.map((row) => ({
      inning: row.inning,
      away: row.away,
      home: row.home,
    })),
    batting,
    pitching,
  };
}
