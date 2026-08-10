// game_edit/types.ts
// テンプレート埋め込み payload・保存 API と1:1で対応する型定義。
// キーは snake_case のまま（camelCase への変換層は作らない）。

/** 打撃成績のうち数値カウントとして送るフィールド。 */
export const BATTING_STAT_FIELDS = [
  "at_bats",
  "singles",
  "doubles",
  "triples",
  "home_runs",
  "runs_batted_in",
  "walks",
  "hit_by_pitch",
  "sacrifice_flies",
] as const;

export type BattingStatField = (typeof BATTING_STAT_FIELDS)[number];

/** 投球成績のうち数値カウントとして送るフィールド。 */
export const PITCHING_COUNT_FIELDS = [
  "earned_runs",
  "strikeouts",
  "hits_allowed",
  "walks_allowed",
  "home_runs_allowed",
  "hit_by_pitch_allowed",
] as const;

export type PitchingCountField = (typeof PITCHING_COUNT_FIELDS)[number];

// ---------------------------------------------------------------------------
// payload（GET・json_script 埋め込み）
// ---------------------------------------------------------------------------

/** payload に載る試合の基本情報。 */
export interface GamePayload {
  id: number;
  year: number;
  played_on: string;
  home_team: number;
  away_team: number;
  home_score: number;
  away_score: number;
}

/** イニングスコア1回ぶん。未実施の回は away/home が null。 */
export interface InningPayload {
  inning: number;
  away: number | null;
  home: number | null;
}

/** 打者1人ぶんの行。未入力の欄は null。 */
export type BatterPayload = {
  player_id: number;
  name: string;
  number: number;
  batting_order: number | null;
  slot_sequence: number | null;
  fielding_position: string | null;
} & Record<BattingStatField, number | null>;

/** 投手1人ぶんの行。未入力の欄は null。投球回は "6.1" 形式の文字列（Decimal のシリアライズ結果）。 */
export type PitcherPayload = {
  player_id: number;
  name: string;
  number: number;
  entered_inning: number | null;
  innings_pitched: string | null;
} & Record<PitchingCountField, number | null>;

/** チームごとのロスター（打者・投手一覧）。 */
export interface RosterPayload {
  team_id: number;
  team_name: string;
  is_home: boolean;
  batters: BatterPayload[];
  pitchers: PitcherPayload[];
}

export interface GameEditUrls {
  save: string;
  detail: string;
  list: string;
}

/** テンプレートに `<script id="game-edit-data" type="application/json">` で埋め込まれる初期データ。 */
export interface GameEditPayload {
  game: GamePayload;
  innings: InningPayload[];
  rosters: RosterPayload[];
  fielding_positions: string[];
  max_innings: number;
  urls: GameEditUrls;
  csrf_token: string;
}

// ---------------------------------------------------------------------------
// 編集中の状態（すべて文字列で保持。空欄 = ""）
// ---------------------------------------------------------------------------

export type BatterFormRow = {
  player_id: number;
  name: string;
  number: number;
  batting_order: string;
  slot_sequence: string;
  fielding_position: string;
} & Record<BattingStatField, string>;

export type PitcherFormRow = {
  player_id: number;
  name: string;
  number: number;
  entered_inning: string;
  innings_pitched: string;
} & Record<PitchingCountField, string>;

export interface RosterFormState {
  team_id: number;
  team_name: string;
  is_home: boolean;
  batters: BatterFormRow[];
  pitchers: PitcherFormRow[];
}

export interface InningFormRow {
  inning: number;
  away: string;
  home: string;
}

/** 画面全体の編集状態。 */
export interface GameEditFormState {
  year: string;
  played_on: string;
  home_score: string;
  away_score: string;
  innings: InningFormRow[];
  rosters: RosterFormState[];
}

// ---------------------------------------------------------------------------
// POST /api/games/<id>/ のリクエスト body
// ---------------------------------------------------------------------------

/** player_id と inning だけ number。それ以外は文字列のまま送る（"" は未入力扱い）。 */
export interface InningRequest {
  inning: number;
  away: string;
  home: string;
}

export type BattingRequestRow = {
  player_id: number;
  batting_order: string;
  slot_sequence: string;
  fielding_position: string;
} & Record<BattingStatField, string>;

export type PitchingRequestRow = {
  player_id: number;
  entered_inning: string;
  innings_pitched: string;
} & Record<PitchingCountField, string>;

export interface GameUpdateRequest {
  year: string;
  played_on: string;
  home_team: number;
  away_team: number;
  home_score: string;
  away_score: string;
  innings: InningRequest[];
  batting: BattingRequestRow[];
  pitching: PitchingRequestRow[];
}

/** 保存 API 成功時に付随するデータ（`lib/api.ts` の `ApiResult` に載せる）。 */
export interface GameUpdateSuccess {
  redirect_url: string;
}
