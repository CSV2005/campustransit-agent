# Windows: exact PowerShell commands

Project root in every command below:

```
E:\campustransit--ai agent\campustransit-agent
```

Note the two hyphens and the space in `campustransit--ai agent` — **the path must be
quoted**. If you cloned elsewhere, change only the first line.

---

## 0. Open PowerShell at the project

```powershell
cd "E:\campustransit--ai agent\campustransit-agent"
```

If script execution is blocked:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

---

## 1. Python environment

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

`python --version` must report 3.10 or newer (3.11 recommended).

---

## 2. Configure (optional providers)

```powershell
Copy-Item .env.example .env -Force
notepad .env
```

Or append a single value without opening an editor:

```powershell
Add-Content -Path .env -Value "GOOGLE_MAPS_SERVER_API_KEY=AIza..."
Add-Content -Path .env -Value "OPENROUTESERVICE_API_KEY=..."
Add-Content -Path .env -Value "TOMTOM_API_KEY=..."
```

Confirm the file is ignored by git (it is, via `.gitignore`):

```powershell
git check-ignore -v .env
```

**Never** put a key in `localStorage`, a query string, or a commit. The browser key is the
only value that is public, and only because it is restricted by HTTP referrer.

Check the process picked up the configuration (no secret is returned):

```powershell
Invoke-RestMethod -Uri "http://localhost:7860/api/v1/config/health" | ConvertTo-Json -Depth 4
```

---

## 3. Build or refresh the data layer (only if `data\processed\transit.db` is absent)

```powershell
python -m src.build_all
```

This runs the D1–D4 importers and builds the policy index. It needs the raw extracts under
`data\raw\`; see README §6.

---

## 4. Start the local model (optional)

```powershell
ollama --version
ollama pull qwen2.5:3b            # or qwen2.5:1.5b / qwen2.5:0.5b on a small machine
ollama list
```

The desktop app normally keeps the server running; if you installed the CLI only, start it
in a second window:

```powershell
ollama serve
```

---

## 5. Start the backend

```powershell
# pick the model for this process only (does not edit .env)
$env:OLLAMA_MODEL = "qwen2.5:0.5b"
$env:CHAT_BUDGET_S = "150"

