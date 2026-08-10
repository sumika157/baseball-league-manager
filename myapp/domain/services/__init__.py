"""ドメインサービス。

単一のエンティティには属さないが、業務ルールであるものを置く。
関心事ごとのモジュールに分かれており、利用側はこのパッケージ経由で使う
（`from myapp.domain import services` のままでよい）。

- rankings: 規定（規定打席・規定投球回）とタイトルランキング
- sorting: 一覧の並べ替えキーと既定の向き
- records: 試合からの集計（通算・順位表・対戦成績・月別成績）
- decisions: 勝敗・セーブ・ホールドの導出（日本プロ野球の規則）
"""

from .decisions import (
    SAVE_LEAD_LIMIT,
    SAVE_LONG_RELIEF_OUTS,
    SAVE_MINIMUM_OUTS,
    STARTER_WIN_MINIMUM_OUTS,
    PitchingDecisions,
    pitching_decisions,
)
from .rankings import (
    QUALIFYING_INNINGS_PER_GAME,
    QUALIFYING_PLATE_APPEARANCES_PER_GAME,
    RankedPlayer,
    leaders_by_batting_average,
    leaders_by_era,
    leaders_by_home_runs,
    leaders_by_ops,
    leaders_by_runs_batted_in,
    leaders_by_strikeouts,
    qualified_batters,
    qualified_pitchers,
    required_outs,
    required_plate_appearances,
)
from .records import (
    MatchupRow,
    MonthlySplit,
    StandingRow,
    TeamMonthlySplit,
    fip_constant,
    head_to_head,
    matchups,
    monthly_splits,
    player_batting_total,
    player_pitching_total,
    seasons_of,
    standings,
    team_batting,
    team_monthly_splits,
    team_pitching,
    team_record,
)
from .sorting import (
    BATTER_SORT_KEYS,
    DEFAULT_BATTER_SORT,
    DEFAULT_PITCHER_SORT,
    PITCHER_SORT_KEYS,
    sort_batters,
    sort_pitchers,
)

__all__ = [
    # rankings
    "QUALIFYING_INNINGS_PER_GAME",
    "QUALIFYING_PLATE_APPEARANCES_PER_GAME",
    "RankedPlayer",
    "leaders_by_batting_average",
    "leaders_by_era",
    "leaders_by_home_runs",
    "leaders_by_ops",
    "leaders_by_runs_batted_in",
    "leaders_by_strikeouts",
    "qualified_batters",
    "qualified_pitchers",
    "required_outs",
    "required_plate_appearances",
    # sorting
    "BATTER_SORT_KEYS",
    "DEFAULT_BATTER_SORT",
    "DEFAULT_PITCHER_SORT",
    "PITCHER_SORT_KEYS",
    "sort_batters",
    "sort_pitchers",
    # records
    "MatchupRow",
    "MonthlySplit",
    "StandingRow",
    "TeamMonthlySplit",
    "fip_constant",
    "head_to_head",
    "matchups",
    "monthly_splits",
    "player_batting_total",
    "player_pitching_total",
    "seasons_of",
    "standings",
    "team_batting",
    "team_monthly_splits",
    "team_pitching",
    "team_record",
    # decisions
    "SAVE_LEAD_LIMIT",
    "SAVE_LONG_RELIEF_OUTS",
    "SAVE_MINIMUM_OUTS",
    "STARTER_WIN_MINIMUM_OUTS",
    "PitchingDecisions",
    "pitching_decisions",
]
