"""合成の起点: `coldaisle-fand` の管理ソケット（`control-admin`。#74 / 決定記録 0072）。

運転モード（`AUTO` / `MANUAL` / `MAX`）と Authority Stage の降格を、走っている `coldaisle-fand` へ
人が届ける入口。
`coldaisle-eventd`（0045）とは別の入口で、読み取り API にも LLM のツールにも生やさない。

- `coldaisle.control` はこの package を import しない。loop が知るのは
  `coldaisle.control.operating_mode.ModeMailbox` の Protocol だけ（0072 §2.2）
- `coldaisle.ai` / `coldaisle.api` / `coldaisle.server` / `coldaisle.event_entry` も import しない
  （0072 §2.9。試験で走査する）
- 管理ソケットは**制御権を増やせない。** 受理するのはモードを選ぶ・authority を下げる・読むだけ
  （段階 2 の #92 で `lower_authority` / `rollback_authority` を足した。昇格の操作は無い）

`control_daemon.py` が `open_control_admin()` で束ねる。
"""

from coldaisle.control_admin.runtime import ControlAdminEntry, open_control_admin

__all__ = ["ControlAdminEntry", "open_control_admin"]