python run.py
```

Or explicitly with uvicorn:

```powershell
python -m uvicorn web.server:app --host 0.0.0.0 --port 7860
```

Open <http://localhost:7860>. The startup log prints one line per config problem and never
prints a key.

Smoke-check it from another PowerShell window:

```powershell
Invoke-RestMethod http://localhost:7860/api/health | ConvertTo-Json -Depth 3
Invoke-RestMethod http://localhost:7860/api/chat/health | ConvertTo-Json -Depth 3
(Invoke-WebRequest http://localhost:7860/api/v1/integrations).Headers["X-Response-Time-Ms"]
```

---

## 6. Test the integrations

Read the registry (free — this never calls a provider):

```powershell
$reg = Invoke-RestMethod http://localhost:7860/api/v1/integrations
$reg.summary
$reg.integrations | Select-Object id, state, state_label, reason | Format-Table -AutoSize
```

Free checks (Ollama, GTFS, tiles, policy index):

```powershell
Invoke-RestMethod -Method Post -Uri "http://localhost:7860/api/v1/integrations/ollama/test" |
  ConvertTo-Json -Depth 3
```

**Billable** checks refuse until you confirm. This is the refusal:

```powershell
try {
  Invoke-RestMethod -Method Post -Uri "http://localhost:7860/api/v1/integrations/google_places/test"
} catch {
  $_.Exception.Response.StatusCode.value__      # 409
  $_.ErrorDetails.Message
}
```

This is the probe — **one request, billed by Google**:

```powershell
Invoke-RestMethod -Method Post `
  -Uri "http://localhost:7860/api/v1/integrations/google_places/test" `
  -ContentType "application/json" `
  -Body '{"confirm_billable": true}' | ConvertTo-Json -Depth 4
```

Swap `google_places` for `google_geocoding`, `google_routes`, `google_traffic`,
`openrouteservice`, `tomtom`.

Disable a provider without touching `.env`:

```powershell
Invoke-RestMethod -Method Put `
  -Uri "http://localhost:7860/api/v1/integrations/tomtom/enabled" `
  -ContentType "application/json" -Body '{"enabled": false}'
```

---

## 7. Run the tests

```powershell
pip install pytest
python -m pytest tests -q
```

Expected: `172 passed, 1 skipped` (the skip is the live-model test; it runs when Ollama has
the configured model).

Live vehicle positions, backend only (protobuf fixtures, no network, no feed needed):

```powershell
python -m pytest tests\test_gtfs_rt.py -q
```

Expected: `24 passed`.

Frontend DOM tests (needs Node 18+):

```powershell
npm install jsdom playwright-core
node tests/test_map_dom.mjs
node tests/test_integrations_dom.mjs
```

Full browser smoke test:

```powershell
npx playwright install chromium
node tests/browser_smoke.mjs
```

Browser check of the live layer — run it twice, because both answers matter:

```powershell
# no feed configured: 0 markers, chip says Unavailable, the reason is on screen
node tests\browser_live.mjs http://localhost:7860

# with a feed: markers, chip says Live, stale ones drawn differently
node tests\browser_live.mjs http://localhost:7862
```

The second run needs the fixture feed from §10 running on port 8099 and a second app
instance on 7862.

---

## 7b. Live bus positions: there is nothing to switch on until you have a feed

BMTC does not publish an open vehicle-positions feed, so this section is either "do
nothing" or "use a credential you obtained yourself". Details and reasoning:
`docs\GTFS_REALTIME.md`.

**No feed (the normal case).** Skip it. The *Live buses* layer draws nothing and says
`Unavailable`; routes and fares are unaffected.

**With an authorised feed.** Add to `.env`, then restart the server:

```powershell
$env:GTFS_RT_VEHICLE_URL = "https://your-feed.example.gov/gtfs-rt/vehicle-positions"
$env:GTFS_RT_AUTH_HEADER = "Authorization: Bearer <token>"   # only if the feed needs it
$env:GTFS_RT_POLL_INTERVAL_S = "20"     # floor 15 s, enforced
$env:GTFS_RT_STALE_AFTER_S = "120"      # older than this -> "Delayed data"

# does the server see a readable feed, and how many buses are fresh?
curl.exe -s http://localhost:7860/api/transit/realtime-status

# the payload the map consumes
curl.exe -s "http://localhost:7860/api/transit/vehicle-positions?limit=5"
```

`200` with `available:false, kind:not_configured` = no URL set. `502` = a URL is set and
the feed could not be read; the `reason` says which failure it was.

**To try the plumbing without a credential** (fabricated data, clearly a local fixture):

```powershell
python tests\make_fixture_feed.py --out C:\temp\feed\vp.pb
# window 2
cd C:\temp\feed; python -m http.server 8099 --bind 127.0.0.1
# window 3
$env:GTFS_RT_VEHICLE_URL = "http://127.0.0.1:8099/vp.pb"
python -m uvicorn web.server:app --host 0.0.0.0 --port 7862
```

The strip will name `127.0.0.1` as the source, which is the point: the label tells you it is
not BMTC.

---

## 8. Stop everything

```powershell
# in the server window
Ctrl + C

# if a stale process holds the port
Get-NetTCPConnection -LocalPort 7860 -State Listen |
  Select-Object -ExpandProperty OwningProcess |
  ForEach-Object { Stop-Process -Id $_ -Force }
```

---

## 9. Common problems

| Problem | Command / fix |
|---|---|
| `Activate.ps1 cannot be loaded` | `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` |
| `ModuleNotFoundError: web` | you are not in the project root — `cd "E:\campustransit--ai agent\campustransit-agent"` |
| Port 7860 in use | the two-line stop command above, or `--port 7861` |
| `.env` changes ignored | the process reads it at import time — restart |
| Ollama OOM on 3b | `ollama pull qwen2.5:0.5b`, then set `OLLAMA_MODEL` |
| Chat says `deadline:` | raise `CHAT_BUDGET_S`, or use the 0.5b/1.5b model |
| `REQUEST_DENIED` after a test | billing or the API is not enabled in the Cloud project |
| Live buses greyed out on toggle | the SVG fallback basemap has no live layer — use OSM or Google |
| Live layer says `Unavailable` | no `GTFS_RT_VEHICLE_URL`; this is the expected state, see §7b |
| Live layer says 502 | a feed *is* configured and unreadable — read the `reason` in the strip |
