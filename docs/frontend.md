# Frontend

loci ships two visual interfaces that both connect to the same HTTP/WS API on
port 7077.

## Hosted graph web UI (built-in)

The primary visual interface is a D3 force-directed graph served directly by
the loci server. No install step; no separate repo.

```bash
uv run loci server          # start the server
uv run loci graph serve     # prints the URL, opens in browser
```

See **[graph-ui.md](./graph-ui.md)** for the full feature reference, including
the left-sidebar chat, trace-of-thought visualization, clickable citations, and
the node editor panel.

---

## Loki Town (loki-frontend VSCode extension)

The interpretation graph lives in `loci`. The **town panel** that lets you
*see* and *interact with* it in VSCode lives in a separate repo —
[`loki-frontend`](https://github.com/<you>/loki-frontend) — published as a
VSCode extension called *Loki Town*.

Put loosely: `loci` is the database + agent, `loki-frontend` is the
window into it. They communicate over HTTP/WS on a single port.

This doc explains the connection model, how to install + run the
extension, and what each on-screen affordance does.

## Architecture

```
+---------------------+        +----------------------------+
|  loci server        |        |  VSCode (Extension Host)   |
|                     |        |                            |
|  FastAPI :7077      |  HTTP  |  TownPanel.ts              |
|  ├─ REST routes  ◄──┼────────┤  └─ LociClient.ts          |
|  ├─ WebSocket   ◄───┼────────┤      └─ fetch + WebSocket  |
|  └─ worker thread   |   WS   |                            |
|                     |        |  Webview (PixiJS town)     |
|  SQLite ~/.loci/    |        |  ◄──── postMessage ──────► |
+---------------------+        +----------------------------+
```

- The extension reads two settings: **`lokiTown.serverUrl`** (default
  `http://127.0.0.1:7077`) and **`lokiTown.projectId`** (the ULID of the
  project the panel should display).
- One Town panel ↔ one project. To switch projects, run **"Loci: Open
  Town"** again or change `lokiTown.projectId` in workspace settings.
- The webview itself is a Pixi-rendered town that talks to the extension
  host via `postMessage` — it never speaks to loci directly. All HTTP/WS
  goes through the host (`TownPanel.ts` + `LociClient.ts`).

## What you need running

Before opening the town, **the loci server must be up**. The extension
doesn't start it for you. From the `loci/` repo:

```bash
uv run loci server
# → Uvicorn running on http://127.0.0.1:7077
# → worker thread started
```

Leave that running in a terminal. Errors like *"graph fetch failed:
TypeError: fetch failed"* in the extension always mean the server isn't
reachable on `serverUrl`.

## Install + build the extension

```bash
git clone https://github.com/<you>/loki-frontend.git
cd loki-frontend
npm install
npm run build            # builds extension/ (esbuild) + webview/ (vite)
```

The build emits:
- `dist/extension/extension.js` — the VSCode entry point
- `dist/webview/` — the Pixi webview bundle the panel loads

For development:

```bash
npm run watch:extension   # rebuild extension/ on save
npm run watch:webview     # rebuild webview/ on save
```

Then in VSCode:

1. Open the `loki-frontend/` folder.
2. Press **F5** ("Run Extension"). VSCode launches an Extension Development
   Host window with Loki Town loaded.
3. In the dev host: Cmd+Shift+P → **"Loci: Open Town"**.

## First-run flow

When the user runs *Loci: Open Town* for the first time:

1. Extension reads `lokiTown.projectId` from settings. Empty → triggers
   the project picker.
2. Picker calls `GET /projects` on the loci server, lists slug + name.
3. User selects one. The id is written to workspace settings via
   `lokiTown.projectId`.
4. The webview panel opens. It immediately requests:
   - `GET /projects/:id/graph?include_raw=true` → 140-ish nodes + edges
   - `GET /projects/:id/communities` → district labels (empty until
     `loci absorb` has run)
   - `GET /projects/:id/pinned` → pedestals
   - `GET /projects/:id/anchors` → anchor tray
5. WebSocket subscribes to `WS /projects/:id/subscribe`. The first frame
   is `{type:"subscribed", channel:"project:<id>", seq:0}`. After that
   every node/edge mutation and every retrieve/draft fires a frame the
   panel reduces into its store.

If the webview hasn't been built yet, the panel falls back to a small HTML
note telling you to `npm run build`.

## What you see

| element              | what it represents                                                |
|----------------------|-------------------------------------------------------------------|
| villager (sprite)    | one node — colour by `subkind`, opacity by `confidence`           |
| pedestal             | a `pinned` node (touchstone)                                      |
| district             | a community from the latest snapshot                              |
| council plaza        | the focal area where retrieved nodes briefly converge             |
| villager walk        | a live `trace` event — node was retrieved or cited just now       |
| thinking bubble      | the node's `body` excerpt on hover                                |
| anchor tray (right)  | the active anchor set used as PPR seeds for retrieve/draft        |

A villager walking to the plaza, pausing 10s, then returning home is one
trace round-trip. If you `loci q` from a terminal while the panel is open,
you'll see the cited nodes animate seconds later — that's the live WS
feed.

## What you can do from the panel

| gesture                              | server effect                                                |
|--------------------------------------|--------------------------------------------------------------|
| right-click villager → "Pin"         | `POST /nodes/:id/pin?project_id=<pid>` — node becomes a touchstone |
| right-click villager → "Dismiss"     | `POST /nodes/:id/dismiss` — node moves to `dismissed`        |
| right-click villager → "Accept"      | `POST /nodes/:id/accept` — confidence +0.15 (rare; mostly housekeeping) |
| edit body in detail pane             | `PATCH /nodes/:id` — bumps `updated_at`, marks one-hop dirty |
| drag villager → villager             | `POST /edges` — proposes a typed edge                        |
| anchor tray → drag villagers in/out  | `POST /projects/:id/anchors` — sets the active-anchor set    |
| draft input (bottom)                 | `POST /projects/:id/draft` — runs the same path as `loci draft` |

All gestures route through `LociClient.ts`. None of them speak to SQLite
directly — everything is REST.

## Settings reference

```jsonc
{
  // Required if loci runs anywhere other than localhost:7077.
  "lokiTown.serverUrl": "http://127.0.0.1:7077",

  // Optional — pre-select the project so "Open Town" skips the picker.
  // Get the ULID from `uv run loci project list` or POST /projects's response.
  "lokiTown.projectId": "01KQ2AGY2T146QMDSF5QMFVJ7A"
}
```

## Walking through codoc end-to-end

Assuming you completed [getting-started.md](./getting-started.md) through
step 8 with the codoc example:

1. `uv run loci server` is running in one terminal.
2. In a second terminal: `cd ~/repos/loki-frontend && npm run watch:webview`
   (rebuilds when you tweak the webview).
3. VSCode is open on the `loki-frontend` folder; press **F5**.
4. In the dev host: Cmd+Shift+P → **"Loci: Open Town"**.
5. Picker → **Code-as-Document**. Panel opens with the codoc graph: ~131
   raw nodes (villagers tinted by modality — papers blue, code green, notes
   beige) and the kickoff questions clustered near them.
6. From the original terminal:
   ```bash
   uv run loci q codoc "how does CoDoc keep code and documentation in sync?"
   ```
   Watch villagers in the panel walk to the council plaza, pause, and
   return — those are the retrieved nodes.
7. Run a draft:
   ```bash
   uv run loci draft codoc "Compare Knuth, CoDoc, and codenav on the relation between code and prose."
   ```
   A few seconds after the draft prints, a new villager appears in the
   panel — the agent's reflection cycle just synthesized a `pattern` node.
8. Right-click that new pattern → **Pin**. It rises onto a pedestal and is
   used as a voice anchor for every subsequent reflection.

## Troubleshooting

**"Loki Town webview is not built yet"** — run `npm run build` in
`loki-frontend/`. The extension host loads `dist/webview/index.html`; if
it's missing the panel falls back to that static note.

**"graph fetch failed: TypeError: fetch failed"** — the loci server isn't
reachable. Check `uv run loci server` is running and that `lokiTown.serverUrl`
matches the port (default 7077).

**Picker shows nothing** — `GET /projects` returns empty. Either you haven't
created any projects yet (`uv run loci project create …`) or your loci data
dir is set somewhere unexpected (check `LOCI_DATA_DIR`).

**Nothing animates when I run `loci q`** — confirm the WS subscribed: the
panel logs `subscribed` to the dev console. If it's connected but quiet,
your retrieve might be hitting a stale project (different `projectId` than
the one selected in the panel).

**Webview can't load assets** — check the panel's CSP (Content-Security
Policy) errors in dev tools. The extension grants `localResourceRoots` for
`dist/webview/` and `assets/`; assets outside those paths get blocked.

## Where the source lives

```
loki-frontend/extension/src/
  extension.ts              # registers the "Loci: Open Town" command
  config.ts                 # reads lokiTown.serverUrl / projectId
  client/
    LociClient.ts           # REST wrapper (fetch)
    GraphSocket.ts          # WS wrapper (ws)
  commands/
    pickProject.ts          # the project picker quick-pick
  panel/
    TownPanel.ts            # the VSCode Webview panel; bridges REST/WS ↔ webview
  state/
    deltaReducer.ts         # WS frame → webview message translation

loki-frontend/webview/        # the Pixi-rendered town (separate vite build)
loki-frontend/shared/         # protocol DTOs shared with loci REST schemas
```

The protocol contract (`NodeDTO`, `EdgeDTO`, `CommunityDTO`, `TraceEventDTO`,
WS message envelopes) is defined under `loki-frontend/shared/protocol/`. Any
change to loci's REST/WS shapes that isn't reflected there will break the
webview's reducer — keep them in sync when you touch `loci/src/loci/api/`.
