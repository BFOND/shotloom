# Apply the external-analysis import onto an existing ShotLoom checkout

Use this on a local checkout that is already running upstream commit `78b65e24a587052ff2c0c4ccae72575295bde34f` (the same commit as BFOND/shotloom `78b65e2`). The patch adds an authenticated import route and teaches the native Excel/PDF exporter to label imported static-frame results as external CLIProxy analysis.

It does not call a model, does not read production media, and does not need a new Python package.

## Leave these alone

Do not replace or delete any of the following:

- `.env`, `backend/.env`, `frontend/.env`
- `*.db`, `*.sqlite`, `backend/app.db`, Postgres volumes
- `uploads/`, `backend/uploads/`, `shots/`, `backend/shots/`, `thumbnails/`, `backend/thumbnails/`
- the existing virtualenv (`backend/.venv`, `venv`, or the interpreter the service already uses)

Do not run `docker compose down -v`. Do not run `start.sh` for this upgrade: that script installs into `.venv` whenever it starts.

No Alembic revision and no SQLite `ALTER TABLE` are required. Restarting the existing process is enough for the new code to load. `init_db()` does not add a column for this feature.

## Option A: checkout the fork branch

From the existing clone, if `origin` is `BFOND/shotloom` and the worktree is clean except for ignored data and env files:

```bash
git fetch origin cursor/external-analysis-import-5f27
git switch -c cursor/external-analysis-import-5f27 origin/cursor/external-analysis-import-5f27
```

If the clone tracks upstream instead of `BFOND/shotloom`, add the fork as a remote and fetch only this branch. Do not push the result to upstream.

## Option B: apply the patch

The patch was made from `78b65e24a587052ff2c0c4ccae72575295bde34f` and does not include this `artifacts/` evidence file. From the checkout root:

```bash
git rev-parse HEAD
# expect 78b65e24a587052ff2c0c4ccae72575295bde34f

git apply --check artifacts/shotloom-external-analysis-import.patch
git apply artifacts/shotloom-external-analysis-import.patch
```

Copy the patch in from the BFOND pull request (`artifacts/shotloom-external-analysis-import.patch`) if it is not already in the tree. `git apply` only changes tracked source files. It does not touch `.env`, databases, uploads, or the virtualenv.

If `git apply --check` fails, the checkout is not that commit or it already has local source edits. Stop and reconcile those source edits. Do not reset the worktree in a way that deletes ignored data.

## Restart

Stop only the API and worker processes. Start them again with the same interpreter, environment file, and database URL they already use. Example, when the app already lives in `backend/` and the venv already exists:

```bash
backend/.venv/bin/python -m uvicorn main:app --app-dir backend --host 0.0.0.0 --port 8000
```

Keep using the existing worker command if a worker is part of the install. The import path does not enqueue a job.

## Check with the synthetic fixture

Use a throwaway account and a short video you are allowed to discard, or shots created by the test helper. Do not send a production film, its frames, or its transcript to CLIProxy or anywhere else for this check.

1. Log in and detect shots.
2. Save `analysis_config` from `backend/fixtures/synthetic_static_frame_import.json` with `PUT /api/videos/{video_id}/analysis-config`.
3. Rewrite the fixture's `import_request` ids, boundaries, and `config_revision` from `GET /api/results/{video_id}`.
4. `POST /api/results/{video_id}/external-analysis`.
5. `GET /api/results/{video_id}` and confirm `SYNTH-STATIC-RED-FRAME`, `SYNTH-STATIC-GREEN-FRAME`, and `SYNTH-OVERALL-BLUE-MOOD`.
6. `GET /api/export/{video_id}?format=excel`. In `导出说明`, `视觉模型` is `未使用 ShotLoom 内置视觉模型` and `分析来源` is `外部 CLIProxy 静帧分析`.

A request that changes a boundary, omits a shot, or marks camera movement as an observation returns 400 and leaves the previous `shots.analysis` values in place.

API details: `docs/external-analysis-import.md`.
