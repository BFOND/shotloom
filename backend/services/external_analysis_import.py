"""Persist external CLIProxy static-frame analysis without calling a model.

ShotLoom does not contact CLIProxy, Qwen, or DashScope from this path. The
caller has already sampled still frames elsewhere. This module checks that the
payload names the owner's current shots and config revision, then stores the
analysis in the existing JSON columns.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from database import Shot, Video, VideoAnalysis, VideoAnalysisConfig
from logger import app_logger
from services.analysis_config import activate_video_config, config_hash, default_config, validate_config

EXTERNAL_SOURCE = "external_cliproxy_static_frame"
ATTRIBUTION = (
    "外部 CLIProxy 静帧分析。不是 ShotLoom 调用通义千问（Qwen）或 DashScope 生成的。"
    "覆盖为非穷尽采样。静帧不能证明摄像机运动。未采样音频，口播、音乐与环境声为未知。"
)
LIMITATIONS = [
    "static_frames_cannot_prove_camera_movement",
    "audio_not_sampled",
    "coverage_is_non_exhaustive",
]
CAMERA_MOVEMENT_KEYS = {"camera_movement", "camera_move", "movement"}
AUDIO_FIELD_KEYS = {
    "transcript", "transcript_segments", "language", "speech_emotion",
    "dialogue", "speaker", "music", "ambient_sound", "sound_type",
    "speaker_emotion", "transcript_timestamps", "audiovisual_sync",
    "audio_narrative_role", "subtitle_speech_consistency",
    "speech_visual_coordination", "speech_subtitle_strategy", "audio_arc",
    "sound_presence", "heard_sound",
}
PUBLIC_PROVENANCE_KEYS = (
    "source", "provider", "method", "coverage", "audio", "camera_movement",
    "attribution", "config_revision", "sampled_timestamps", "external_model",
    "imported_at", "limitations",
)
_TIME_TOLERANCE = 0.001
_STATEMENT_KEYS = {"status", "value", "evidence", "frame_time"}


class ImportRejected(Exception):
    def __init__(self, status_code: int, code: str, message: str, **extra: Any):
        self.status_code = status_code
        self.detail = {"code": code, "message": message, **extra}
        super().__init__(message)


def provenance_from_stored(shot_analyses: list[Any], continuity: Any, segments: Any) -> dict[str, Any] | None:
    block = _stored_block(continuity) or _stored_block(segments)
    if block is None:
        for analysis in shot_analyses:
            block = _stored_block(analysis)
            if block is not None:
                break
    if block is None:
        return None
    return {key: block.get(key) for key in PUBLIC_PROVENANCE_KEYS}


def import_external_analysis(video: Video, payload: dict[str, Any], db: Session) -> dict[str, Any]:
    shots = db.query(Shot).filter(Shot.video_id == video.id).order_by(Shot.index).all()
    if not shots:
        raise ImportRejected(400, "NO_SHOTS", "请先检测镜头，再导入外部分析")
    if video.current_task_id or video.status == "analyzing":
        raise ImportRejected(409, "ANALYSIS_ALREADY_RUNNING", "视频正在分析，不能导入外部结果")

    config_row = _config_row(video.id, db)
    requested_revision = payload.get("config_revision")
    if isinstance(requested_revision, bool) or not isinstance(requested_revision, int):
        raise ImportRejected(400, "CONFIG_REVISION_REQUIRED", "config_revision 必须是整数")
    if requested_revision != config_row.draft_revision:
        raise ImportRejected(
            409,
            "CONFIG_REVISION_CONFLICT",
            "配置版本与当前草稿不一致",
            current_revision=config_row.draft_revision,
        )
    try:
        config = validate_config(config_row.draft_config)
    except (TypeError, ValueError) as exc:
        raise ImportRejected(422, "ANALYSIS_CONFIG_INVALID", str(exc)) from exc

    provenance = payload.get("provenance") or {}
    normalized = _validate_payload(shots, config, payload, provenance)
    if _has_results(video.id, db) and not payload.get("replace_existing"):
        raise ImportRejected(409, "RESULTS_EXIST", "已有分析结果。确认覆盖时请设置 replace_existing=true")

    activate_video_config(config_row, db)
    imported_at = datetime.now(timezone.utc).isoformat()
    union_timestamps = _union_timestamps(item["sampled_timestamps"] for item in normalized["shots"])
    base_block = _base_block(provenance, requested_revision, imported_at, union_timestamps)

    for item in normalized["shots"]:
        shot = item["shot"]
        shot.analysis = _with_marker(item["analysis"], item["epistemic"], {**base_block, "sampled_timestamps": item["sampled_timestamps"]})

    overall = _with_marker(
        normalized["overall"],
        normalized["overall_epistemic"],
        base_block,
    )
    segment_rows = []
    for item in normalized["segments"]:
        stored = _with_marker(
            item["analysis"],
            item["epistemic"],
            {**base_block, "sampled_timestamps": item["sampled_timestamps"]},
        )
        stored["segment_index"] = item["segment_index"]
        stored["shot_indices"] = item["shot_indices"]
        segment_rows.append(stored)
    segments_report = {
        "strategy": EXTERNAL_SOURCE,
        "reason": "Imported non-exhaustive CLIProxy static-frame analysis. Not generated by Qwen or DashScope.",
        "shot_count": len(shots),
        "coverage": "non_exhaustive",
        "audio": "unknown",
        "external_import": base_block,
        "segments": segment_rows,
    }

    existing = db.query(VideoAnalysis).filter(VideoAnalysis.video_id == video.id).first()
    if existing is None:
        existing = VideoAnalysis(video_id=video.id)
        db.add(existing)
    existing.continuity_report = overall
    existing.segments_report = segments_report
    existing.rhythm_report = None
    video.status = "completed"
    video.error_msg = None
    app_logger.info(
        "导入外部静帧分析: video_id=%s shots=%s segments=%s revision=%s",
        video.id,
        len(shots),
        len(segment_rows),
        requested_revision,
    )
    return {
        "ok": True,
        "video_id": video.id,
        "shot_count": len(shots),
        "segment_count": len(segment_rows),
        "config_revision": config_row.draft_revision,
        "analysis_provenance": provenance_from_stored([], overall, segments_report),
    }


def _config_row(video_id: int, db: Session) -> VideoAnalysisConfig:
    row = db.query(VideoAnalysisConfig).filter(VideoAnalysisConfig.video_id == video_id).first()
    if row is not None:
        return row
    config = validate_config(default_config())
    row = VideoAnalysisConfig(video_id=video_id, draft_revision=1, draft_hash=config_hash(config))
    row.draft_config = config
    db.add(row)
    return row


def _has_results(video_id: int, db: Session) -> bool:
    if db.query(Shot.id).filter(Shot.video_id == video_id, Shot._analysis.is_not(None)).first():
        return True
    return db.query(VideoAnalysis.id).filter(VideoAnalysis.video_id == video_id).first() is not None


def _stored_block(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    block = value.get("external_import")
    if isinstance(block, dict) and block.get("source") == EXTERNAL_SOURCE:
        return block
    return None


def _base_block(provenance: dict[str, Any], revision: int, imported_at: str, timestamps: list[float]) -> dict[str, Any]:
    return {
        "source": EXTERNAL_SOURCE,
        "provider": "cliproxy",
        "method": "static_frame",
        "coverage": "non_exhaustive",
        "audio": "unknown",
        "camera_movement": "not_observable_from_stills",
        "attribution": ATTRIBUTION,
        "limitations": list(LIMITATIONS),
        "config_revision": revision,
        "sampled_timestamps": timestamps,
        "external_model": provenance.get("external_model"),
        "imported_at": imported_at,
    }


def _with_marker(analysis: dict[str, Any], epistemic: dict[str, Any], block: dict[str, Any]) -> dict[str, Any]:
    stored = dict(analysis)
    stored["analysis_source"] = EXTERNAL_SOURCE
    stored["analysis_mode"] = "external_static_frame"
    stored["external_import"] = {**block, "epistemic": epistemic}
    return stored


def _validate_payload(shots: list[Shot], config: dict[str, Any], payload: dict[str, Any], provenance: dict[str, Any]) -> dict[str, Any]:
    if provenance.get("provider") != "cliproxy" or provenance.get("method") != "static_frame":
        raise ImportRejected(400, "SOURCE_NOT_STATIC_FRAME", "只接受 provider=cliproxy 且 method=static_frame 的外部静帧结果")
    if provenance.get("coverage") != "non_exhaustive":
        raise ImportRejected(400, "COVERAGE_MUST_BE_NON_EXHAUSTIVE", "静帧导入必须声明 coverage=non_exhaustive")
    if provenance.get("audio") != "absent":
        raise ImportRejected(400, "AUDIO_ABSENT_REQUIRED", "静帧导入必须声明 audio=absent")

    submitted = payload.get("shots")
    if not isinstance(submitted, list):
        raise ImportRejected(400, "SHOTS_REQUIRED", "shots 必须是数组")
    by_id = {shot.id: shot for shot in shots}
    if len(submitted) != len(by_id):
        raise ImportRejected(400, "SHOT_SET_MISMATCH", "必须一次提交当前视频的全部镜头，不能多也不能少", expected=len(by_id))

    seen_ids: set[int] = set()
    normalized_shots = []
    timestamps_by_index: dict[int, list[float]] = {}
    for item in submitted:
        if not isinstance(item, dict):
            raise ImportRejected(400, "SHOT_INVALID", "shots 中的每一项必须是对象")
        shot_id = item.get("id")
        if isinstance(shot_id, bool) or not isinstance(shot_id, int) or shot_id not in by_id:
            raise ImportRejected(400, "SHOT_ID_MISMATCH", "镜头 id 不属于这个视频", shot_id=shot_id)
        if shot_id in seen_ids:
            raise ImportRejected(400, "DUPLICATE_SHOT", "镜头 id 重复", shot_id=shot_id)
        seen_ids.add(shot_id)
        shot = by_id[shot_id]
        if item.get("index") != shot.index:
            raise ImportRejected(400, "SHOT_INDEX_MISMATCH", "镜头序号与记录不一致", shot_id=shot_id, expected_index=shot.index)
        start = _required_time(item.get("start_time"), "start_time", shot_id)
        end = _required_time(item.get("end_time"), "end_time", shot_id)
        if end <= start:
            raise ImportRejected(400, "SHOT_BOUNDARY_MISMATCH", "镜头结束时间必须大于开始时间", shot_id=shot_id)
        if abs(start - float(shot.start_time)) > _TIME_TOLERANCE or abs(end - float(shot.end_time)) > _TIME_TOLERANCE:
            raise ImportRejected(
                400,
                "SHOT_BOUNDARY_MISMATCH",
                "镜头边界与当前记录不一致",
                shot_id=shot_id,
                expected_start=shot.start_time,
                expected_end=shot.end_time,
            )
        sampled = _normalize_timestamps(item.get("sampled_timestamps"), start, end, shot_id)
        analysis, epistemic = _normalize_tree(
            (config.get("scopes") or {}).get("shot") or [],
            item.get("analysis"),
            sampled=sampled,
            owner=f"shot:{shot.index}",
        )
        _apply_computed((config.get("scopes") or {}).get("shot") or [], analysis, "", "shot", start, end, epistemic)
        normalized_shots.append({
            "shot": shot,
            "analysis": analysis,
            "epistemic": epistemic,
            "sampled_timestamps": sampled,
        })
        timestamps_by_index[shot.index] = sampled
    if seen_ids != set(by_id):
        raise ImportRejected(400, "SHOT_SET_MISMATCH", "必须一次提交当前视频的全部镜头，不能多也不能少")

    all_timestamps = _union_timestamps(timestamps_by_index.values())
    normalized_segments = _validate_segments(
        payload.get("segments"),
        shots,
        (config.get("scopes") or {}).get("segment") or [],
        timestamps_by_index,
    )
    overall, overall_epistemic = _normalize_tree(
        (config.get("scopes") or {}).get("overall") or [],
        payload.get("overall_analysis"),
        sampled=all_timestamps,
        owner="overall",
    )
    _apply_computed((config.get("scopes") or {}).get("overall") or [], overall, "", "overall", 0.0, 0.0, overall_epistemic)
    return {
        "shots": normalized_shots,
        "segments": normalized_segments,
        "overall": overall,
        "overall_epistemic": overall_epistemic,
    }


def _validate_segments(submitted: Any, shots: list[Shot], fields: list[dict[str, Any]], timestamps_by_index: dict[int, list[float]]) -> list[dict[str, Any]]:
    if not isinstance(submitted, list) or not submitted:
        raise ImportRejected(400, "SEGMENTS_REQUIRED", "segments 必须覆盖全部分镜头")
    expected = {shot.index for shot in shots}
    seen_segments: set[int] = set()
    covered: set[int] = set()
    normalized = []
    for item in submitted:
        if not isinstance(item, dict):
            raise ImportRejected(400, "SEGMENT_INVALID", "segments 中的每一项必须是对象")
        segment_index = item.get("segment_index")
        if isinstance(segment_index, bool) or not isinstance(segment_index, int):
            raise ImportRejected(400, "SEGMENT_INDEX_INVALID", "segment_index 必须是整数")
        if segment_index in seen_segments:
            raise ImportRejected(400, "DUPLICATE_SEGMENT", "段落序号重复", segment_index=segment_index)
        seen_segments.add(segment_index)
        raw_indices = item.get("shot_indices")
        if not isinstance(raw_indices, list) or not raw_indices:
            raise ImportRejected(400, "SEGMENT_SHOTS_INVALID", "段落必须列出镜头序号", segment_index=segment_index)
        shot_indices: list[int] = []
        for raw_index in raw_indices:
            if isinstance(raw_index, bool) or not isinstance(raw_index, int) or raw_index not in expected:
                raise ImportRejected(400, "SEGMENT_SHOTS_INVALID", "段落引用了不存在的镜头", segment_index=segment_index)
            if raw_index in covered or raw_index in shot_indices:
                raise ImportRejected(400, "SEGMENT_SHOT_OVERLAP", "每个镜头只能属于一个段落", shot_index=raw_index)
            shot_indices.append(raw_index)
        member_timestamps = _union_timestamps(timestamps_by_index[index] for index in shot_indices)
        sampled = _normalize_timestamp_list(item.get("sampled_timestamps"), f"segment:{segment_index}")
        for stamp in sampled:
            if not _matches(stamp, member_timestamps):
                raise ImportRejected(400, "SEGMENT_TIMESTAMP_NOT_SAMPLED", "段落采样时间必须来自所属镜头已经采样的静帧", segment_index=segment_index)
            if not any(_within_shot(stamp, shots, index) for index in shot_indices):
                raise ImportRejected(400, "SEGMENT_TIMESTAMP_OUTSIDE_SHOT", "段落采样时间超出所属镜头边界", segment_index=segment_index)
        analysis, epistemic = _normalize_tree(fields, item.get("analysis"), sampled=sampled, owner=f"segment:{segment_index}")
        _apply_computed(fields, analysis, "", "segment", 0.0, 0.0, epistemic)
        covered.update(shot_indices)
        normalized.append({
            "segment_index": segment_index,
            "shot_indices": sorted(shot_indices),
            "sampled_timestamps": sampled,
            "analysis": analysis,
            "epistemic": epistemic,
        })
    if covered != expected or seen_segments != set(range(len(submitted))):
        raise ImportRejected(400, "SEGMENT_COVERAGE_MISMATCH", "段落必须从 0 连续编号，并且不重不漏地覆盖全部分镜头")
    return normalized


def _within_shot(stamp: float, shots: list[Shot], index: int) -> bool:
    shot = next(item for item in shots if item.index == index)
    return float(shot.start_time) - _TIME_TOLERANCE <= stamp <= float(shot.end_time) + _TIME_TOLERANCE


def _normalize_tree(fields: list[dict[str, Any]], payload: Any, sampled: list[float], owner: str) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ImportRejected(400, "ANALYSIS_MUST_BE_OBJECT", "分析内容必须是对象", owner=owner)
    output, epistemic = _normalize_fields(fields, payload, "", sampled, owner)
    return output, epistemic


def _normalize_fields(fields: list[dict[str, Any]], payload: dict[str, Any], prefix: str, sampled: list[float], owner: str) -> tuple[dict[str, Any], dict[str, Any]]:
    allowed = {field["key"]: field for field in fields}
    extra = sorted(set(payload) - set(allowed))
    if extra:
        raise ImportRejected(400, "UNKNOWN_FIELD", "分析里有当前配置不存在的字段", owner=owner, fields=extra)
    output: dict[str, Any] = {}
    epistemic: dict[str, Any] = {}
    for key, field in allowed.items():
        path = f"{prefix}.{key}" if prefix else key
        children = field.get("fields") or []
        if children:
            if key not in payload:
                if _all_leaves_computed(children):
                    child_payload = {}
                else:
                    raise ImportRejected(400, "MISSING_FIELD", "缺少分析字段", owner=owner, field=path)
            else:
                child_payload = payload[key]
                if not isinstance(child_payload, dict):
                    raise ImportRejected(400, "FIELD_MUST_BE_OBJECT", "分组字段必须是对象", owner=owner, field=path)
            child_output, child_epistemic = _normalize_fields(children, child_payload, path, sampled, owner)
            if child_output:
                output[key] = child_output
            epistemic.update(child_epistemic)
            continue
        if field.get("source", "vision") == "computed":
            if key in payload:
                raise ImportRejected(400, "COMPUTED_FIELD_SERVER_OWNED", "系统计算字段由服务器填写，请不要提交", owner=owner, field=path)
            continue
        if key not in payload:
            raise ImportRejected(400, "MISSING_FIELD", "缺少分析字段", owner=owner, field=path)
        value, entry = _normalize_statement(payload[key], field, path, sampled, owner)
        output[key] = value
        epistemic[path] = entry
    return output, epistemic


def _normalize_statement(statement: Any, field: dict[str, Any], path: str, sampled: list[float], owner: str) -> tuple[Any, dict[str, Any]]:
    if not isinstance(statement, dict):
        raise ImportRejected(400, "EPISTEMIC_STATEMENT_REQUIRED", "每个叶子字段都要写明观察、推断或未知", owner=owner, field=path)
    extra = sorted(set(statement) - _STATEMENT_KEYS)
    if extra:
        raise ImportRejected(400, "UNKNOWN_STATEMENT_FIELD", "叶子字段包含不支持的键", owner=owner, field=path, fields=extra)
    status = statement.get("status")
    if status not in {"observation", "inference", "unknown"}:
        raise ImportRejected(400, "EPISTEMIC_STATUS_INVALID", "status 只能是 observation、inference 或 unknown", owner=owner, field=path)
    key = path.rsplit(".", 1)[-1]
    source = field.get("source", "vision")
    field_type = field.get("type", "string")
    if source == "asr" or key in AUDIO_FIELD_KEYS:
        if status != "unknown":
            raise ImportRejected(400, "AUDIO_UNKNOWN_REQUIRED", "没有音频，口播、音乐和环境声只能标为未知", owner=owner, field=path)
    if key in CAMERA_MOVEMENT_KEYS and status == "observation":
        raise ImportRejected(400, "CAMERA_MOVEMENT_NOT_OBSERVABLE", "静帧不能证明摄像机运动，运镜不能标为观察", owner=owner, field=path)

    evidence = statement.get("evidence")
    if evidence is not None and not isinstance(evidence, str):
        raise ImportRejected(400, "EVIDENCE_INVALID", "evidence 必须是字符串", owner=owner, field=path)
    evidence_text = (evidence or "").strip()
    if status in {"observation", "inference"}:
        if not evidence_text:
            raise ImportRejected(400, "EVIDENCE_REQUIRED", "观察和推断必须写证据", owner=owner, field=path)
        if len(evidence_text) > 1000:
            raise ImportRejected(400, "EVIDENCE_TOO_LONG", "evidence 不能超过 1000 字", owner=owner, field=path)
        if "frame_time" not in statement or statement.get("frame_time") is None:
            raise ImportRejected(400, "FRAME_TIME_REQUIRED", "观察和推断必须给出采样静帧时间", owner=owner, field=path)
        frame_time = _finite_float(statement.get("frame_time"), owner, path)
        if not _matches(frame_time, sampled):
            raise ImportRejected(400, "FRAME_TIME_NOT_SAMPLED", "frame_time 必须是该范围已经声明的采样时间", owner=owner, field=path)
        if key in CAMERA_MOVEMENT_KEYS:
            note = "静帧不能证明摄像机运动，此项仅为推断。"
            if note not in evidence_text:
                evidence_text = f"{evidence_text} {note}".strip()
        return _coerce_value(statement.get("value"), field_type, owner, path), {
            "status": status,
            "evidence": evidence_text,
            "frame_time": round(frame_time, 3),
        }

    if statement.get("frame_time") is not None:
        raise ImportRejected(400, "UNKNOWN_HAS_FRAME_TIME", "未知项不能附带证明用的静帧时间", owner=owner, field=path)
    if not _is_empty_unknown(statement.get("value")):
        raise ImportRejected(400, "UNKNOWN_VALUE_MUST_BE_EMPTY", "未知项的 value 必须为空", owner=owner, field=path)
    if len(evidence_text) > 1000:
        raise ImportRejected(400, "EVIDENCE_TOO_LONG", "evidence 不能超过 1000 字", owner=owner, field=path)
    if not evidence_text:
        if key in CAMERA_MOVEMENT_KEYS:
            evidence_text = "静帧不能证明摄像机运动"
        elif source == "asr" or key in AUDIO_FIELD_KEYS:
            evidence_text = "未采样音频，此项未知"
        else:
            evidence_text = "不可观察"
    return _unknown_value(field_type), {"status": "unknown", "evidence": evidence_text}


def _coerce_value(value: Any, field_type: str, owner: str, path: str) -> Any:
    if field_type == "string":
        if not isinstance(value, str) or not value.strip():
            raise ImportRejected(400, "VALUE_TYPE_MISMATCH", "字符串字段需要非空文本", owner=owner, field=path)
        if len(value) > 4000:
            raise ImportRejected(400, "VALUE_TOO_LARGE", "字段值过长", owner=owner, field=path)
        return value.strip()
    if field_type == "number":
        number = _finite_float(value, owner, path, code="VALUE_TYPE_MISMATCH", message="数字字段必须是有限数字")
        return int(number) if float(number).is_integer() else number
    if field_type == "boolean":
        if not isinstance(value, bool):
            raise ImportRejected(400, "VALUE_TYPE_MISMATCH", "布尔字段必须是 true 或 false", owner=owner, field=path)
        return value
    if field_type == "string_array":
        if not isinstance(value, list) or not value or len(value) > 30:
            raise ImportRejected(400, "VALUE_TYPE_MISMATCH", "字符串数组需要 1 到 30 项", owner=owner, field=path)
        cleaned = []
        for item in value:
            if not isinstance(item, str) or not item.strip() or len(item) > 500:
                raise ImportRejected(400, "VALUE_TYPE_MISMATCH", "字符串数组的每一项都要是非空短文本", owner=owner, field=path)
            cleaned.append(item.strip())
        return cleaned
    if field_type in {"object", "object_array"}:
        _assert_jsonish(value, owner, path, 0)
        if field_type == "object":
            if not isinstance(value, dict):
                raise ImportRejected(400, "VALUE_TYPE_MISMATCH", "对象字段必须是对象", owner=owner, field=path)
        elif not isinstance(value, list) or len(value) > 30 or any(not isinstance(item, dict) for item in value):
            raise ImportRejected(400, "VALUE_TYPE_MISMATCH", "对象数组的每一项都必须是对象", owner=owner, field=path)
        return value
    raise ImportRejected(400, "VALUE_TYPE_MISMATCH", "不支持的字段类型", owner=owner, field=path)


def _assert_jsonish(value: Any, owner: str, path: str, depth: int) -> None:
    if depth > 6:
        raise ImportRejected(400, "VALUE_TOO_DEEP", "字段值嵌套过深", owner=owner, field=path)
    if value is None or isinstance(value, (str, bool)):
        if isinstance(value, str) and len(value) > 4000:
            raise ImportRejected(400, "VALUE_TOO_LARGE", "字段值过长", owner=owner, field=path)
        return
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            raise ImportRejected(400, "VALUE_TYPE_MISMATCH", "数字必须是有限值", owner=owner, field=path)
        return
    if isinstance(value, list):
        if len(value) > 30:
            raise ImportRejected(400, "VALUE_TOO_LARGE", "数组过长", owner=owner, field=path)
        for item in value:
            _assert_jsonish(item, owner, path, depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > 40:
            raise ImportRejected(400, "VALUE_TOO_LARGE", "对象字段过多", owner=owner, field=path)
        for key, item in value.items():
            if not isinstance(key, str):
                raise ImportRejected(400, "VALUE_TYPE_MISMATCH", "对象键必须是字符串", owner=owner, field=path)
            _assert_jsonish(item, owner, path, depth + 1)
        return
    raise ImportRejected(400, "VALUE_NOT_JSON", "字段值必须是 JSON 值", owner=owner, field=path)


def _unknown_value(field_type: str) -> Any:
    if field_type == "string":
        return "未知"
    if field_type in {"string_array", "object_array"}:
        return []
    return None


def _is_empty_unknown(value: Any) -> bool:
    return value is None or value == "" or value == "未知" or value == [] or value == {}


def _apply_computed(fields: list[dict[str, Any]], output: dict[str, Any], prefix: str, scope: str, start: float, end: float, epistemic: dict[str, Any]) -> None:
    for field in fields:
        key = field["key"]
        path = f"{prefix}.{key}" if prefix else key
        children = field.get("fields") or []
        if children:
            if not _contains_computed(children):
                continue
            child = output.get(key)
            if not isinstance(child, dict):
                child = {}
                output[key] = child
            _apply_computed(children, child, path, scope, start, end, epistemic)
            continue
        if field.get("source") != "computed":
            continue
        if scope != "shot" or path != "content.time_evidence":
            raise ImportRejected(400, "UNSUPPORTED_COMPUTED_FIELD", "导入只能由服务器填写 shot.content.time_evidence", field=f"{scope}.{path}")
        output[key] = f"{start:.3f}s-{end:.3f}s"
        epistemic[path] = {"status": "computed", "evidence": "镜头边界由 ShotLoom 记录，不是静帧模型观察"}


def _contains_computed(fields: list[dict[str, Any]]) -> bool:
    for field in fields:
        children = field.get("fields") or []
        if children and _contains_computed(children):
            return True
        if not children and field.get("source") == "computed":
            return True
    return False


def _all_leaves_computed(fields: list[dict[str, Any]]) -> bool:
    saw = False
    for field in fields:
        children = field.get("fields") or []
        if children:
            if not _all_leaves_computed(children):
                return False
            saw = True
            continue
        saw = True
        if field.get("source", "vision") != "computed":
            return False
    return saw


def _normalize_timestamps(raw: Any, start: float, end: float, shot_id: int) -> list[float]:
    values = _normalize_timestamp_list(raw, f"shot:{shot_id}")
    for stamp in values:
        if stamp < start - _TIME_TOLERANCE or stamp > end + _TIME_TOLERANCE:
            raise ImportRejected(400, "TIMESTAMP_OUTSIDE_SHOT", "采样时间必须落在镜头边界内", shot_id=shot_id)
    return values


def _normalize_timestamp_list(raw: Any, owner: str) -> list[float]:
    if not isinstance(raw, list) or not raw or len(raw) > 48:
        raise ImportRejected(400, "SAMPLED_TIMESTAMPS_REQUIRED", "必须提供 1 到 48 个采样时间", owner=owner)
    values = [round(_finite_float(item, owner, "sampled_timestamps"), 3) for item in raw]
    ordered = sorted(values)
    for previous, current in zip(ordered, ordered[1:]):
        if abs(previous - current) <= _TIME_TOLERANCE:
            raise ImportRejected(400, "DUPLICATE_TIMESTAMP", "采样时间重复", owner=owner)
    return ordered


def _required_time(value: Any, name: str, shot_id: int) -> float:
    return _finite_float(value, f"shot:{shot_id}", name)


def _finite_float(value: Any, owner: str, field: str, *, code: str = "TIME_INVALID", message: str = "必须是有限数字") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ImportRejected(400, code, message, owner=owner, field=field)
    return float(value)


def _matches(value: float, choices: list[float]) -> bool:
    return any(abs(value - choice) <= _TIME_TOLERANCE for choice in choices)


def _union_timestamps(groups: Any) -> list[float]:
    seen: set[float] = set()
    for group in groups:
        for stamp in group:
            seen.add(round(float(stamp), 3))
    return sorted(seen)
