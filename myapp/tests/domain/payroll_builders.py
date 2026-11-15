"""年俸の上限（#40 K1b）のテストの道具。テストのメソッドは持たない（`test_` で始めない）。"""

from myapp.domain.pennant.contract import OKU
from myapp.domain.pennant.payroll_cap import PayrollCap

# どの球団の年俸の合計よりも十分に高い上限（上限で止まらない場合の規則を見るテストに渡す）
AMPLE_CAP = PayrollCap(yen=1_000 * OKU, estimated=False)
