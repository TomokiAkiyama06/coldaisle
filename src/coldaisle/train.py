"""合成の起点: 学習の CLI ``coldaisle-train``（#84 / 決定記録 0150）。

Thermal Dataset v2 の公開物から反実仮想 Thermal Model artifact v2（``ridge_linear_v1``・
``thermal-features-v2``・capability ``counterfactual_action``・同梱 Confidence Profile v2）を作り、
``--out`` に公開する。``--apply`` のときだけ Model Registry へ **candidate** として登録する。

```bash
uv run coldaisle-train --dataset var/datasets/dataset-<32 hex> --replay-path ~/server_sensor_logs \\
    --calibration-history-db var/coldaisle.db --declared-changes var/declared-changes.yaml \\
    --quality-config config/quality.yaml --calibration config/calibration.json \\
    --metrics-config config/metrics.yaml --training-spec var/training.yaml \\
    --model-id rack-thermal --model-version 0.1.0 --created-at 2026-10-20T09:00:00+09:00 \\
    --out var/training/rack-thermal-0.1.0
```

- **人が手で実行する。** timer やデーモンからは起動しない
  （本番のオンライン学習はしない。0150 §5 の 7）
- **どこからも import しない**（``authority_cli.py`` と同じ扱い。0150 §2.1）。LLM に依らない
- 検査の順は 0150 §2.4: Dataset の1回の読み → 元の入力と本番の DB との照合と作り直し →
  較正の記録との照合 → split → 学習と同梱 Profile → 登録する bytes → 読み込み側の検査
- 設定はすべて上限で切って1回だけ読み、その bytes から parse と hash を行う（0150 §2.7）
- 壁時計を読まない（``--created-at`` は引数）。乱数を使わない
- 呼ぶ Registry の操作は ``register_candidate`` だけ
  （``validate`` / ``promote`` は呼ばない。0150 §2.6）。
  ``requested_demand``・PWM・hwmon・Authority Stage・制御設定へ届く経路を持たない（0150 §2.8）

失敗は構造化ログに段（``stage``）を出して終了コード 1。段と回復は 0150 §2.4 の表。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from coldaisle import logs
from coldaisle.calibration_log import verify_training_calibration
from coldaisle.control.drift.model import ChangeKind, DeclaredChange
from coldaisle.control.model.calibration_digest import RuntimeCalibration
from coldaisle.control.model.confidence import ConfidenceProfileSpec, SupportAxis
from coldaisle.control.model.counterfactual import (
    FEATURE_SCHEMA_V2_VERSION,
    TARGET_SCHEMA_V2_VERSION,
    CounterfactualThermalModelArtifact,
    RegistryCounterfactualThermalModel,
    canonical_counterfactual_artifact_bytes,
    check_artifact_contents,
    counterfactual_registry_metadata,
    counterfactual_registry_metadata_json_bytes,
)
from coldaisle.control.model.counterfactual_confidence import seal_counterfactual_artifact
from coldaisle.control.model.counterfactual_training import (
    CounterfactualTrainingSpec,
    load_training_dataset_artifact_v2,
    train_counterfactual_ridge,
)
from coldaisle.control.model.dataset import DatasetExampleV2, split_temporally_v2
from coldaisle.control.model.thermal import MAX_ARTIFACT_BYTES
from coldaisle.control.model.training import split_sha256
from coldaisle.control.model_registry import (
    MODEL_REGISTRY_CONFIG_FILENAME,
    ArtifactLoadStatus,
    ArtifactMetadata,
    ArtifactRef,
    ModelCompatibility,
    ModelRegistry,
    ModelRegistryError,
    ModelRegistryLimits,
    RegistryCommitNotDurableError,
)
from coldaisle.control.schema import AuthorityStage
from coldaisle.dataset import open_output_root
from coldaisle.declared_changes import DeclaredChangesInput, parse_declared_changes
from coldaisle.ingest.calibration import Calibration
from coldaisle.metrics import MetricCatalog
from coldaisle.store import QualityRules
from coldaisle.training_entry import verify_training_dataset_v2
from coldaisle.training_output import (
    ARTIFACT_FILENAME,
    METADATA_FILENAME,
    REPORT_FILENAME,
    OutputExistsError,
    PublishedTrainingOutput,
    TrainingOutputError,
    TrainingOutputIncomplete,
    UntrustedPathError,
    VerifiedTrainingOutput,
    check_trusted_parent,
    output_differences,
    path_names_directory,
    publish_training_output,
    read_training_output,
)

LOGGER = logging.getLogger("coldaisle.train")

EXIT_OK = 0
EXIT_FAILED = 1

MAX_TRAINING_CONFIG_BYTES = 1024 * 1024
"""設定の入力（6つに共通）を読む上限。

