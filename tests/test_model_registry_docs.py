"""`docs/model-registry.md` の runtime contract と CLI の例が artifact v2 と合っていること。

#104 / 決定記録 0079 §2.9 の段 5。

文書の例は運用者がそのまま写す。v1 の schema のまま残ると、v2 の production を
`unusable` と判定させる contract を配ることになるため、例そのものを読んで確かめる。
`rollback` は戻り先の artifact の schema を渡すので、v2 同士と移行期の v1 への例を分けて持つ。
"""

from __future__ import annotations

import re
from pathlib import Path

from coldaisle import registry as cli
from coldaisle.control.model import CounterfactualRegistryMetadata
from coldaisle.control.model.thermal import FEATURE_SCHEMA_VERSION, TARGET_SCHEMA_VERSION
from coldaisle.control.model_registry import ArtifactKind

_ROOT = Path(__file__).resolve().parents[1]
_DOC = _ROOT / "docs" / "model-registry.md"
_COMMAND = re.compile(r"uv run coldaisle-registry (\w+)((?:[^\n]*\\\\?\n)*[^\n]*)")
_SCHEMA_FLAGS = re.compile(r"--feature-schema (\S+) --target-schema (\S+)")


def _v2_schemas() -> tuple[str, str]:
    fields = CounterfactualRegistryMetadata.model_fields
    feature = fields["feature_schema_version"].default
    target = fields["target_schema_version"].default
    assert isinstance(feature, str)
    assert isinstance(target, str)
    return feature, target


def _schemas_by_command(text: str) -> dict[str, set[tuple[str, str]]]:
    found: dict[str, set[tuple[str, str]]] = {}
    for command, rest in _COMMAND.findall(text):
        for pair in _SCHEMA_FLAGS.findall(rest.split("uv run")[0]):
            found.setdefault(command, set()).add(pair)
    return found


def _contract_examples(text: str) -> list[str]:
    blocks = re.findall(r"```yaml\n(.*?)```", text, flags=re.DOTALL)
    return [block for block in blocks if "contracts:" in block]


def test_the_documented_runtime_contract_parses_and_names_the_v2_schemas() -> None:
    examples = _contract_examples(_DOC.read_text(encoding="utf-8"))
    assert examples, "docs/model-registry.md に runtime contract の例が無い"
    for example in examples:
        contracts = cli.RuntimeContracts.from_bytes(example.encode("utf-8")).compatibility()
        thermal = contracts[ArtifactKind.THERMAL_MODEL]
        assert (thermal.feature_schema_version, thermal.target_schema_version) == _v2_schemas()


def test_documented_promote_examples_use_the_v2_schemas() -> None:
    for text in (_DOC.read_text(encoding="utf-8"), cli.__doc__ or ""):
        assert _schemas_by_command(text).get("promote") == {_v2_schemas()}


def test_documented_rollback_examples_name_the_schema_of_the_rollback_target() -> None:
    """v2 同士の rollback と、移行期に v1 の戻り先へ戻す rollback の両方を例に持つ。

    `rollback()` は戻り先を渡された互換性で検証する。promotion は戻り先を checksum と
    format だけで選ぶので、最初の v2 の後の戻り先は v1 でありうる（決定記録 0037 §2）。
    """
    v1 = (FEATURE_SCHEMA_VERSION, TARGET_SCHEMA_VERSION)
    rollbacks = _schemas_by_command(_DOC.read_text(encoding="utf-8")).get("rollback")
    assert rollbacks == {_v2_schemas(), v1}
    assert _schemas_by_command(cli.__doc__ or "").get("rollback") == {_v2_schemas()}
