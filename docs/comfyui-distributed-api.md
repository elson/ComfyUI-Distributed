# ComfyUI-Distributed API (Experimental)

This document describes the **public HTTP API** added to ComfyUI-Distributed to allow queueing *distributed* workflows from external tools (scripts, services, CI jobs, render farms, etc.) without using the ComfyUI web UI.

## Demo

- Video walkthrough: https://youtu.be/yiQlPd0MzLk

## Examples Repository

- Examples repo: https://github.com/umanets/ComfyUI-Distributed-API-examples.git

---

## Overview

### What this adds

- `POST /distributed/queue` — queues a workflow using the same distributed orchestration rules as the UI:
  - Detects distributed nodes in the prompt (`DistributedCollector`, `UltimateSDUpscaleDistributed`).
  - Resolves enabled/selected workers.
  - Probes and dispatches workers through `/distributed/worker_ws` by default.
  - If `settings.websocket_orchestration=false`, probes with `GET /prompt` and dispatches with `POST /prompt` instead.
  - Queues the master workflow in ComfyUI’s prompt queue.
  - If any `DistributedCollector` has `load_balance=true`, selects one least-busy participant for this run.

### What it does *not* add

- Authentication/authorization.
- A separate “job status” API for distributed results (you still use ComfyUI’s normal prompt history / websocket flow, and the existing `/distributed/queue_status/{job_id}` behavior for collector queues).

---

## Endpoint: `POST /distributed/queue`

Queue a workflow for distributed execution.

### URL

- `http://<master-host>:<master-port>/distributed/queue`

### Headers

- `Content-Type: application/json`

### Request Body

```json
{
  "prompt": { "<node_id>": { "class_type": "...", "inputs": { } } },
  "workflow": { },
  "client_id": "external-client",
  "delegate_master": false,
  "enabled_worker_ids": ["1", "2"],
  "workers": ["1", "2"],
  "auto_prepare": true,
  "trace_execution_id": "exec_1700000000_ab12cd"
}
```

#### Fields

- `prompt` (required unless `workflow.prompt` is present, object)
  - A complete ComfyUI API-format prompt graph, using the same shape as `POST /prompt`.
  - This is not the normal visual workflow export from the ComfyUI editor.
- `workflow` (optional, object)
  - Workflow metadata that ComfyUI normally stores in `extra_pnginfo.workflow`.
  - If you don’t care about UI metadata, you can omit it.
- `client_id` (required, string)
  - Passed through as `extra_data.client_id` (useful if you consume ComfyUI websocket events).
- `delegate_master` (optional, boolean)
  - If `true`, attempts “workers-only” execution for workflows based on `DistributedCollector`.
  - Current limitation: delegate-only mode **does not support** `UltimateSDUpscaleDistributed` and will fall back to running the full prompt on master.
- `enabled_worker_ids` (required, array of strings)
  - The explicit worker IDs to consider for this run.
- `workers` (optional, array of strings or objects with `id`)
  - Transitional alias for `enabled_worker_ids` used by older clients.
- `auto_prepare` (optional, boolean)
  - Kept for wire compatibility.
  - Backend orchestration always runs with auto-prepare semantics.
  - If top-level `prompt` is omitted, backend will attempt `workflow.prompt`.
- `trace_execution_id` (optional, string)
  - Passed through to orchestration logs.
  - Server log lines include the marker as `[exec:<trace_execution_id>]`.

##### How to get `enabled_worker_ids`

Worker IDs come from the plugin config (`GET /distributed/config`) under `workers[].id`.

Example (bash + `jq`):

```bash
curl -s "http://127.0.0.1:8188/distributed/config" \
  | jq -r '.workers[] | "id=\(.id)\tname=\(.name)\tenabled=\(.enabled)\thost=\(.host)\tport=\(.port)\ttype=\(.type)"'
```

Example (PowerShell):

```powershell
$cfg = Invoke-RestMethod "http://127.0.0.1:8188/distributed/config"
$cfg.workers | Select-Object id,name,enabled,host,port,type | Format-Table -AutoSize
```

### Response Body

```json
{
  "prompt_id": "<uuid>",
  "worker_count": 2,
  "auto_prepare_supported": true
}
```

- `prompt_id` — the master prompt id queued into ComfyUI.
- `worker_count` — number of workers that received a dispatched prompt (only those that passed the health check).

### Status Codes

- `200` — queued.
- `400` — invalid JSON or invalid body.
- `500` — orchestration/dispatch failure (see server logs for details).

---

## Worker requirements (important)

For a worker to participate, it must be reachable from the master. By default:

- WebSocket probe and dispatch: `<worker-base>/distributed/worker_ws` must accept the connection.

If `settings.websocket_orchestration=false`:

- Health check: `GET <worker-base>/prompt` must return HTTP 200.
- Dispatch: `POST <worker-base>/prompt` must accept the prompt.

Also, for collector-based flows:

- Workers will send results back to the master via `POST /distributed/job_complete` (that route must be reachable from workers).

---

## Endpoint: `POST /distributed/job_complete`

Submit one completed worker image back to the master collector queue.

### URL

