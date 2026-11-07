"""Authenticated external static-frame import and native export provenance."""
import copy
import io
import json
from pathlib import Path
from uuid import uuid4

from openpyxl import load_workbook
from sqlalchemy import inspect

from database import Credits, CreditTransaction, Shot, Video, VideoAnalysis
from services.analysis_config import iter_leaf_fields
from test_e2e import (
    TEST_ENGINE,
    auth_headers,
    clear_client_cookies,
    client,
    create_video_with_shots,
    get_db_session,
    register,
)

FIXTURE = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "synthetic_static_frame_import.json").read_text(encoding="utf-8")
)
MARKERS = (
    "SYNTH-STATIC-RED-FRAME",
    "SYNTH-STATIC-GREEN-FRAME",
    "SYNTH-SEGMENT-BEAT",
    "SYNTH-OVERALL-BLUE-MOOD",
    "SYNTH-BEAT-INFERENCE",
)


def install_schema(token: str, video_id: int, schema: dict | None = None) -> int:
    current = client.get(f"/api/videos/{video_id}/analysis-config", headers=auth_headers(token))
    assert current.status_code == 200, current.text
    saved = client.put(
        f"/api/videos/{video_id}/analysis-config",
        json={"config": schema or FIXTURE["analysis_config"], "revision": current.json()["draft_revision"]},
        headers=auth_headers(token),
    )
    assert saved.status_code == 200, saved.text
    return saved.json()["draft_revision"]


def shot_rows(video_id: int) -> list[Shot]:
    db = get_db_session()
    try:
        return db.query(Shot).filter(Shot.video_id == video_id).order_by(Shot.index).all()
    finally:
        db.close()


def payload_for(video_id: int, revision: int, replace_existing: bool = False) -> dict:
    body = copy.deepcopy(FIXTURE["import_request"])
    body["config_revision"] = revision
    body["replace_existing"] = replace_existing
    rows = shot_rows(video_id)
    assert len(body["shots"]) == len(rows)
    for item, shot in zip(body["shots"], rows):
        assert item["index"] == shot.index
        assert item["start_time"] == shot.start_time
        assert item["end_time"] == shot.end_time
        item["id"] = shot.id
    return body


def snapshot(video_id: int) -> dict:
    db = get_db_session()
    try:
        video = db.query(Video).filter(Video.id == video_id).one()
        shots = db.query(Shot).filter(Shot.video_id == video_id).order_by(Shot.index).all()
        report = db.query(VideoAnalysis).filter(VideoAnalysis.video_id == video_id).first()
        return {
            "status": video.status,
            "error_msg": video.error_msg,
            "task": video.current_task_id,
            "shots": [
                (shot.id, shot.index, shot.start_time, shot.end_time, shot.thumbnail_path, shot._analysis)
                for shot in shots
            ],
            "continuity": report._continuity_report if report else None,
            "segments": report._segments_report if report else None,
            "rhythm": report._rhythm_report if report else None,
        }
    finally:
        db.close()


def workbook_text(workbook) -> str:
    chunks = []
    for name in workbook.sheetnames:
        for row in workbook[name].iter_rows(values_only=True):
            chunks.extend(str(cell) for cell in row if cell is not None)
    return "\n".join(chunks)


def manifest(workbook) -> dict:
    return {
        row[0]: row[1]
        for row in workbook["导出说明"].iter_rows(values_only=True)
        if row and row[0]
    }


