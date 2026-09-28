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
3. Run the end-to-end check. It starts SolidWorks if needed, builds a 40×20×10 mm test
   block in a temp folder, and exercises every tool:
   ```powershell
   .venv\Scripts\python scripts\smoke_test.py
   ```
   Send back `smoke_test_report.txt`. To test with one of your own parts, add
   `--part "C:\path\part.SLDPRT"`. A copy is used, and you will be asked to click a face.

## Using it from OpenCode

Run `opencode` in this folder. It reads `opencode.json` and starts the server itself over stdio.
In this folder, `opencode.json` restricts the default **build** agent to the SolidWorks tools.
Shell and file editing are denied, so the model cannot drift into writing its own COM scripts.
The working rules for the model are in `AGENTS.md`, which OpenCode loads automatically.
Pick a model and ask, for example:

> What is open in SolidWorks? … *(click a face)* … make this 5 mm thicker and export a STEP to C:\Temp\part.step

To compare the free models, use [docs/model-test-prompts.md](docs/model-test-prompts.md).

## Tools

OpenCode shows each tool with the server name as a prefix, e.g. `solidworks_get_status`.

| Tool | Arguments | What it does |
|------|-----------|--------------|
| `get_status` | none | Connects to SolidWorks, starting it if closed, and reports the version, the active document and the active sketch. |
| `open_document` | `file_path` | Opens a .SLDPRT, .SLDASM or .SLDDRW. SolidWorks load errors come back in plain words. |
| `get_selection_context` | `max_items=5` | Describes what the user clicked: face (surface, normal, radius, area, owning feature and its dimensions), edge, dimension, mate, component or vertex. |
| `set_dimension` | `dimension_name`, `new_value` | Changes a dimension (mm or degrees) and rebuilds. If the rebuild breaks, the old value is restored automatically. |
| `save_document` | `save_as_path=""`, `overwrite=false` | Saves in place, or saves as / exports by extension (.step .stl .pdf .dxf .igs .x_t …). It refuses to overwrite a file unless told to. |

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
| `SW_MCP_LOG` | `%LOCALAPPDATA%\sw_mcp\sw_mcp.log` | Log file (stdout is never used: it carries the protocol) |

To set these, add an `"environment": {...}` block to the server entry in `opencode.json`.
To serve over HTTP instead of stdio, run `python -m sw_mcp --http --port 8765` and use an
OpenCode `"type": "remote"` entry with `"url": "http://127.0.0.1:8765/mcp"`.

## Troubleshooting

- **"OpenCode's free tier can only be used from within OpenCode".** This is an OpenCode bug
  ([anomalyco/opencode#50806](https://github.com/anomalyco/opencode/issues/50806),
  [#49592](https://github.com/anomalyco/opencode/issues/49592)): free models only accept requests
  from the built-in `build` agent with OpenCode's own system prompt. That is why this project
  never replaces the agent prompt: the rules live in `AGENTS.md`, and `opencode.json` only
  sets tool permissions. Session titles and auto-compaction can hit the same error on free
  models; start a new session when a long one stops.

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
