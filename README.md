# sw_mcp: SolidWorks MCP server for small models

A pure-Python MCP server that lets an LLM inspect and edit the model open in SolidWorks.
It uses pywin32 COM and the official `mcp` SDK (v2); there is no C#, Node.js or VBA.
The tools are built for small and free models: few tools, flat arguments, units in the
argument names, and JSON answers that always carry a `fix` hint when something goes wrong.

It works with any recent SolidWorks (about 2020 onward) and with localized (non-English)
installs.

## Setup on the SolidWorks PC

1. Install Python 3.10+ from python.org and tick "Add python.exe to PATH".
2. Copy this folder to the PC. Then, in PowerShell inside the folder, run:
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\install.ps1
   ```
   This creates `.venv`, installs the server, writes `opencode.json` with the right Python
   path, and runs the unit tests.
3. Run the end-to-end check. It starts SolidWorks if needed, edits a 40×20×10 mm test block,
   then builds a plate with fillets, holes, a pocket, a boss and an angled cut from scratch,
   checking every step. Everything happens in a temp folder:
   ```powershell
   .venv\Scripts\python scripts\smoke_test.py
   ```
   Send back `smoke_test_report.txt`. To test with one of your own parts, add
   `--part "C:\path\part.SLDPRT"`. A copy is used, and you will be asked to click a face.

## Using it from OpenCode

The setup uses free models through **OpenRouter**. Connect it once with `opencode auth login`
and pick OpenRouter; this needs an OpenRouter account and API key.

Run `opencode` in this folder. It reads `opencode.json`, starts the server over stdio, and opens
the **solidworks** agent (`.opencode/agents/solidworks.md`) by default. The agent:
- uses `openrouter/qwen/qwen3.8-27b:free`. To try another free model, change the `model:`
  line in the agent file; free OpenRouter model IDs end in `:free`.
- can only use the SolidWorks tools. Every built-in OpenCode tool (shell, file edits, web…) is
  denied, so the model cannot drift into writing its own COM scripts.
- uses a short system prompt of its own instead of OpenCode's long coding prompt.

Ask, for example:

> What is open in SolidWorks? … *(click a face)* … make this 5 mm thicker and export a STEP to C:\Temp\part.step

To compare the free models, use [docs/model-test-prompts.md](docs/model-test-prompts.md).

**Keeping usage low.** On OpenRouter's free models, every model turn counts as one request, and
each tool call adds a turn. The daily request limit is small unless the account has bought
credits; check OpenRouter's current limits. This setup is tuned to use few requests:
- **Agent:** it calls `get_status` only at the start and after errors, stops after 12 steps,
  and answers in one or two sentences.
- **OpenCode config:** `compaction.prune` drops old tool outputs from the history, and
  `small_model` sends session-title requests to a tiny free model.
- **Server:** it sends no connect-time instructions (`SW_MCP_INSTRUCTIONS=0`), because the
  agent file already carries the rules.
- **Sessions:** start a new one for each new task, so old history isn't re-sent every turn.

## Tools

OpenCode shows each tool with the server name as a prefix, e.g. `solidworks_get_status`.

| Tool | Arguments | What it does |
|------|-----------|--------------|
| `get_status` | none | Connects to SolidWorks, starting it if closed, and reports the version, the active document and the active sketch. |
| `open_document` | `file_path` | Opens a .SLDPRT, .SLDASM or .SLDDRW. SolidWorks load errors come back in plain words. |
| `get_selection_context` | `max_items=5` | Describes what the user clicked: face (surface, normal, radius, area, owning feature and its dimensions), edge, dimension, mate, component or vertex. |
| `set_dimension` | `dimension_name`, `new_value` | Changes a dimension (mm or degrees) and rebuilds. If the rebuild breaks, the old value is restored automatically. |
| `save_document` | `save_as_path=""`, `overwrite=false` | Saves in place, or saves as / exports by extension (.step .stl .pdf .dxf .igs .x_t …). It refuses to overwrite a file unless told to. |
| `new_part` | none | Creates an empty part from the default template. |
| `make_box` | `mode` (add/cut), `x/y/z_min_mm`, `x/y/z_max_mm` | Adds or cuts a block between world coordinates: plates, ribs, pockets, slots. |
| `make_cylinder` | `mode`, start and end centers (`start_x_mm` … `end_z_mm`), `diameter_mm` | Adds a boss or rod, or cuts a hole, along X, Y or Z. |
| `make_prism` | `mode`, `axis`, `points_mm` ("a,b; a,b; …"), `start_mm`, `end_mm` | Adds or cuts any straight-sided outline (L, T, U, triangle) pushed along an axis. |
| `finish_edges` | `kind` (fillet/chamfer), `size_mm`, `edges` (vertical/top/bottom/parallel_x/parallel_z/circular/all) | Rounds or bevels a group of edges. |
| `undo_last_feature` | none | Deletes the most recent feature. |
| `get_model_summary` | none | Size, min/max, volume, body count and feature list, to check a build against the request. |

### How building works

The model never draws sketches or picks sketch planes. It gives shapes in **world millimeters**
(X right, Y up, Z toward the viewer), and each tool does the SolidWorks steps: select the default
plane by its position in the tree, draw the profile, extrude or cut.

**Every result is checked.** The tool reads back the faces SolidWorks actually created and checks
that they sit where the shape was asked for. If SolidWorks built it in the wrong direction (or,
with the fallback sketch mapping, mirrored), the attempt is deleted and rebuilt the other way
automatically. Each result reports the part size, volume change and warnings (a separate body,
a cut that removed nothing), so the model can verify every step. The server log records how
many attempts each shape needed.

The **solidworks** agent writes a numbered plan with every coordinate first, runs it one tool
per step, and finishes with `get_model_summary` to compare against the request.

Every answer is minified JSON, either `{"ok":true,...}` or
`{"ok":false,"error":"CODE","message":"...","fix":"what to do next"}`.

## How it stays up

- **One COM thread.** All SolidWorks calls run on a single worker thread initialized with
  `pythoncom.CoInitialize()`. If SolidWorks hangs (usually a modal dialog), that thread is
  abandoned and a fresh one reconnects.
- **Circuit breaker.** Lost connections, busy rejections and timeouts count toward it; after 3,
  tools fail fast for 20 s with a message telling the user to check for a dialog box.
  The model's own argument mistakes never trip it. `get_status` always bypasses the breaker
  and is the recovery path.
- **Error classifier.** COM HRESULTs are mapped to plain codes: `SW_BUSY`, `SW_DISCONNECTED`,
  `UNSUPPORTED_BY_THIS_SOLIDWORKS` and so on.
- **Version independence.** The server uses late binding only (no type library), its own
  enum constants, and the newer API call first with older fallbacks. It never starts a
  second SolidWorks instance.
- **Fast startup.** The server never touches SolidWorks at startup, so OpenCode's 5-second
  tool-list timeout is safe. The connection is made on the first tool call.

## Settings (environment variables, all optional)

| Variable | Default | Meaning |
|----------|---------|---------|
| `SW_MCP_COM_TIMEOUT` | 30 | Seconds before one SolidWorks call is considered hung |
| `SW_MCP_BREAKER_THRESHOLD` | 3 | Connection failures before tools fail fast |
| `SW_MCP_BREAKER_COOLDOWN` | 20 | Seconds tools fail fast before retrying |
| `SW_MCP_AUTO_LAUNCH` | 1 | Let `get_status` start SolidWorks when it is not running |
| `SW_MCP_INSTRUCTIONS` | 0 | Send workflow instructions at connect time (for clients without an agent prompt) |
| `SW_MCP_LOG` | `%LOCALAPPDATA%\sw_mcp\sw_mcp.log` | Log file (stdout is never used: it carries the protocol) |

To set these, add an `"environment": {...}` block to the server entry in `opencode.json`.
To serve over HTTP instead of stdio, run `python -m sw_mcp --http --port 8765` and use an
OpenCode `"type": "remote"` entry with `"url": "http://127.0.0.1:8765/mcp"`.

## Troubleshooting

- **"OpenCode's free tier can only be used from within OpenCode".** You picked one of
  OpenCode's own free models (the "Free" ones in OpenCode's list), not an OpenRouter model.
  OpenCode's free tier currently rejects custom agents and MCP setups
  ([anomalyco/opencode#50806](https://github.com/anomalyco/opencode/issues/50806),
  [#49580](https://github.com/anomalyco/opencode/issues/49580)). Use an `openrouter/...:free`
  model instead.
- **HTTP 429 or "rate limit" from OpenRouter.** You hit the free requests-per-minute or
  requests-per-day limit. Wait, or add credits to the OpenRouter account.

- **`SW_STARTING` never ends.** SolidWorks is running but not visible to COM. Close any
  dialog box in SolidWorks. Then check that SolidWorks and OpenCode run as the same Windows
  user, and either both or neither with "Run as administrator".
- **`UNSUPPORTED_BY_THIS_SOLIDWORKS`.** An API call is missing in your release. Send the log
  file along with your SolidWorks version.
- **Details of any failure** are in the log file above, one line per tool call with timing.

## Development

```powershell
.venv\Scripts\python -m pytest -q
```
The tests run without SolidWorks. `tests/fakes/fake_sw.py` is an in-memory SolidWorks object
tree, and `tests/test_tool_schemas.py` enforces the small-model rules (flat arguments, at most
5 per tool, docstring template, size budget).

Layout: `src/sw_mcp/core` holds threading, the connection and resilience; `src/sw_mcp/sw`
holds the SolidWorks logic with no MCP code; `src/sw_mcp/tools` holds the thin MCP wrappers.
To add a tool, write the logic in `sw/`, add a wrapper decorated with `@sw_tool(needs=...)`
in `tools/`, and register it in `tools/__init__.py`.

Reference implementations this design draws from are in `references/`.
