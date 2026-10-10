"""``pwmN_enable=0`` を全速として確かめた hwmon driver の一覧（決定記録 0118 §2.3a）。

hwmon の ABI では ``pwmN_enable=0`` は「制御なし（全速）」だが、driver によっては
Fan を止める意味になる（例: Linux の ``pwm-fan``）。実機 backend は、この一覧に無い
driver の header では制御を取らない。

一覧は設定ではなくコードの定数に置く。設定の書き換えだけで、確かめていない driver に
``0`` を書けるようにしないためである。引き継ぎ実行部（``coldaisle.safety_handoff``）は
coldaisle のパッケージに依存しないため同じ一覧を自分で持ち、2つが一致することを
試験で確かめる。driver を足すには、導入先で 0118 §2.6 と同じ確認を行い、新しい
決定記録で決める。
"""

from __future__ import annotations

VERIFIED_HWMON_DRIVERS: frozenset[str] = frozenset({"nct6799"})
"""``pwmN_enable=0`` が全速になることを導入先で確かめた driver（0118 §2.6）。"""
