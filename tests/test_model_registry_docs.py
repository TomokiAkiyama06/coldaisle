"""`docs/model-registry.md` の runtime contract の例が artifact v2 と合っていること。

#104 / 決定記録 0079 §2.9 の段 5。

文書の例は運用者がそのまま写す。v1 の schema のまま残ると、v2 の production を
`unusable` と判定させる contract を配ることになるため、例そのものを読んで確かめる。
"""

from __future__ import annotations

import re
from pathlib import Path

from coldaisle import registry as cli
from coldaisle.control.model import CounterfactualRegistryMetadata
from coldaisle.control.model_registry import ArtifactKind

_ROOT = Path(__file__).resolve().parents[1]
_DOC = _ROOT / "docs" / "model-registry.md"
_SCHEMA_FLAGS = re.compile(r"--feature-schema (\S+) --target-schema (\S+)")


def _v2_schemas() -> tuple[str, str]:
    fields = CounterfactualRegistryMetadata.model_fields
    feature = fields["feature_schema_version"].default
    target = fields["target_schema_version"].default
    assert isinstance(feature, str)
    assert isinstance(target, str)
    return feature, target


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


def test_documented_promote_and_rollback_examples_use_the_v2_schemas() -> None:
    for text in (_DOC.read_text(encoding="utf-8"), cli.__doc__ or ""):
        pairs = _SCHEMA_FLAGS.findall(text)
        assert pairs, "promote / rollback の例が見つからない"
        assert set(pairs) == {_v2_schemas()}
