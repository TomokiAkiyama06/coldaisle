"""合成の起点: Learned MPC / RL Supervisor worker と `coldaisle-fand` の経路（#86 / 0077）。

`coldaisle-fand` が役割ごとの `SOCK_SEQPACKET` ソケットで待ち受け、worker が接続する。worker から
受け取るのは結果（MPC の `MpcProposal`・RL Supervisor の `DeliveredSupervisorOutput`）と
heartbeat だけで、`coldaisle-fand` から送るのは毎 tick の frame（`LearnedFrame`）だけである。

- `coldaisle.control` はこの package を import しない。loop が知るのは
  `coldaisle.control.learned_handoff` の Protocol と、`LearnedProposalSource` /
  `SupervisorOutputSource` だけ（0077 §2.2 / §2.8）
- `coldaisle.ai` / `coldaisle.api` / `coldaisle.server` / `coldaisle.event_entry` /
  `coldaisle.control_admin` も import しない。**LLM から到達できる経路を作らない**
  （AGENTS.md ルール1）
- worker が出せるのは `requested_demand` までの提案と、Supervisor の戦略・重み（Demand を
  含まない）で、Reactive Guard と Critical Safety を迂回する経路は無い（AGENTS.md ルール2）

`control_daemon.py` が `open_learned_channel()` で束ねる。
"""

from coldaisle.learned_channel.runtime import (
    DisabledLearnedChannel,
    LearnedChannelEntry,
    open_learned_channel,
)

__all__ = ["DisabledLearnedChannel", "LearnedChannelEntry", "open_learned_channel"]
