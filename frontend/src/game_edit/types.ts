// game_edit/types.ts
// テンプレート埋め込み payload・保存 API と1:1で対応する型定義。
// キーは snake_case のまま（camelCase への変換層は作らない）。
//
// **打席の語彙（結果・進塁の理由・失策の種類）はここに列挙しない。** payload の
// vocabulary としてサーバーから届く。TypeScript から Python の Enum は読めないので、
// ここに書き写すとずれても例外にならず、選択肢や既定値だけが静かに古くなる。

/** 塁の番号。ドメインの Base と同じ値で、**大小がそのまま「進んだか」を表す**。
 *  表示名は vocabulary.bases から引く。数字が一致していることは
 *  tests/integration/test_stat_fields.py が突き合わせる。 */
export const BASE_BATTER = 0;
export const BASE_FIRST = 1;
export const BASE_SECOND = 2;
export const BASE_THIRD = 3;
export const BASE_HOME = 4;
export const BASE_OUT = -1;

/** 走者が留まれる塁。先の塁から順に並べてある（進塁は必ずこの順で適用する）。 */
export const OCCUPIABLE_BASES = [BASE_THIRD, BASE_SECOND, BASE_FIRST] as const;

export const OUTS_PER_INNING = 3;
export const LINEUP_SIZE = 9;

// ---------------------------------------------------------------------------
// payload（GET・json_script 埋め込み）
// ---------------------------------------------------------------------------

export interface GamePayload {
  id: number;
  year: number;
  played_on: string;
  home_team: number;
  away_team: number;
  home_score: number;
  away_score: number;
}

export interface PlayerPayload {
  id: number;
  name: string;
  number: number;
  position: string;
  is_pitcher: boolean;
}

/** 打順の1枠。誰が何番でどこを守ったか。 */
export interface LineupSlotPayload {
  player_id: number;
  batting_order: number;
  slot_sequence: number;
  fielding_position: string;
}

export interface TeamPayload {
  team_id: number;
  team_name: string;
  is_home: boolean;
  players: PlayerPayload[];
  lineup: LineupSlotPayload[];
}

/** 走者1人ぶんの進塁。打者自身も走者として持つ（進塁前が BASE_BATTER）。 */
export interface AdvancePayload {
  runner_id: number;
  from_base: number;
  to_base: number;
  reason: string;
  error_index: number | null;
}

export interface FieldingErrorPayload {
  player_id: number;
  position: string;
  kind: string;
}

export interface PlateAppearancePayload {
  sequence: number;
  inning: number;
  is_bottom: boolean;
  batter_id: number;
  pitcher_id: number;
  batting_order: number;
  slot_sequence: number;
  result: string;
  fielded_by: string;
  advances: AdvancePayload[];
  errors: FieldingErrorPayload[];
}

/** 打席の結果1つぶんの語彙。既定の進塁もここに載っている。 */
export interface ResultVocabulary {
  label: string;
  retires_batter: boolean;
  is_hit: boolean;
  counts_as_at_bat: boolean;
  requires_error: boolean;
  default_batter_base: number;
  default_batter_reason: string;
  /** 塁上の走者の動かし方。値の意味はドメインの DefaultRunnerAdvance を参照。 */
  default_runner_advance: string;
  default_runner_reason: string;
}

export interface ReasonVocabulary {
  label: string;
  is_out: boolean;
  earns_run_batted_in: boolean;
}

export interface Vocabulary {
  results: ResultVocabulary[];
  reasons: ReasonVocabulary[];
  bases: { value: number; label: string }[];
  error_kinds: string[];
  fielding_positions: string[];
  defensive_positions: string[];
}

export interface GameEditUrls {
  save: string;
  detail: string;
}

export interface GameEditPayload {
  game: GamePayload;
  teams: TeamPayload[];
  plate_appearances: PlateAppearancePayload[];
  vocabulary: Vocabulary;
  max_innings: number;
  urls: GameEditUrls;
  csrf_token: string;
}

// ---------------------------------------------------------------------------
// 編集中の状態
// ---------------------------------------------------------------------------

/** 画面全体の編集状態。打席と打順はそのまま送り返せる形で持つ。 */
export interface ScorebookState {
  year: string;
  played_on: string;
  /** チーム id → 打順の枠。 */
  lineups: Record<number, LineupSlotPayload[]>;
  plate_appearances: PlateAppearancePayload[];
}

/** 半回の途中の状態。打席を順に適用して求める。 */
export interface HalfState {
  inning: number;
  is_bottom: boolean;
  outs: number;
  /** 塁の番号 → 走者の選手 id。 */
  occupied: Record<number, number>;
}

// ---------------------------------------------------------------------------
// POST /api/games/<id>/scorebook/ のリクエスト body
// ---------------------------------------------------------------------------

export type LineupRequestRow = LineupSlotPayload & { team_id: number };

export interface ScorebookRequest {
  year: string;
  played_on: string;
  home_team: number;
  away_team: number;
  lineup: LineupRequestRow[];
  plate_appearances: PlateAppearancePayload[];
}

export interface GameUpdateSuccess {
  redirect_url: string;
}
