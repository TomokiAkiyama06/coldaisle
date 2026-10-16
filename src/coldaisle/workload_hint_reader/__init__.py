"""合成の起点: Workload Hint の読み手のスレッド（#319 / 決定記録 0133 §2.8 / 0066 §2.2）。

`coldaisle-fand` の中で、``events`` の ``workload_hint`` の行を**読み取り専用の接続**で読み、
ヒントの終端状態・run の記録・取り込みの境界を**別の接続**で自分の表にだけ書く。
検証済みの不変の値（`HintFeed`）を受け渡し口に置き、loop は tick ごとにそれを待たずに覗く。

- `coldaisle.control` はこの package を import しない。loop が知るのは
  `coldaisle.control.supervisor.workload_hint.WorkloadHintMailbox` の Protocol だけ
- `coldaisle.ai` / `coldaisle.api` / `coldaisle.server` もこの package を import しない
- ``events`` には書かない（書き込みの入口は 0045 のソケットだけ）
- 読めない・書けないはヒント無しに倒すだけで、制御は止めない（0133 §2.8。MAX にしない）

`control_daemon.py` の `_open_workload_hint()` が束ねる。
"""

from coldaisle.workload_hint_reader.mailbox import HintMailbox
from coldaisle.workload_hint_reader.reader import (
    WorkloadHintReader,
    open_events_reader,
    parse_hint_row,
)

__all__ = ["HintMailbox", "WorkloadHintReader", "open_events_reader", "parse_hint_row"]
