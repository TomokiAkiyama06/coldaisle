"""合成の起点: `coldaisle-fand` の管理ソケット（`control-admin`。#74 / 決定記録 0072）。

運転モード（`AUTO` / `MANUAL` / `MAX`）を、走っている `coldaisle-fand` へ人が届ける入口。
`coldaisle-eventd`（0045）とは別の入口で、読み取り API にも LLM のツールにも生やさない。

- `coldaisle.control` はこの package を import しない。loop が知るのは
  `coldaisle.control.operating_mode.ModeMailbox` の Protocol だけ（0072 §2.2）
- `coldaisle.ai` / `coldaisle.api` / `coldaisle.server` / `coldaisle.event_entry` も import しない
  （0072 §2.9。試験で走査する）
- 管理ソケットは**制御権を増やせない。** 受理するのはモードを選ぶ・読むだけ（段階 1）

`control_daemon.py` が `open_control_admin()` で束ねる。
"""

from coldaisle.control_admin.runtime import ControlAdminEntry, open_control_admin

__all__ = ["ControlAdminEntry", "open_control_admin"]
