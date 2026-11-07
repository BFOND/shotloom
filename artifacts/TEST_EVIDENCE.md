# External static-frame import — test evidence

Base: `78b65e24a587052ff2c0c4ccae72575295bde34f`  
Feature commit: `8bf03ebddce05dfccd19bef550c802a40dc3c122`  
Patch: `artifacts/shotloom-external-analysis-import.patch` (applies cleanly on the base commit with `git apply --check`)

No model API was called. The fixture is synthetic text only (`backend/fixtures/synthetic_static_frame_import.json`). No production media was read or sent.

## Command

```bash
cd backend
python3 -m pytest test_e2e.py test_external_analysis_import.py test_progress_fix.py test_progress_update.py -q --tb=line
```

`pytest-asyncio` was installed for the two existing async progress tests (`backend/requirements-dev.txt`). Result: **120 passed** in 19.95s.

## What the new tests checked

- `POST /api/results/{video_id}/external-analysis` as the video owner, then `GET /api/results/{video_id}`: shot ids unchanged, `SYNTH-STATIC-RED-FRAME`, `SYNTH-STATIC-GREEN-FRAME`, `SYNTH-SEGMENT-BEAT`, `SYNTH-OVERALL-BLUE-MOOD`, and `SYNTH-BEAT-INFERENCE` stored.
- Epistemic statuses: shot scale `observation`, pacing and mood `inference`, camera movement and transcript `unknown`, time evidence `computed` from the stored cut (`0.000s-9.000s`).
- Provenance: `external_cliproxy_static_frame`, provider `cliproxy`, method `static_frame`, coverage `non_exhaustive`, audio `unknown`, camera movement `not_observable_from_stills`. Attribution says the result is not a ShotLoom Qwen or DashScope run.
- Native `GET /api/export/{video_id}?format=excel`: those markers are in the workbook; `导出说明` uses `分析来源=外部 CLIProxy 静帧分析`, `视觉模型=未使用 ShotLoom 内置视觉模型`, ASR `未使用`, non-exhaustive coverage, sample timestamps `1.250` and `12.000`. The workbook text does not contain `qwen3.7-plus` or `qwen3-asr-flash-filetrans`. Cells are suffixed `〔观察〕`, `〔推断〕`, `〔未知〕`, `〔系统计算〕`.
- A built-in-shaped v2 export with no `external_import` block still labels `视觉模型` as `qwen3.7-plus` and `ASR模型` as `qwen3-asr-flash-filetrans`, and does not add the `外部来源` sheet.
- Rejected imports leave the previous shot analysis, overall report, rhythm report, shot ids, thumbnail path, status, and error message unchanged. Cases: bad boundary, missing shot, camera movement marked as observation, transcript marked as observation, sample time outside the shot, stale `config_revision`, `replace_existing=false` when results exist, active analysis task, another video's shot ids.
- Auth: no token returns 401, another user returns 403, unknown video returns 404. `provider=dashscope` returns 422.
- `PUT /api/shots/{video_id}/adjust` still returns 200 and still does not keep submitted analysis.
- The default v2 schema can be imported when ASR and camera-movement leaves are `unknown`. No new SQLite column is added (`shots.analysis` and `video_analyses.continuity_report` / `segments_report` / `rhythm_report` only).
- No credit transaction with reason `analysis` is created.

## PDF

`GET /api/export/{video_id}?format=pdf` returned **200** and a `%PDF` file of 56567 bytes. `pdftotext` on that file contained `SYNTH-STATIC-RED-FRAME`, `SYNTH-OVERALL-BLUE-MOOD`, `未使用 ShotLoom 内置视觉模型`, and `外部 CLIProxy 静帧分析`, and did not contain `qwen3.7-plus` or `qwen3-asr-flash-filetrans`. The same strings are asserted on the HTML passed to WeasyPrint. If WeasyPrint is not installed, the test records the endpoint as untested and still checks that HTML.