class TestExternalAnalysisImport:
    def setup_method(self):
        registered = register(f"ext_{uuid4().hex[:10]}@example.com")
        assert registered.status_code == 200, registered.text
        self.token = registered.json()["access_token"]
        self.user_id = registered.json()["user_id"]
        self.video_id = create_video_with_shots(self.user_id, shot_count=2)
        self.revision = install_schema(self.token, self.video_id)
        self.body = payload_for(self.video_id, self.revision)
        self.url = f"/api/results/{self.video_id}/external-analysis"

    def _post(self, body: dict, token: str | None = None):
        return client.post(self.url, json=body, headers=auth_headers(token or self.token))

    def test_import_get_and_native_excel_keep_markers_and_source(self):
        db = get_db_session()
        try:
            balance = db.query(Credits).filter(Credits.user_id == self.user_id).one().balance
            shot_ids = [shot.id for shot in db.query(Shot).filter(Shot.video_id == self.video_id).order_by(Shot.index)]
            db.query(Shot).filter(Shot.video_id == self.video_id, Shot.index == 0).update({"thumbnail_path": "/tmp/synthetic-thumb.jpg"})
            db.commit()
        finally:
            db.close()

        imported = self._post(self.body)
        assert imported.status_code == 200, imported.text
        provenance = imported.json()["analysis_provenance"]
        assert provenance["source"] == "external_cliproxy_static_frame"
        assert provenance["provider"] == "cliproxy"
        assert provenance["method"] == "static_frame"
        assert provenance["coverage"] == "non_exhaustive"
        assert provenance["audio"] == "unknown"
        assert provenance["camera_movement"] == "not_observable_from_stills"
        assert provenance["sampled_timestamps"] == [1.25, 3.5, 12.0]
        assert "DashScope" in provenance["attribution"]
        assert "Qwen" in provenance["attribution"]

        fetched = client.get(f"/api/results/{self.video_id}", headers=auth_headers(self.token))
        assert fetched.status_code == 200, fetched.text
        body = fetched.json()
        assert [shot["id"] for shot in body["shots"]] == shot_ids
        assert body["shots"][0]["thumbnail_path"] == "/tmp/synthetic-thumb.jpg"
        first = body["shots"][0]["analysis"]
        assert first["picture"]["shot_scale"] == "中景 SYNTH-STATIC-RED-FRAME"
        assert first["picture"]["camera_movement"] == "未知"
        assert first["speech"]["transcript"] == "未知"
        assert first["content"]["time_evidence"] == "0.000s-9.000s"
        epistemic = first["external_import"]["epistemic"]
        assert epistemic["picture.shot_scale"]["status"] == "observation"
        assert epistemic["picture.camera_movement"]["status"] == "unknown"
        assert epistemic["beat.pacing"]["status"] == "inference"
        assert epistemic["beat.mood"]["status"] == "inference"
        assert epistemic["speech.transcript"]["status"] == "unknown"
        assert epistemic["content.time_evidence"]["status"] == "computed"
        assert "SYNTH-STATIC-GREEN-FRAME" in body["shots"][1]["analysis"]["picture"]["shot_scale"]
        assert "SYNTH-SEGMENT-BEAT" in body["segments"]["segments"][0]["segment_summary"]["title"]
        assert "SYNTH-OVERALL-BLUE-MOOD" in body["overall_analysis"]["reading"]["mood"]
        assert body["analysis_schema"]["name"] == "摄影与节奏气质"
        assert body["analysis_provenance"]["method"] == "static_frame"
        assert body["video"]["status"] == "completed"

        exported = client.get(f"/api/export/{self.video_id}?format=excel", headers=auth_headers(self.token))
        assert exported.status_code == 200, exported.text
        workbook = load_workbook(io.BytesIO(exported.content))
        labels = manifest(workbook)
        text = workbook_text(workbook)
        for marker in MARKERS:
            assert marker in text
        assert labels["分析来源"] == "外部 CLIProxy 静帧分析"
        assert labels["视觉模型"] == "未使用 ShotLoom 内置视觉模型"
        assert labels["ASR模型"].startswith("未使用")
        assert "非穷尽采样" in labels["覆盖范围"]
        assert "1.250" in labels["采样时间戳（秒）"]
        assert "12.000" in labels["采样时间戳（秒）"]
        assert "静帧不能证明摄像机运动" in labels["运镜"]
        assert "外部来源" in workbook.sheetnames
        assert "qwen3.7-plus" not in text
        assert "qwen3-asr-flash-filetrans" not in text
        assert "〔观察〕" in text
        assert "〔推断〕" in text
        assert "〔未知〕" in text
        assert "〔系统计算〕" in text

        from config import ASR_MODEL_NAME, VISION_MODEL_NAME
        from services.export_service import export_pdf_html

        html = export_pdf_html(
            {
                "filename": body["video"]["filename"],
                "duration": body["video"]["duration"],
                "vision_model": VISION_MODEL_NAME,
                "asr_model": ASR_MODEL_NAME,
            },
            body["shots"],
            body["overall_analysis"],
            body["segments"],
            body["analysis_schema"],
        )
        assert "未使用 ShotLoom 内置视觉模型" in html
        assert "外部 CLIProxy 静帧分析" in html
        assert "SYNTH-STATIC-RED-FRAME" in html
        assert "SYNTH-OVERALL-BLUE-MOOD" in html
        assert "qwen3.7-plus" not in html
        assert "qwen3-asr-flash-filetrans" not in html

        pdf_response = client.get(f"/api/export/{self.video_id}?format=pdf", headers=auth_headers(self.token))
        try:
            from weasyprint import HTML
        except Exception as exc:
            print(f"PDF_ENDPOINT_UNTESTED {type(exc).__name__}: {exc}")
            assert pdf_response.status_code == 500
        else:
            assert pdf_response.status_code == 200, pdf_response.text
            assert pdf_response.content[:4] == b"%PDF"
            rendered = HTML(string=html).write_pdf()
            assert rendered[:4] == b"%PDF"
            import shutil
            import subprocess
            import tempfile

            if shutil.which("pdftotext"):
                with tempfile.NamedTemporaryFile(suffix=".pdf") as handle:
                    handle.write(pdf_response.content)
                    handle.flush()
                    extracted = subprocess.check_output(["pdftotext", handle.name, "-"], text=True)
                assert "SYNTH-STATIC-RED-FRAME" in extracted
                assert "SYNTH-OVERALL-BLUE-MOOD" in extracted
                assert "未使用 ShotLoom 内置视觉模型" in extracted
                assert "外部 CLIProxy 静帧分析" in extracted
                assert "qwen3.7-plus" not in extracted
                assert "qwen3-asr-flash-filetrans" not in extracted
                print(f"PDF_ENDPOINT_TESTED bytes={len(pdf_response.content)} text=pdftotext")
            else:
                print(f"PDF_ENDPOINT_TESTED bytes={len(pdf_response.content)} text=header-only")

        db = get_db_session()
        try:
            assert db.query(Credits).filter(Credits.user_id == self.user_id).one().balance == balance
            assert db.query(CreditTransaction).filter(
                CreditTransaction.user_id == self.user_id,
                CreditTransaction.reason == "analysis",
            ).count() == 0
        finally:
            db.close()

    def test_native_v2_export_still_names_configured_models(self):
        from services.export_service import export_excel

        data = export_excel(
            {
                "filename": "native.mp4",
                "vision_model": "qwen3.7-plus",
                "asr_model": "qwen3-asr-flash-filetrans",
            },
            [{
                "index": 0,
                "start_time": 0.0,
                "end_time": 1.0,
                "duration": 1.0,
                "analysis": {"picture": {"shot_scale": "中景"}},
            }],
            {},
            {"segments": []},
            FIXTURE["analysis_config"],
        )
        workbook = load_workbook(io.BytesIO(data))
        labels = manifest(workbook)
        assert labels["视觉模型"] == "qwen3.7-plus"
        assert labels["ASR模型"] == "qwen3-asr-flash-filetrans"
        assert "外部来源" not in workbook.sheetnames
        assert "中景" in workbook_text(workbook)
        assert "〔观察〕" not in workbook_text(workbook)

    def test_camera_movement_inference_keeps_the_still_frame_limit(self):
        body = copy.deepcopy(self.body)
        body["shots"][0]["analysis"]["picture"]["camera_movement"] = {
            "status": "inference",
            "value": "可能是固定机位",
            "evidence": "两张静帧里人物位置接近，但不能证明机位没有移动",
            "frame_time": 1.25,
        }
        response = self._post(body)
        assert response.status_code == 200, response.text
        fetched = client.get(f"/api/results/{self.video_id}", headers=auth_headers(self.token))
        shot = fetched.json()["shots"][0]["analysis"]
        assert shot["picture"]["camera_movement"] == "可能是固定机位"
        evidence = shot["external_import"]["epistemic"]["picture.camera_movement"]
        assert evidence["status"] == "inference"
        assert "静帧不能证明摄像机运动，此项仅为推断。" in evidence["evidence"]
        excel = client.get(f"/api/export/{self.video_id}?format=excel", headers=auth_headers(self.token))
        text = workbook_text(load_workbook(io.BytesIO(excel.content)))
        assert "可能是固定机位 〔推断〕" in text
        assert "qwen3.7-plus" not in text

    def test_replace_existing_and_stale_revision_are_atomic(self):
        assert self._post(self.body).status_code == 200
        stale = copy.deepcopy(self.body)
        stale["config_revision"] = self.revision - 1
        stale["replace_existing"] = True
        before = snapshot(self.video_id)
        rejected = self._post(stale)
        assert rejected.status_code == 409
        assert rejected.json()["detail"]["code"] == "CONFIG_REVISION_CONFLICT"
        assert snapshot(self.video_id) == before

        blocked = copy.deepcopy(self.body)
        blocked["replace_existing"] = False
        blocked["shots"][0]["analysis"]["picture"]["shot_scale"]["value"] = "不应写入"
        rejected = self._post(blocked)
        assert rejected.status_code == 409
        assert rejected.json()["detail"]["code"] == "RESULTS_EXIST"
        assert snapshot(self.video_id) == before

        replaced = copy.deepcopy(self.body)
        replaced["replace_existing"] = True
        replaced["shots"][0]["analysis"]["picture"]["shot_scale"]["value"] = "中景 SYNTH-STATIC-RED-FRAME-REPLACED"
        accepted = self._post(replaced)
        assert accepted.status_code == 200, accepted.text
        fetched = client.get(f"/api/results/{self.video_id}", headers=auth_headers(self.token))
        assert "SYNTH-STATIC-RED-FRAME-REPLACED" in fetched.json()["shots"][0]["analysis"]["picture"]["shot_scale"]

    def test_invalid_import_does_not_write_partial_results(self):
        db = get_db_session()
        try:
            first = db.query(Shot).filter(Shot.video_id == self.video_id, Shot.index == 0).one()
            first.analysis = {"marker": "KEEP-ME"}
            first.thumbnail_path = "/tmp/keep-thumb.jpg"
            report = VideoAnalysis(video_id=self.video_id)
            report.continuity_report = {"marker": "KEEP-OVERALL"}
            report.segments_report = {"segments": [{"marker": "KEEP-SEGMENT"}]}
            report.rhythm_report = {"marker": "KEEP-RHYTHM"}
            db.add(report)
            video = db.query(Video).filter(Video.id == self.video_id).one()
            video.status = "completed"
            video.error_msg = "keep-error"
            db.commit()
            original_ids = [shot.id for shot in db.query(Shot).filter(Shot.video_id == self.video_id).order_by(Shot.index)]
        finally:
            db.close()
        before = snapshot(self.video_id)

        cases = []
        boundary = copy.deepcopy(self.body)
        boundary["replace_existing"] = True
        boundary["shots"][1]["end_time"] = 18.0
        cases.append((boundary, "SHOT_BOUNDARY_MISMATCH"))

        missing = copy.deepcopy(self.body)
        missing["replace_existing"] = True
        missing["shots"] = missing["shots"][:1]
        cases.append((missing, "SHOT_SET_MISMATCH"))

        camera = copy.deepcopy(self.body)
        camera["replace_existing"] = True
        camera["shots"][0]["analysis"]["picture"]["camera_movement"] = {
            "status": "observation",
            "value": "向右横摇",
            "evidence": "单张静帧看起来像动了",
            "frame_time": 1.25,
        }
        cases.append((camera, "CAMERA_MOVEMENT_NOT_OBSERVABLE"))

        audio = copy.deepcopy(self.body)
        audio["replace_existing"] = True
        audio["shots"][0]["analysis"]["speech"]["transcript"] = {
            "status": "observation",
            "value": "你好",
            "evidence": "其实没有音频",
            "frame_time": 1.25,
        }
        cases.append((audio, "AUDIO_UNKNOWN_REQUIRED"))

        outside = copy.deepcopy(self.body)
        outside["replace_existing"] = True
        outside["shots"][0]["sampled_timestamps"] = [30.0]
        outside["shots"][0]["analysis"]["picture"]["shot_scale"]["frame_time"] = 30.0
        outside["shots"][0]["analysis"]["picture"]["composition"]["frame_time"] = 30.0
        outside["shots"][0]["analysis"]["picture"]["lighting_color"]["frame_time"] = 30.0
        outside["shots"][0]["analysis"]["beat"]["pacing"]["frame_time"] = 30.0
        outside["shots"][0]["analysis"]["beat"]["mood"]["frame_time"] = 30.0
        cases.append((outside, "TIMESTAMP_OUTSIDE_SHOT"))

        for body, code in cases:
            response = self._post(body)
            assert response.status_code == 400, response.text
            assert response.json()["detail"]["code"] == code
            assert snapshot(self.video_id) == before
            db = get_db_session()
            try:
                ids = [shot.id for shot in db.query(Shot).filter(Shot.video_id == self.video_id).order_by(Shot.index)]
            finally:
                db.close()
            assert ids == original_ids

    def test_running_task_rejects_import(self):
        db = get_db_session()
        try:
            video = db.query(Video).filter(Video.id == self.video_id).one()
            video.status = "analyzing"
            video.current_task_id = "task_block_import"
            db.commit()
        finally:
            db.close()
        before = snapshot(self.video_id)
        response = self._post(self.body)
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "ANALYSIS_ALREADY_RUNNING"
        assert snapshot(self.video_id) == before

    def test_unauthorized_wrong_video_and_other_owner(self):
        other = register(f"other_{uuid4().hex[:10]}@example.com")
        other_token = other.json()["access_token"]
        other_video = create_video_with_shots(other.json()["user_id"], shot_count=2)
        install_schema(other_token, other_video)
        foreign_body = payload_for(other_video, self.revision)
        before = snapshot(self.video_id)
        other_before = snapshot(other_video)

        mismatched = self._post(foreign_body)
        assert mismatched.status_code == 400
        assert mismatched.json()["detail"]["code"] == "SHOT_ID_MISMATCH"
        assert snapshot(self.video_id) == before
        assert snapshot(other_video) == other_before

        forbidden = client.post(self.url, json=self.body, headers=auth_headers(other_token))
        assert forbidden.status_code == 403
        assert snapshot(self.video_id) == before

        missing = client.post(
            "/api/results/999999/external-analysis",
            json=self.body,
            headers=auth_headers(self.token),
        )
        assert missing.status_code == 404

        clear_client_cookies()
        anonymous = client.post(self.url, json=self.body)
        assert anonymous.status_code == 401
        assert snapshot(self.video_id) == before

    def test_adjust_still_drops_submitted_analysis(self):
        response = client.put(
            f"/api/shots/{self.video_id}/adjust",
            json={
                "overall_analysis": {"reading": {"mood": "SHOULD-NOT-STICK"}},
                "shots": [
                    {"start_time": 0.0, "end_time": 4.0, "analysis": {"picture": {"shot_scale": "SHOULD-NOT-STICK"}}},
                    {"start_time": 4.0, "end_time": 8.0, "analysis": {"picture": {"shot_scale": "SHOULD-NOT-STICK"}}},
                ],
            },
            headers=auth_headers(self.token),
        )
        assert response.status_code == 200, response.text
        fetched = client.get(f"/api/results/{self.video_id}", headers=auth_headers(self.token))
        assert fetched.status_code == 200
        assert fetched.json()["overall_analysis"] is None
        assert fetched.json()["analysis_provenance"] is None
        assert all(shot["analysis"] is None for shot in fetched.json()["shots"])

    def test_default_schema_import_and_provenance_columns_are_existing_text(self):
        video_id = create_video_with_shots(self.user_id, shot_count=1)
        revision = install_schema(self.token, video_id, _vision_only_default_schema())
        db = get_db_session()
        try:
            shot = db.query(Shot).filter(Shot.video_id == video_id).one()
            shot_id = shot.id
            start = shot.start_time
            end = shot.end_time
            config = client.get(
                f"/api/videos/{video_id}/analysis-config",
                headers=auth_headers(self.token),
            ).json()["draft_config"]
        finally:
            db.close()
        body = _default_schema_request(config, shot_id, start, end, revision)
        response = client.post(
            f"/api/results/{video_id}/external-analysis",
            json=body,
            headers=auth_headers(self.token),
        )
        assert response.status_code == 200, response.text
        fetched = client.get(f"/api/results/{video_id}", headers=auth_headers(self.token))
        analysis = fetched.json()["shots"][0]["analysis"]
        assert "SYNTH-DEFAULT-SCHEMA" in analysis["content"]["visual_summary"]
        assert analysis["visual_execution"]["camera_movement"] == "未知"
        assert analysis["speech"]["transcript"] == "未知"
        assert fetched.json()["analysis_provenance"]["source"] == "external_cliproxy_static_frame"

        shot_columns = {column["name"] for column in inspect(TEST_ENGINE).get_columns("shots")}
        report_columns = {column["name"] for column in inspect(TEST_ENGINE).get_columns("video_analyses")}
        assert "external_import" not in shot_columns
        assert "analysis" in shot_columns
        assert "import_provenance" not in report_columns
        assert {"continuity_report", "segments_report", "rhythm_report"} <= report_columns

    def test_non_cliproxy_provider_is_rejected(self):
        body = copy.deepcopy(self.body)
        body["provenance"]["provider"] = "dashscope"
        response = self._post(body)
        assert response.status_code == 422
        assert snapshot(self.video_id)["continuity"] is None


