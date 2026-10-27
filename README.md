# Entropy

Entropy is an AI coding workspace built with FastAPI. It streams agent activity,
manages generated workspace files, and can start local project previews.

## Run locally

Requirements: Python 3.13 and [uv](https://docs.astral.sh/uv/).

1. Install dependencies:

   ```powershell
   uv sync
   ```

2. Copy `.env.example` to `.env` and fill in your model provider's API key,
   API base URL, model name, and a long random `SESSION_SECRET`.

3. Start the app:

   ```powershell
   uv run uvicorn web.app:app --host 127.0.0.1 --port 8000
   ```

4. Open <http://127.0.0.1:8000>.

Do not use Uvicorn's `--reload` while running generated tasks. Its file watcher
can restart Entropy when files are written to `workspace/`, which clears the
in-memory registry for running previews. Preview links remain available only
while the Entropy process that started them is running.

Never commit `.env`; it contains private credentials. `.env.example` contains
placeholders only.

## Deploy on Render

1. Push this repository to GitHub, then in Render choose **New + → Web Service**
   and connect the repository's `main` branch.
2. Set the runtime to **Python** and use:

   - **Build command:** `pip install uv && uv sync --frozen`
   - **Start command:** `uv run --frozen uvicorn web.app:app --host 0.0.0.0 --port $PORT`

3. Add these environment variables in Render's **Environment** settings:

   - `base_url`: your model provider's API base URL.
   - `api_key`: your model provider's API key. Enter it in Render only; do not
     put it in GitHub.
   - `LLM_MODEL`: a model available from that provider.
   - `SESSION_SECRET`: a long, random secret generated for this deployment.
   - `SESSION_COOKIE_SECURE`: `true`.
   - `ENTROPY_DB_PATH`: `/var/data/entropy.db` (after adding the persistent disk
     below).

4. In the service's **Disks** settings, add a persistent disk mounted at
   `/var/data`. The app stores login accounts in SQLite; without a persistent
   disk, Render's ephemeral filesystem can lose the database during a restart
   or redeploy.
5. Deploy, then open the `onrender.com` URL shown by Render.

Generated projects run on an internal loopback port. Entropy serves them through
an authenticated `/preview/{project_id}/` reverse-proxy route on the same public
Render URL, so generated apps do not need their own public ports. The proxy
rewrites root-relative HTML, JavaScript, and CSS URLs to stay under the preview
path. Run the service as a single instance with one worker because project
processes are kept in that app process's memory.