運用の閾値ではなくメモリを守る構造上の上限（0150 §5 の 12）。
"""

AUTHORITY_COMPATIBILITY: tuple[AuthorityStage, ...] = (AuthorityStage.SHADOW,)
"""最初の版は ``(shadow,)`` 固定。引数で変えられない（0150 §2.2）。"""

REPORT_SCHEMA_VERSION = 1
FAN_AXES = ("fan.front", "fan.rear", "fan.top")

# O_NONBLOCK: 書き手の無い FIFO を開いて止まらない（regular file の読み込みには効かない）
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_READ_CHUNK_BYTES = 64 * 1024
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

_TRAINING_ERRORS = (
    ValueError,
    TypeError,
    OSError,
    ModelRegistryError,
    yaml.YAMLError,
    RecursionError,
)
"""検査・学習の失敗として終了コード 1 にする例外（握りつぶさず、理由をログに出す）。"""


class TrainingStopped(Exception):
    """1つの段で止まった。``stage`` と ``fields`` を構造化ログに出す。"""

    def __init__(self, message: str, *, stage: str, fields: Mapping[str, object]) -> None:
        super().__init__(message)
        self.stage = stage
        self.fields = dict(fields)


# ---------------------------------------------------------------- 学習の選択（--training-spec）


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class TrainingSpecFile(_Strict):
    """``--training-spec`` の YAML（schema v1）。**どれも本番の既定値を置かない**（0150 §2.3）。"""

    schema_version: Literal[1]
    validation_start_ms: int = Field(ge=0)
    test_start_ms: int = Field(ge=0)
    ridge_lambda: float = Field(gt=0.0, allow_inf_nan=False)
    support_axes: tuple[SupportAxis, ...] = Field(min_length=1)
    residual_scale_floor: float = Field(gt=0.0, allow_inf_nan=False)

    @model_validator(mode="after")
    def _boundaries_and_fan_axes(self) -> TrainingSpecFile:
        if self.validation_start_ms >= self.test_start_ms:
            raise ValueError("validation_start_ms は test_start_ms より前にする")
        sources = {axis.source for axis in self.support_axes}
        missing = [axis for axis in FAN_AXES if axis not in sources]
        if missing:
            raise ValueError(f"support_axes に全 zone の fan 軸が要る（欠け: {missing}）")
        return self

    def profile_spec(self) -> ConfidenceProfileSpec:
        """同梱 Profile v2 の選択。"""
        return ConfidenceProfileSpec(
            support_axes=self.support_axes, residual_scale_floor=self.residual_scale_floor
        )


def _tuples(value: object) -> object:
    """YAML の list を tuple にする（strict な型は list を tuple として受けない）。"""
    if isinstance(value, list):
        return tuple(_tuples(item) for item in value)
    if isinstance(value, dict):
        return {key: _tuples(item) for key, item in value.items()}
    return value


# ---------------------------------------------------------------- 設定の1回の読み


def read_config_bytes(path: Path, *, label: str) -> bytes:
    """上限＋1 byte まで**だけ**読む。symlink をたどらず regular file だけ（0150 §2.7）。"""
    try:
        fd = os.open(path, _READ_FLAGS)
    except OSError as error:
        raise ValueError(f"{label} を開けない: {path}: {error}") from error
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"{label} が regular file ではない: {path}")
        chunks: list[bytes] = []
        total = 0
        while total <= MAX_TRAINING_CONFIG_BYTES:
            chunk = os.read(fd, min(_READ_CHUNK_BYTES, MAX_TRAINING_CONFIG_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
    finally:
        os.close(fd)
    if total > MAX_TRAINING_CONFIG_BYTES:
        raise ValueError(f"{label} が上限 {MAX_TRAINING_CONFIG_BYTES} byte を超える: {path}")
    return b"".join(chunks)


def _yaml_mapping(payload: bytes, *, label: str) -> dict[str, Any]:
    try:
        loaded: object = yaml.safe_load(payload.decode("utf-8"))
    except RecursionError as error:
        raise ValueError(f"{label} の入れ子が深すぎる") from error
    if not isinstance(loaded, dict):
        raise ValueError(f"{label} が辞書ではない")
    return loaded


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class TrainingInputs:
    """1回だけ読んだ設定と、同じ bytes の SHA-256。"""

    spec: TrainingSpecFile
    spec_sha256: str
    quality: QualityRules
    quality_sha256: str
    metric_catalog: MetricCatalog
    metrics_sha256: str
    declared: DeclaredChangesInput
    calibration: Calibration
    calibration_sha256: str
    registry_limits: ModelRegistryLimits | None


def read_inputs(args: argparse.Namespace) -> TrainingInputs:
    """6つの設定を上限で切って1回だけ読み、その bytes から parse して hash する。"""
    spec_bytes = read_config_bytes(args.training_spec, label="--training-spec")
    spec = TrainingSpecFile.model_validate(
        _tuples(_yaml_mapping(spec_bytes, label="--training-spec"))
    )
    quality_bytes = read_config_bytes(args.quality_config, label="--quality-config")
    quality = QualityRules.model_validate(_yaml_mapping(quality_bytes, label="--quality-config"))
    metrics_bytes = read_config_bytes(args.metrics_config, label="--metrics-config")
    try:
        catalog, metrics_sha256 = MetricCatalog.from_yaml_bytes_with_sha256(
            metrics_bytes, origin=str(args.metrics_config)
        )
    except RecursionError as error:
        raise ValueError("--metrics-config の入れ子が深すぎる") from error
    declared_bytes = read_config_bytes(args.declared_changes, label="--declared-changes")
    try:
        declared = parse_declared_changes(declared_bytes, origin=args.declared_changes.name)
    except RecursionError as error:
        raise ValueError("--declared-changes の入れ子が深すぎる") from error
    calibration_bytes = read_config_bytes(args.calibration, label="--calibration")
    try:
        calibration = Calibration.from_json_bytes(calibration_bytes, origin=str(args.calibration))
    except RecursionError as error:
        raise ValueError("--calibration の入れ子が深すぎる") from error
    limits: ModelRegistryLimits | None = None
    if args.apply:
        limits_bytes = read_config_bytes(
            args.registry_limits / MODEL_REGISTRY_CONFIG_FILENAME, label="--registry-limits"
        )
        limits = ModelRegistryLimits.model_validate(
            _yaml_mapping(limits_bytes, label="--registry-limits")
        )
    return TrainingInputs(
        spec=spec,
        spec_sha256=_sha256(spec_bytes),
        quality=quality,
        quality_sha256=_sha256(quality_bytes),
        metric_catalog=catalog,
        metrics_sha256=metrics_sha256,
        declared=declared,
        calibration=calibration,
        calibration_sha256=_sha256(calibration_bytes),
        registry_limits=limits,
    )


# ---------------------------------------------------------------- 検査と学習（0150 §2.4）


@dataclass(frozen=True, slots=True)
class TrainedOutput:
    """``--out`` に書く3つの bytes と、登録の後の検査に使う値。"""

    files: dict[str, bytes]
    model_id: str
    model_version: str


def _window(examples: tuple[DatasetExampleV2, ...]) -> dict[str, object]:
    if not examples:
        return {"count": 0, "start_ms": None, "end_ms": None}
    return {
        "count": len(examples),
        "start_ms": min(example.history_start_ms for example in examples),
        "end_ms": max(example.label_end_ms for example in examples),
    }


def _reject_declared_calibration_after(
    changes: tuple[DeclaredChange, ...], period_end_ms: int
) -> None:
    after = [
        change.ts_ms
        for change in changes
        if change.kind is ChangeKind.CALIBRATION_CHANGED and change.ts_ms > period_end_ms
    ]
    if after:
        raise ValueError(
            f"dataset の期間の終わり（{period_end_ms}）より後に宣言された較正の変更がある: {after}"
            "（決定記録 0150 §2.4 の 3）"
        )


def train_artifact(args: argparse.Namespace, inputs: TrainingInputs) -> TrainedOutput:
    """0150 §2.4 の 1〜7。公開の前に1つでも外れれば例外（何も書かない）。"""
    # 1. Dataset の読み込みと公開物への束縛を1回の読みで
    source = load_training_dataset_artifact_v2(args.dataset)
    dataset = source.dataset
    if not dataset.examples:
        raise ValueError("example の無い Dataset v2 では学習しない")
    # 2. 元の入力・本番の DB との照合と作り直し
    history = verify_training_dataset_v2(
        dataset,
        production_db=args.calibration_history_db,
        replay_path=args.replay_path,
        declared_changes=inputs.declared.changes,
        quality_rules=inputs.quality,
    )
    # 3. 較正の記録との照合（宣言の calibration_changed も期間の後なら拒否）
    period_end_ms = max(example.label_end_ms for example in dataset.examples)
    activation = verify_training_calibration(
        history,
        period_end_ms=period_end_ms,
        calibration_offsets_c=inputs.calibration.offsets_c,
    )
    _reject_declared_calibration_after(inputs.declared.changes, period_end_ms)
    # 4. split
    split = split_temporally_v2(
        dataset.examples,
        validation_start_ms=inputs.spec.validation_start_ms,
        test_start_ms=inputs.spec.test_start_ms,
    )
    # 5. train だけで学習し、同梱 Profile は seal の中で1回だけ作る
    #    （範囲は train・residual は validation）
    trained = train_counterfactual_ridge(
        source,
        split,
        CounterfactualTrainingSpec(
            model_id=args.model_id,
            model_version=args.model_version,
            created_at=args.created_at,
            ridge_lambda=inputs.spec.ridge_lambda,
            authority_compatibility=AUTHORITY_COMPATIBILITY,
            calibration_offsets_c=dict(inputs.calibration.offsets_c),
            code_commit=args.code_commit,
        ),
        metric_catalog=inputs.metric_catalog,
    )
    artifact = seal_counterfactual_artifact(trained, source, split, inputs.spec.profile_spec())
    # 6. 登録する bytes と metadata
    artifact_bytes = canonical_counterfactual_artifact_bytes(artifact)
    metadata = counterfactual_registry_metadata(artifact, artifact_bytes)
    metadata_bytes = counterfactual_registry_metadata_json_bytes(metadata)
    # 7. 読み込み側の検査を作った bytes に通す（L9 の較正の digest を含む）
    reread = CounterfactualThermalModelArtifact.model_validate_json(artifact_bytes)
    check_artifact_contents(
        reread,
        metric_catalog=inputs.metric_catalog,
        calibration=RuntimeCalibration.available(inputs.calibration.offsets_c),
        check_calibration=True,
    )
    registry_metadata = ArtifactMetadata.model_validate_json(metadata_bytes)
    if registry_metadata.sha256 != _sha256(artifact_bytes):
        raise ValueError("metadata の sha256 が artifact の bytes と一致しない")

    run = dataset.manifest.source_runs[0]
    bindings = dataset.manifest.replay_bindings or ()
    report: dict[str, object] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "dataset": {
            "alias": source.artifact_id,
            "manifest_sha256": source.manifest_sha256,
            "examples_sha256": source.examples_sha256,
            "excluded": dataset.manifest.excluded.model_dump(mode="json"),
        },
        "split": {
            "sha256": split_sha256(split),
            "validation_start_ms": inputs.spec.validation_start_ms,
            "test_start_ms": inputs.spec.test_start_ms,
            "train": _window(split.train),
            "validation": _window(split.validation),
            "test": _window(split.test),
            "purged": {"count": len(split.purged)},
        },
        "inputs": {
            "training_spec_sha256": inputs.spec_sha256,
            "declared_changes_sha256": inputs.declared.file_sha256,
            "quality_config_sha256": inputs.quality_sha256,
            "metrics_config_sha256": inputs.metrics_sha256,
            "calibration_sha256": inputs.calibration_sha256,
        },
        "calibration": {
            "offsets_sha256": activation.offsets_sha256,
            "activation_ts_ms": activation.ts_ms,
            "activation_row_sha256": activation.row_sha256,
        },
        "replay": {
            "source_sha256": run.source_sha256,
            "export_binding_sha256": [binding.export_binding_sha256 for binding in bindings],
        },
        "artifact": {
            "model_id": args.model_id,
            "model_version": args.model_version,
            "sha256": _sha256(artifact_bytes),
            "confidence_profile_sha256": reread.manifest.confidence_profile_sha256,
            "authority_compatibility": [stage.value for stage in AUTHORITY_COMPATIBILITY],
        },
    }
    report_bytes = (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    return TrainedOutput(
        files={
            REPORT_FILENAME: report_bytes,
            ARTIFACT_FILENAME: artifact_bytes,
            METADATA_FILENAME: metadata_bytes,
        },
        model_id=args.model_id,
        model_version=args.model_version,
    )


# ---------------------------------------------------------------- 入口


def _absolute(path: Path) -> Path:
    if ".." in path.parts:
        raise TrainingStopped(
            "--out に '..' は使えない", stage="untrusted_path", fields={"out": str(path)}
        )
    absolute = path if path.is_absolute() else Path.cwd() / path
    return Path(os.path.normpath(absolute))


def _require_apply_arguments(args: argparse.Namespace) -> None:
    registry_flags = {
        "--registry-root": args.registry_root,
        "--registry-limits": args.registry_limits,
        "--actor": args.actor,
        "--reason": args.reason,
    }
    if args.apply:
        missing = [name for name, value in registry_flags.items() if value is None]
        if args.code_commit is None:
            missing.append("--code-commit")
        if missing:
            raise TrainingStopped(
                f"--apply には {', '.join(missing)} が要る",
                stage="before_publish",
                fields={"missing": missing},
            )
    else:
        given = [name for name, value in registry_flags.items() if value is not None]
        if given:
            # 書かない実行に Registry の引数を受けると、登録したと取り違える
            raise TrainingStopped(
                f"{', '.join(given)} は --apply のときだけ渡す",
                stage="before_publish",
                fields={"unexpected": given},
            )


def _refuse_existing_out(parent_fd: int, out: Path) -> None:
    """``--out`` が既にあれば学習の前に拒否し、有効な完成の印があるかをログに出す。"""
    try:
        os.stat(out.name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    valid = False
    try:
        fd = os.open(out.name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except OSError:
        fd = -1
    if fd >= 0:
        try:
            read_training_output(
                fd,
                max_artifact_bytes=MAX_ARTIFACT_BYTES,
                max_other_bytes=MAX_TRAINING_CONFIG_BYTES,
            )
            valid = True
        except TrainingOutputIncomplete:
            valid = False
        finally:
            os.close(fd)
    raise TrainingStopped(
        f"--out が既にある（上書きしない）: {out}",
        stage="before_publish",
        fields={"out": str(out), "out_exists": True, "complete_marker_valid": valid},
    )


def _verify_contents(
    published: PublishedTrainingOutput,
    *,
    expected: Mapping[str, bytes],
    max_artifact_bytes: int,
    max_other_bytes: int,
) -> tuple[VerifiedTrainingOutput | None, list[dict[str, str]], str | None]:
    """開いた ``--out`` の fd を確かめる関数に通し、期待した bytes との違いを返す。"""
    try:
        verified = read_training_output(
            published.fd,
            max_artifact_bytes=max_artifact_bytes,
            max_other_bytes=max_other_bytes,
        )
    except TrainingOutputIncomplete as error:
        return None, [], str(error)
    return verified, output_differences(verified, expected, published.marker), None


def _log_stopped(stop: TrainingStopped) -> None:
    LOGGER.error(
        str(stop),
        extra={logs.FIELDS_KEY: {"stage": stop.stage, "stages": [stop.stage], **stop.fields}},
    )


def run(args: argparse.Namespace) -> int:
    """1回の学習。成功で 0、失敗で 1（段は構造化ログ）。"""
    out = _absolute(args.out)
    _require_apply_arguments(args)
    try:
        inputs = read_inputs(args)
    except _TRAINING_ERRORS as error:
        raise TrainingStopped(
            f"設定を読めない: {error}", stage="before_publish", fields={"error": str(error)}
        ) from error
    try:
        check_trusted_parent(out)
        parent_fd = open_output_root(out.parent, create=False)
    except UntrustedPathError as error:
        raise TrainingStopped(str(error), stage=error.stage, fields=error.fields()) from error
    except _TRAINING_ERRORS as error:
        raise TrainingStopped(
            f"--out の親を開けない: {error}",
            stage="before_publish",
            fields={"out": str(out), "error": str(error)},
        ) from error
    try:
        _refuse_existing_out(parent_fd, out)
        try:
            trained = train_artifact(args, inputs)
        except _TRAINING_ERRORS as error:
            raise TrainingStopped(
                f"学習の前の検査か学習に失敗した: {error}",
                stage="before_publish",
                fields={"error": str(error), "error_type": type(error).__name__},
            ) from error
        try:
            published = publish_training_output(parent_fd, out, trained.files)
        except OutputExistsError as error:
            raise TrainingStopped(
                str(error), stage="before_publish", fields={"out": str(out), "out_exists": True}
            ) from error
        except OSError as error:
            # --out を作れなかった（mkdir の EEXIST 以外の失敗）。書き手は何も作っていない
            raise TrainingStopped(
                f"--out を作れない: {error}",
                stage="before_publish",
                fields={"out": str(out), "error": str(error)},
            ) from error
        except TrainingOutputError as error:
            cause = error.__cause__
            raise TrainingStopped(
                str(error),
                stage=error.stage,
                fields={
                    "out": str(out),
                    **error.fields(),
                    "cause": None if cause is None else str(cause),
                },
            ) from error
    finally:
        os.close(parent_fd)
    try:
        if not args.apply:
            return _finish_without_apply(published, trained)
        assert inputs.registry_limits is not None
        return _apply(args, published, trained, inputs)
    finally:
        published.close()


def _finish_without_apply(published: PublishedTrainingOutput, trained: TrainedOutput) -> int:
    sizes = [len(published.marker), *(len(trained.files[name]) for name in trained.files)]
    _verified, differences, problem = _verify_contents(
        published,
        expected=trained.files,
        max_artifact_bytes=len(trained.files[ARTIFACT_FILENAME]),
        max_other_bytes=max(sizes),
    )
    if problem is not None or differences:
        raise TrainingStopped(
            "--out の中身が書いた bytes と違う（成功を報告しない）",
            stage="out_modified",
            fields={"out": str(published.path), "differences": differences, "problem": problem},
        )
    LOGGER.info(
        "学習の出力を公開した（Registry は書いていない）",
        extra={
            logs.FIELDS_KEY: {
                "out": str(published.path),
                "artifact_sha256": _sha256(trained.files[ARTIFACT_FILENAME]),
                "model_id": trained.model_id,
                "model_version": trained.model_version,
            }
        },
    )
    return EXIT_OK


def _apply(
    args: argparse.Namespace,
    published: PublishedTrainingOutput,
    trained: TrainedOutput,
    inputs: TrainingInputs,
) -> int:
    """``--out`` の公開の後に candidate を登録する。

    0150 §2.5「--apply の Registry 側の書き込み」。
    """
    limits = inputs.registry_limits
    assert limits is not None
    out = published.path
    # 登録の直前の照合（名前・中身）。外れたら登録しない
    try:
        check = path_names_directory(out, published.identity)
    except UntrustedPathError as error:
        raise TrainingStopped(
            str(error), stage=error.stage, fields={"out": str(out), **error.fields()}
        ) from error
    if not check.matches:
        raise TrainingStopped(
            "登録の直前に --out の名前が書き手のディレクトリを指していない（登録しない）",
            stage="out_replaced",
            fields={
                "out": str(out),
                "expected_identity": list(published.identity),
                "actual_identity": None if check.actual is None else list(check.actual),
                "mismatch_reason": check.reason,
            },
        )
    verified, differences, problem = _verify_contents(
        published,
        expected=trained.files,
        max_artifact_bytes=limits.max_artifact_bytes,
        max_other_bytes=limits.max_snapshot_bytes,
    )
    if verified is None or differences:
        raise TrainingStopped(
            "登録の直前に --out の中身が書いた bytes と違う（登録しない）",
            stage="out_modified",
            fields={"out": str(out), "differences": differences, "problem": problem},
        )
    # 登録するのは確かめる関数が返した bytes（メモリ上の bytes ではない）
    try:
        metadata = ArtifactMetadata.model_validate_json(verified.metadata)
    except ValidationError as error:
        raise TrainingStopped(
            f"metadata を検証できない: {error}", stage="register", fields={"out": str(out)}
        ) from error
    registry = ModelRegistry(args.registry_root, limits=limits)
    stages: list[str] = []
    fields: dict[str, object] = {"out": str(out)}
    try:
        ref = registry.register_candidate(
            metadata, verified.artifact, actor=args.actor, reason=args.reason
        )
    except RegistryCommitNotDurableError as error:
        ref = error.ref
        stages.append("register_durability")
        fields["register_durability"] = (
            f"{error}（candidate は見えている。もう一度 coldaisle-train --apply で登録し直さず、"
            "coldaisle-registry status で確かめる）"
        )
    except (ModelRegistryError, OSError) as error:
        raise TrainingStopped(
            f"Registry の登録に失敗した: {error}",
            stage="register",
            fields={
                "out": str(out),
                "error": str(error),
                "recovery": f"coldaisle-registry register --training-output {out}",
            },
        ) from error
    fields["artifact"] = ref.key
    stages.extend(_after_register(published, verified, out, fields))
    stages.extend(_post_register_check(registry, ref, inputs, fields))
    if stages:
        LOGGER.error(
            "candidate の登録の後の検査で問題がある（成功を報告しない。CLI は retire しない）",
            extra={logs.FIELDS_KEY: {"stage": stages[0], "stages": stages, **fields}},
        )
        return EXIT_FAILED
    LOGGER.info(
        "candidate を登録した（production は動かない）",
        extra={
            logs.FIELDS_KEY: {
                "out": str(out),
                "artifact": ref.key,
                "actor": args.actor,
                "reason": args.reason,
            }
        },
    )
    return EXIT_OK


def _after_register(
    published: PublishedTrainingOutput,
    registered: VerifiedTrainingOutput,
    out: Path,
    fields: dict[str, object],
) -> list[str]:
    """登録の後の path の照合と中身の再検証（どちらも外れたら段を返す）。"""
    stages: list[str] = []
    try:
        check = path_names_directory(out, published.identity)
    except UntrustedPathError as error:
        stages.append("out_replaced_after_register")
        fields["out_replaced_after_register"] = {"untrusted": True, **error.fields()}
    else:
        if not check.matches:
            stages.append("out_replaced_after_register")
            fields["out_replaced_after_register"] = {
                "expected_identity": list(published.identity),
                "actual_identity": None if check.actual is None else list(check.actual),
                "mismatch_reason": check.reason,
            }
    expected = registered.file_bytes()
    sizes = [len(registered.marker), *(len(value) for value in expected.values())]
    try:
        again = read_training_output(
            published.fd,
            max_artifact_bytes=len(registered.artifact),
            max_other_bytes=max(sizes),
        )
    except TrainingOutputIncomplete as error:
        stages.append("out_modified_after_register")
        fields["out_modified_after_register"] = {"problem": str(error)}
    else:
        differences = output_differences(again, expected, registered.marker)
        if differences:
            stages.append("out_modified_after_register")
            fields["out_modified_after_register"] = {"differences": differences}
    return stages


def _post_register_check(
    registry: ModelRegistry,
    ref: ArtifactRef,
    inputs: TrainingInputs,
    fields: dict[str, object],
) -> list[str]:
    """登録した bytes を ``load_version`` で読み直し、L1〜L12 を通す（0150 §2.4 の 7）。"""
    compatibility = ModelCompatibility(
        feature_schema_version=FEATURE_SCHEMA_V2_VERSION,
        target_schema_version=TARGET_SCHEMA_V2_VERSION,
        authority_stage=AuthorityStage.SHADOW,
    )
    try:
        result = registry.load_version(ref, compatibility)
        if result.status is not ArtifactLoadStatus.LOADED or result.artifact is None:
            raise ValueError(f"load_version が読めない: {result.status.value}: {result.detail}")
        RegistryCounterfactualThermalModel.from_verified_artifact(
            result.artifact,
            metric_catalog=inputs.metric_catalog,
            calibration=RuntimeCalibration.available(inputs.calibration.offsets_c),
        )
    except _TRAINING_ERRORS as error:
        fields["post_register_check"] = (
            f"{error}（coldaisle-registry retire で candidate を退ける。CLI は retire しない）"
        )
        return ["post_register_check"]
    return []


def build_parser() -> argparse.ArgumentParser:
    """引数（0150 §2.3。すべて必須・既定値を置かない）。"""
    parser = argparse.ArgumentParser(
        prog="coldaisle-train",
        description=(
            "Thermal Dataset v2 から反実仮想 Thermal Model artifact v2 を作る（#84 / 0150）"
        ),
    )
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--dataset", type=Path, required=True, help="dataset-<32 hex> の公開物")
    parser.add_argument("--replay-path", type=Path, required=True, help="元の日次 CSV と manifest")
    parser.add_argument(
        "--calibration-history-db", type=Path, required=True, help="本番の DB（読み取り専用）"
    )
    parser.add_argument(
        "--declared-changes", type=Path, required=True, help="dataset と同じ宣言の YAML"
    )
    parser.add_argument("--quality-config", type=Path, required=True, help="品質の設定")
    parser.add_argument(
        "--calibration", type=Path, required=True, help="学習期間に効いていた calibration.json"
    )
    parser.add_argument("--metrics-config", type=Path, required=True, help="config/metrics.yaml")
    parser.add_argument("--training-spec", type=Path, required=True, help="学習の選択の YAML")
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--model-version", required=True)
    parser.add_argument("--created-at", required=True, help="RFC 3339（壁時計を読まない）")
    parser.add_argument("--out", type=Path, required=True, help="出力先（まだ無いこと）")
    parser.add_argument("--code-commit", default=None, help="--apply のときは必須")
    parser.add_argument(
        "--apply", action="store_true", help="Model Registry へ candidate として登録する"
    )
    parser.add_argument("--registry-root", type=Path, default=None)
    parser.add_argument(
        "--registry-limits", type=Path, default=None, help="model-registry.yaml のあるディレクトリ"
    )
    parser.add_argument("--actor", default=None)
    parser.add_argument("--reason", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    """1回の学習を実行し、終了コードを返す。"""
    args = build_parser().parse_args(argv)
    logs.configure(args.log_level)
    try:
        return run(args)
    except TrainingStopped as stop:
        _log_stopped(stop)
        return EXIT_FAILED


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
