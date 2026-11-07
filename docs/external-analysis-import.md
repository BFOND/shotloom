# External static-frame analysis import

ShotLoom can store analysis that an external client already produced and can export it with the native Excel/PDF exporter. This route does not call CLIProxy, Qwen, DashScope, or any other model, and it does not accept images.

`PUT /api/shots/{video_id}/adjust` only replaces shot boundaries. A body that also contains `analysis` still returns 200, then deletes existing analysis. It is not an import API.

## Flow

1. Detect shots in ShotLoom so each shot has a stable `id`, `index`, `start_time`, and `end_time`.
2. `GET /api/videos/{video_id}/analysis-config` and, if you want the cinematography schema in the sample, `PUT` that config with the current `draft_revision`.
3. `GET /api/results/{video_id}` and copy `config_revision`, every shot `id`, `index`, `start_time`, and `end_time`.
4. `POST /api/results/{video_id}/external-analysis` with a bearer token for the video owner.
5. `GET /api/results/{video_id}` to read the stored analysis.
6. `GET /api/export/{video_id}?format=excel` or `format=pdf` to download the native report.

The sample body is `backend/fixtures/synthetic_static_frame_import.json`. Its shot ids are placeholders. Replace them with the ids from `GET /api/results/{video_id}`. The sample boundaries match a two-shot video at `0–9s` and `10–19s`. For any other cut, copy the stored boundaries; the server rejects a mismatch of more than 1 millisecond.

The sample prompts describe cinematography, beat, and mood. They are instructions for the external client. ShotLoom does not send them to a model.

## Request

`POST /api/results/{video_id}/external-analysis`

```json
{
  "config_revision": 2,
  "replace_existing": false,
  "provenance": {
    "provider": "cliproxy",
    "method": "static_frame",
    "coverage": "non_exhaustive",
    "audio": "absent",
    "external_model": "synthetic-static-reviewer"
  },
  "shots": [],
  "segments": [],
  "overall_analysis": {}
}
```

`config_revision` must equal the current draft revision (`config_revision` on `GET /api/results/{video_id}`). The server validates every leaf against that draft, then activates it, so the native exporter uses the same schema.

`shots` must list every current shot exactly once. Each item's `id`, `index`, `start_time`, and `end_time` must match the stored shot. `sampled_timestamps` are seconds inside that shot, with at least one timestamp. The import does not create, delete, or move shots.

`segments` must be a complete partition of those shot indexes: `segment_index` is `0..n-1`, and every shot index appears in exactly one segment. A segment timestamp has to be one of the sampled timestamps of its shots.

`overall_analysis` is required. `frame_time` on an overall statement has to be one of the shot sample times.

### Leaf values

Every schema leaf except server-owned computed fields is an object:

```json
{"status": "observation", "value": "中景", "evidence": "人物约占画面高度一半", "frame_time": 1.25}
```

`status` is `observation`, `inference`, or `unknown`.

- `observation` and `inference` need a non-empty `value`, `evidence`, and a `frame_time` that was listed in `sampled_timestamps`.
- `unknown` needs an empty `value` (`null`, `""`, `"未知"`, `[]`, or `{}`). Do not send `frame_time`. Strings are stored as `未知`.
- `camera_movement` (and `movement` / `camera_move`) cannot be `observation`. A still cannot prove camera movement. An `inference` is kept only with that limitation appended to the evidence.
- ASR fields, and audio keys such as `transcript`, `dialogue`, `music`, and `ambient_sound`, must be `unknown`. Absent audio stays unknown.
- `shot.content.time_evidence` is computed by ShotLoom from the shot boundary. Omit it.

`replace_existing` defaults to false. The first import can omit it. A later import must set it to true or the server returns 409 and leaves the previous rows untouched.

## Response and readback

Success is `200` with `analysis_provenance`. `GET /api/results/{video_id}` then returns:

- `shots[].analysis` with schema values plus `analysis_source=external_cliproxy_static_frame` and `external_import` (sampled timestamps and per-field epistemic status)
- `segments` and `overall_analysis`
- `analysis_provenance.source=external_cliproxy_static_frame`
- `analysis_provenance.coverage=non_exhaustive`
- `analysis_provenance.audio=unknown`
- `analysis_provenance.camera_movement=not_observable_from_stills`

The native workbook's `导出说明` sheet says the vision model was not ShotLoom's built-in model and ASR was not used. It names CLIProxy static frames, non-exhaustive coverage, sample timestamps, and the camera-movement limit. Those cells are not `qwen3.7-plus` or `qwen3-asr-flash-filetrans`. A built-in Qwen run does not gain this sheet, and its model rows stay on the configured Qwen/ASR names.

## Errors

Invalid input does not write shot analysis or the video-level report. The import uses one transaction. A rejected request rolls that transaction back, including a draft config row that existed only in the failed request. Config rows created earlier by `GET` or `PUT /api/videos/{video_id}/analysis-config` stay as they were.

| HTTP | code | when |
| --- | --- | --- |
| 401 | | missing login |
| 403 | | another user's video |
| 404 | | unknown video |
| 409 | `CONFIG_REVISION_CONFLICT` | revision is not the current draft |
| 409 | `RESULTS_EXIST` | results exist and `replace_existing` is false |
| 409 | `ANALYSIS_ALREADY_RUNNING` | a task is active |
| 400 | `SHOT_ID_MISMATCH` | id is not on this video |
| 400 | `SHOT_SET_MISMATCH` | shot list is not exactly the current set |
| 400 | `SHOT_BOUNDARY_MISMATCH` | boundaries differ from the stored cut |
| 400 | `SHOT_INDEX_MISMATCH` | index does not match the id |
| 400 | `CAMERA_MOVEMENT_NOT_OBSERVABLE` | movement marked as observation |
| 400 | `AUDIO_UNKNOWN_REQUIRED` | audio or ASR marked as observation or inference |
| 400 | `TIMESTAMP_OUTSIDE_SHOT` | sample time outside the shot |
| 400 | `FRAME_TIME_NOT_SAMPLED` | evidence frame was not declared |
| 400 | `SEGMENT_COVERAGE_MISMATCH` | segments do not cover every shot exactly once |
| 422 | | provenance is not `cliproxy` / `static_frame` / `non_exhaustive` / `audio=absent`, or the saved analysis config is invalid |

## Storage

Values stay in the existing `shots.analysis`, `video_analyses.continuity_report`, and `video_analyses.segments_report` text columns. There is no new table and no `ALTER TABLE`. SQLite and Postgres both keep storing JSON text. A later built-in analysis overwrites those JSON values, and the export stops claiming the external source once those markers are gone.