def _vision_only_default_schema() -> dict:
    """The built-in preset is valid to import, including its ASR leaves as unknown."""
    from prompt_config import load_prompt_config

    return load_prompt_config()


def _statement_for(path: str, field: dict, frame_time: float):
    key = path.rsplit(".", 1)[-1]
    source = field.get("source", "vision")
    audio_keys = {
        "transcript", "transcript_segments", "language", "speech_emotion", "dialogue",
        "speaker", "music", "ambient_sound", "sound_type", "speaker_emotion",
        "transcript_timestamps", "audiovisual_sync", "audio_narrative_role",
        "subtitle_speech_consistency", "speech_visual_coordination",
        "speech_subtitle_strategy", "audio_arc",
    }
    if source == "computed":
        return None
    if source == "asr" or key in audio_keys or key in {"camera_movement", "movement", "camera_move"}:
        return {"status": "unknown", "value": None, "evidence": "静帧或无音频"}
    field_type = field.get("type", "string")
    if path == "content.visual_summary":
        value = "静帧里是红色背景前的人物 SYNTH-DEFAULT-SCHEMA"
    elif field_type == "number":
        value = 4
    elif field_type == "boolean":
        value = True
    elif field_type == "string_array":
        value = ["可见的画面信息"]
    elif field_type == "object":
        value = {"note": "静帧"}
    elif field_type == "object_array":
        value = [{"note": "静帧"}]
    else:
        value = "静帧可见的摄影信息"
    return {
        "status": "observation",
        "value": value,
        "evidence": "来自采样静帧，不涉及口播或运镜",
        "frame_time": frame_time,
    }


