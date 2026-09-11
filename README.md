# Hail detector

This service monitors the EUMETNET **OPERA DBZH maximum-reflectivity composite** around Sofia. It archives native ODIM HDF5 composites, segments and tracks significant reflectivity cells, projects confirmed tracks through a fixed target radius, and can send a Telegram alert for a severe approaching signature.

The optional Craiova OPERA polar-volume feed provides an experimental vertical DBZH profile for each confirmed primary track. It never changes the primary track or projection. When the optional web-research gate is enabled, its latest matching profile is passed to the agent as explicitly experimental context.

## Alert rule

Telegram delivery is enabled only for a fresh OPERA composite when all of these hold:

- a confirmed track has a maximum observed band of at least **65 dBZ**;
- its medium-or-higher confidence fitted trajectory enters the configured target radius within **30 minutes** (including immediate entry);
- all current-frame archival, rendering, and analysis steps succeeded.

This is a severe-reflectivity signature, not confirmation of hail and not an official weather warning. Transitions are deduplicated per track, and alerts expire only after 20 minutes of fresh non-qualifying data.

## Optional web-researched hail-risk gate

Set `HAIL_LLM_ENABLED=true` and provide `OPENAI_API_KEY` to require a second, internet-backed assessment before Telegram delivery. A confirmed track is sent to the agent only when it is projected to enter the target radius within `HAIL_LLM_MAXIMUM_LEAD_MINUTES` and reaches `HAIL_LLM_CANDIDATE_MINIMUM_DBZ`. The agent receives the current latitude/longitude, recent radar observations, dBZ bands, cell areas, trajectory, target region, and any matching experimental volume profile.

The Responses API is required to use web search and a strict JSON schema (`rating`, `description`, and sources). Each automatic request starts a persisted, per-track **30-minute cooldown**. During that window a successful result is reused, while a failed request remains fail-closed and is not retried. The response is saved in `/assessments` as `llm_hail_risk`; a Telegram event is created only when the returned rating is **strictly greater than** `HAIL_LLM_ALERT_THRESHOLD` (default: `7`). Its `description` becomes the alert reason.

Each displayed confirmed track also has a **Test hail-risk agent** button when the agent is enabled. It sends the archived track to the agent with simulated qualifying dBZ, target-intersection, and lead-time fields, but never creates an alert, mutates alert state, or sends Telegram. The full test response is logged in the browser developer console.

## Run locally

Use two processes in separate terminals:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m hail_detector.poller
```

```powershell
.\.venv\Scripts\uvicorn.exe hail_detector.api:app --app-dir src --reload
```

The debug dashboard is at `http://127.0.0.1:8000/`. `GET /health` returns HTTP 503 until a fresh primary composite has been archived and analysed. `GET /tracks`, `/assessments`, and `/alerts` expose the current OPERA state.

## Run automatically on Ubuntu/EC2

The two systemd unit files in `deploy/systemd/` start the API and poller at
boot and restart either process if it exits. They assume the app is installed
at `/home/ubuntu/hail-detector` and runs as the `ubuntu` user, matching the
EC2 deployment. If your path or user differs, update those values first.

After deploying this version to the instance, stop any manually launched copy
of the app and install the units. Check `run/api.pid` and `run/poller.pid`
with `ps` first; if either process is still running, stop that specific
reported PID before enabling the corresponding service.

```bash
cd ~/hail-detector
sudo install -m 0644 deploy/systemd/hail-detector-api.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/hail-detector-poller.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hail-detector-api.service hail-detector-poller.service
```

Check the services and their live logs with:

```bash
systemctl status hail-detector-api hail-detector-poller
journalctl -u hail-detector-api -u hail-detector-poller -f
```

The services read the existing `.env` automatically through the application
configuration. Do not use `start.sh` after systemd takes over; it is only for
the manual, background-process deployment mode.