- `http://<master-host>:<master-port>/distributed/job_complete`

### Request Body

```json
{
  "job_id": "exec_1234567890_17",
  "worker_id": "worker-1",
  "batch_idx": 0,
  "image": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAA...",
  "is_last": false
}
```

### Canonical envelope (required fields)

- `job_id` (string, required)
- `worker_id` (string, required)
- `batch_idx` (integer >= 0, required)
- `image` (string, required)
  - PNG payload as either:
    - data URL: `data:image/png;base64,...`
    - raw base64 PNG bytes
- `is_last` (boolean, required)

Legacy multipart/tensor payload formats are no longer accepted on this endpoint.

### CORS note

If you call the API from a browser (not from a backend), ensure the master ComfyUI is started with `--enable-cors-header`.

---

## Endpoint: `POST /distributed/job_complete_video`

Submit one finished video file back to the master collector queue, instead of the frames it
was made from. Used when a `Video Combine` is connected to the collector's `video` input: the
worker encodes the clip and sends only the file, which for a long sequence is orders of
magnitude less data than one base64 PNG per frame.

### URL

- `http://<master-host>:<master-port>/distributed/job_complete_video`

### Request Body

`multipart/form-data`, not JSON. The file is streamed in both directions, so neither end holds
the clip in memory.

| field | type | required | notes |
| --- | --- | --- | --- |
| `job_id` | text | yes | must appear **before** the `video` part |
| `worker_id` | text | yes | must appear **before** the `video` part |
| `basename` | text | no | suggested filename; sanitised to a bare basename with a video extension, and ignored otherwise |
| `md5` | text | no | verified against the received bytes; a mismatch is a 400 and the partial file is discarded |
| `is_last` | text | no | always treated as true - one file completes the worker |
| `video` | file | yes | the encoded clip |

### Response

```json
{
  "status": "success",
  "path": "/path/to/ComfyUI/output/clip_00001.mp4",
  "bytes": 12345678
}
```

The file is written into the master's output directory. It lands on a `.part` first and is
moved into place only once the whole body has arrived and the md5 matches, so a truncated
transfer is never mistaken for a finished render.

### Errors

- `400` - not multipart, missing `job_id`/`worker_id`, no file part, or md5 mismatch
- `404` - no collector is waiting on that `job_id`. The received file is kept.
- `413` - `Content-Length` above `COMFYUI_MAX_VIDEO_PAYLOAD_BYTES` (default 8 GiB)

---

## Log Endpoints

### `GET /distributed/worker_log/{worker_id}`

Read log files for workers launched locally by the master UI process manager.

- Intended for managed local workers.
- Query param: `lines` (optional, default `1000`).

### `GET /distributed/local_log`

Read this ComfyUI instance's in-memory runtime log buffer.

- Available on any ComfyUI-Distributed instance (master or worker).
- Query param: `lines` (optional, default `300`, max `3000`).

### `GET /distributed/remote_worker_log/{worker_id}`

Proxy endpoint on master that fetches logs from a configured remote/cloud worker's
`/distributed/local_log`.

- Intended for remote/cloud workers in master config.
- Query param: `lines` (optional, default `300`, max `3000`).

---

## Examples

### 1) Minimal `curl` request envelope

```bash
curl -X POST "http://127.0.0.1:8188/distributed/queue" \
  -H "Content-Type: application/json" \
  -d @payload.json
```

`payload.json` must contain a complete ComfyUI API-format prompt. The abbreviated envelope below illustrates the request shape but is not directly executable:

```json
{
  "prompt": {
    "<node_id>": {
      "class_type": "<node class>",
      "inputs": {"<required_input>": "<value or connection>"}
    }
  },
  "enabled_worker_ids": [],
  "client_id": "external-client"
}
```

Export or construct a valid API-format prompt with all required node inputs and at least one output node before submitting it.

### 2) Python (`requests`)

```python
import requests

url = "http://127.0.0.1:8188/distributed/queue"
payload = {
    "prompt": {...},
    "workflow": {...},
    "client_id": "external-client",
    "delegate_master": False,
    "enabled_worker_ids": ["1", "2"],
}

r = requests.post(url, json=payload, timeout=60)
r.raise_for_status()
print(r.json())
```

### 3) JavaScript (`fetch`)

```js
const url = "http://127.0.0.1:8188/distributed/queue";

const payload = {
  prompt: {/* ... */},
  workflow: {/* ... */},
  client_id: "external-client",
  delegate_master: false,
  enabled_worker_ids: ["1", "2"],
};

const resp = await fetch(url, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(payload),
});

if (!resp.ok) throw new Error(await resp.text());
console.log(await resp.json());
```

---

## Operational notes / gotchas

- If the workflow contains **no distributed nodes**, the endpoint falls back to normal master queueing and returns `worker_count: 0`.
- Worker selection is “best-effort”: offline workers are skipped.
- For public URLs/tunnels: prefer configuring `master.host` with an explicit scheme (`https://...`) to avoid ambiguity.

---

## Changelog (this feature)

- Added `POST /distributed/queue` endpoint.
- Added orchestration module used by the endpoint.