def _build_scope(fields: list[dict], frame_time: float, prefix: str = "") -> dict:
    payload = {}
    for field in fields:
        path = f"{prefix}.{field['key']}" if prefix else field["key"]
        children = field.get("fields") or []
        if children:
            payload[field["key"]] = _build_scope(children, frame_time, path)
            continue
        statement = _statement_for(path, field, frame_time)
        if statement is not None:
            payload[field["key"]] = statement
    return payload


def _default_schema_request(config: dict, shot_id: int, start: float, end: float, revision: int) -> dict:
    frame_time = round(start + min(1.0, (end - start) / 2), 3)
    scopes = config["scopes"]
    shot_analysis = _build_scope(scopes["shot"], frame_time)
    return {
        "config_revision": revision,
        "replace_existing": False,
        "provenance": {
            "provider": "cliproxy",
            "method": "static_frame",
            "coverage": "non_exhaustive",
            "audio": "absent",
            "external_model": "synthetic-static-reviewer",
        },
        "shots": [{
            "id": shot_id,
            "index": 0,
            "start_time": start,
            "end_time": end,
            "sampled_timestamps": [frame_time],
            "analysis": shot_analysis,
        }],
        "segments": [{
            "segment_index": 0,
            "shot_indices": [0],
            "sampled_timestamps": [frame_time],
            "analysis": _build_scope(scopes["segment"], frame_time),
        }],
        "overall_analysis": _build_scope(scopes["overall"], frame_time),
    }


def test_fixture_schema_is_valid_and_has_no_cta_prompt():
    from services.analysis_config import validate_config

    config = validate_config(FIXTURE["analysis_config"])
    prompt_text = json.dumps(config["prompts"], ensure_ascii=False)
    assert "转化" not in prompt_text
    assert "CTA" not in prompt_text
    assert "带货" not in prompt_text
    assert "行动号召" not in prompt_text
    assert "运镜" in prompt_text
    assert "节奏" in prompt_text
    assert "情绪" in prompt_text
    leaves = [path for path, _field in iter_leaf_fields(config["scopes"]["shot"])]
    assert "picture.camera_movement" in leaves
    assert "speech.transcript" in leaves