## Configuration

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `HAIL_POLL_INTERVAL_SECONDS` | `1800` | Standard automatic polling interval (30 minutes). |
| `HAIL_NEARBY_TRACK_POLL_INTERVAL_SECONDS` / `HAIL_NEARBY_TRACK_DISTANCE_KM` / `HAIL_NEARBY_TRACK_HOLD_UPDATES` | `600` / `100` / `5` | A fresh track within 100 km of the protected-circle edge temporarily switches automatic primary and volume updates to 10 minutes for the next five updates. |
| `HAIL_OPERA_ENABLED` | `true` | Enable the primary OPERA DBZH composite poller. |
| `HAIL_OPERA_LOOKBACK_MINUTES` / `HAIL_OPERA_MAX_FRAME_AGE_MINUTES` | `70` / `15` | Recent archive window and freshness requirement. |
| `HAIL_OPERA_SEGMENT_MINIMUM_DBZ` / `HAIL_OPERA_SEGMENT_MINIMUM_PIXELS` | `35` / `2` | Sensitive raw-component segmentation threshold. |
| `HAIL_OPERA_STORM_MIN_AREA_SQUARE_KM` / `HAIL_OPERA_STORM_MIN_45DBZ_CORE_SQUARE_KM` | `30` / `8` | Broad-cell operational tracking gate. |
| `HAIL_OPERA_STORM_MIN_55DBZ_CORE_SQUARE_KM` | `1` | Compact-core exception to the broad-cell gate. |
| `HAIL_OPERA_TRACK_MINIMUM_CONFIRMATIONS` | `2` | Observations required before a track is operationally confirmed. |
| `HAIL_TARGET_LATITUDE` / `HAIL_TARGET_LONGITUDE` / `HAIL_WARNING_RADIUS_KM` | Sofia centre / `10` | Fixed protected location and radius. |
| `HAIL_SEVERE_MINIMUM_DBZ` / `HAIL_SEVERE_MAXIMUM_LEAD_MINUTES` | `65` / `30` | Telegram severe-reflectivity threshold and maximum target-entry lead time. |
| `HAIL_MINIMUM_TRAJECTORY_CONFIDENCE` | `medium` | Lowest allowed fitted-track confidence: `low`, `medium`, or `high`. |
| `HAIL_ALERT_EXPIRY_MINUTES` | `20` | Fresh non-qualifying period before an active alert expires. |
| `HAIL_OPERA_VOLUME_ENABLED` | `true` | Enable the optional Craiova volume-profile feed. It never changes primary tracking or projection; an enabled research agent receives its matching profile as experimental context. |
| `HAIL_LLM_ENABLED` | `false` | Enable the optional OpenAI web-researched hail-risk gate. Requires `OPENAI_API_KEY`. |
| `HAIL_LLM_MODEL` / `HAIL_LLM_REASONING_EFFORT` | `gpt-5.6-sol` / `high` | Responses API model and deliberate reasoning effort used for web research. |
| `HAIL_LLM_CANDIDATE_MINIMUM_DBZ` / `HAIL_LLM_MAXIMUM_LEAD_MINUTES` | `55` / `30` | Minimum radar band and target-entry lead time before a research request is made. |
| `HAIL_LLM_ALERT_THRESHOLD` | `7` | Send an alert only when the agent rating is strictly greater than this 0–10 value. |
| `HAIL_LLM_RESEARCH_COOLDOWN_MINUTES` | `30` | Per-track delay after an automatic request before that track can be sent to the agent again. Successful results are reused during the delay. |
| `HAIL_LLM_TIMEOUT_SECONDS` / `HAIL_LLM_MAX_OUTPUT_TOKENS` | `90` / `6000` | Bound the external research call's latency and response size. The output budget includes high-reasoning tokens. |
| `HAIL_TELEGRAM_ENABLED` | `false` | Enable Telegram delivery of eligible OPERA alert transitions. |
| `HAIL_TELEGRAM_BOT_TOKEN` / `HAIL_TELEGRAM_CHAT_ID` | unset | Required when Telegram is enabled. Keep the token secret. |
| `HAIL_ENV_FILE` | `.env` | Optional dotenv path; process environment values take precedence. |

## Verify

```powershell
.\.venv\Scripts\python.exe -m pytest
```

## Deploy to EC2

From Windows, [deploy.ps1](deploy.ps1) packages the tracked and non-ignored
working tree (including uncommitted changes), uploads it with the configured
EC2 PEM key, rebuilds the remote virtual environment, runs tests, and restarts
the API and poller. It uses the existing `hail-detector-api.service` and
`hail-detector-poller.service` systemd units when present, otherwise it uses
`start.sh`. It replaces all remote application files while preserving the
server's `.env` file.

```powershell
.\deploy.ps1 -RemotePath /home/ubuntu/hail-detector
```

The default destination is `ubuntu@ec2-54-76-118-84.eu-west-1.compute.amazonaws.com`.
Set `-RemoteHost` or `-RemoteUser` only when deploying elsewhere; provide the
actual deployed application directory in `-RemotePath`. Add `-SkipTests` only
when an emergency deployment cannot wait for the remote test suite.
