# sw_mcp: SolidWorks MCP server for small models

A pure-Python MCP server that lets an LLM inspect and edit the model open in SolidWorks.
It uses pywin32 COM and the official `mcp` SDK (v2); there is no C#, Node.js or VBA.
The tools are built for small and free models: few tools, flat arguments, units in the
argument names, and JSON answers that always carry a `fix` hint when something goes wrong.

It works with any recent SolidWorks (about 2020 onward) and with localized (non-English)
installs. It comes with `sw-agent`, which connects free AI providers (setup wizard) and
links the tools to OpenCode, VS Code Copilot, Gemini CLI, Claude Desktop and other MCP apps.

> **SolidWorks users (no coding needed):** read [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md).
> In short: double-click **Install SolidWorks Assistant.bat**, connect a free AI provider when
> asked, then double-click **SolidWorks Assistant** to open it in your browser. Everything else
> (checking SolidWorks, collecting logs, providers, updates) is in **SolidWorks Assistant - Tools.bat**.

## Setup on the SolidWorks PC

1. Get the project: `git clone https://github.com/llamauser/solidworks-mcp-python.git`, then open
   PowerShell in that folder and run:
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\install.ps1
   ```
   It installs Python if needed (via winget, after asking), creates `.venv`, installs everything,
   runs the unit tests, starts the **AI provider wizard** (below) and offers a desktop shortcut.
   It is safe to run again after every `git pull`. (`Install SolidWorks Assistant.bat` does the
   same with a double-click; `-Quiet` skips all questions.)
2. Check SolidWorks works end to end (Tools menu option 3, or `sw-agent check`, which runs
   the script below and then zips the logs). This starts SolidWorks if needed, edits a test block,
   then builds parts from scratch (holes, pockets, repeats, a flange from one plan), checking
   every step, all in a temp folder:
   ```powershell
   .venv\Scripts\python scripts\smoke_test.py
   ```
   Send back `smoke_test_report.txt`. To test with one of your own parts, add
   `--part "C:\path\part.SLDPRT"`. A copy is used, and you will be asked to click a face.

## Connecting AI models (the wizard)

`sw-agent setup` walks through AI providers one by one: OpenRouter, Google Gemini, Groq,
Mistral, NVIDIA, Cerebras and Hugging Face (free tiers), OpenAI (paid, pay per use), plus LM
Studio and Ollama for models running on your own PC. `sw-agent setup openai` sets up just one
provider. For each one it:
1. says what you get for free and **what happens to your data**. Some free tiers may train on
   your prompts; the wizard says so clearly, and you decide.
2. opens the page where you sign in and create a key;
3. takes the key (hidden while you type), checks it live, and finds which models can really use
   tools (one tiny request per model);
4. stores the key in **Windows Credential Manager**, never in a file.

Connect several: when one provider hits its daily limit, the assistant can use another.
`sw-agent status` shows what is connected. The provider list is `src/sw_agent/providers.toml`;
free offers change often, and updating that file needs no code change.

## The assistant

- **Browser:** `sw-agent web`, or double-click `SolidWorks Assistant.bat`. It is a local page
  (127.0.0.1 only, protected by a per-session token) with the chat, live progress, model
  switching and examples.
- **Terminal:** `sw-agent`. Type `/help` for its commands.
- **Model choice:** both try the connected models best-first and switch automatically when one
  is rate-limited or failing.
- **Benchmark:** `sw-agent bench` scores your connected models on three SolidWorks tasks
  against a fake SolidWorks (no SolidWorks needed), so the best ones are tried first.

## Using it from other AI apps

The SolidWorks tools are a standard MCP server, so any MCP app can use them.
`sw-agent connect` shows the steps for your apps.

| App | How |
|-----|-----|
| **OpenCode** | Run `opencode` in this folder. The **solidworks** agent opens by default; it can only use the SolidWorks tools. Its model is set in `.opencode/agents/solidworks.md`. OpenCode's own "Free" models reject setups with tools, so use an `openrouter/...:free` model (connect OpenRouter with `opencode auth login`). |
| **VS Code + GitHub Copilot** | Open this folder. `.vscode/mcp.json` starts the server; in Chat, pick the **SolidWorks** agent (`.github/agents/solidworks.agent.md`). |
| **Gemini CLI** | Run `gemini` in this folder. `.gemini/settings.json` starts the server, and `GEMINI.md` holds the instructions. |
| **Claude Desktop** | `sw-agent connect` adds the server to its config for you (a backup of the old config is kept). |
| **Cursor, Cline, LM Studio, Windsurf, ...** | `sw-agent connect` prints the `mcpServers` snippet to paste. |

All the apps get the same operating guide, `src/sw_mcp/guide.md`. Edit only that file, then
run `sw-agent sync` to regenerate each app's copy. A test checks that the copies are current.
Apps without their own instruction file receive the guide from the server itself
(`SW_MCP_INSTRUCTIONS=1`).

If a free model answers "temporarily rate-limited upstream", its provider is overloaded; switch
models. To compare models, use [docs/model-test-prompts.md](docs/model-test-prompts.md).

**Keeping usage low:**
- **Plan in one request:** `build_part` builds a whole part from one plan, instead of one
  request per feature.
- **OpenCode:** `compaction.prune` drops old tool outputs from the history.
- **Sessions:** start a new one for each new task, so old history isn't re-sent every turn.

Ask, for example:

> Make a 100 x 60 x 12 mm base plate with rounded corners (R8) and four 8 mm holes 10 mm in from each side.

## Tools

OpenCode shows each tool with the server name as a prefix, e.g. `solidworks_get_status`.

| Tool | Arguments | What it does |
|------|-----------|--------------|
| `get_status` | none | Connects to SolidWorks, starting it if closed, and reports the version, the active document and the active sketch. |
| `open_document` | `file_path` | Opens a .SLDPRT, .SLDASM or .SLDDRW. SolidWorks load errors come back in plain words. |
| `get_selection_context` | `max_items=5` | Describes what the user clicked: face (surface, normal, radius, area, owning feature and its dimensions), edge, dimension, mate, component or vertex. |
| `set_dimension` | `dimension_name`, `new_value` | Changes a dimension (mm or degrees) and rebuilds. If the rebuild breaks, the old value is restored automatically. |
| `save_document` | `save_as_path=""`, `overwrite=false` | Saves in place, or saves as / exports by extension (.step .stl .pdf .dxf .igs .x_t …). It refuses to overwrite a file unless told to. |
| `build_part` | `plan` (JSON), `start_new_part=true`, `save_as=""` | **Builds a whole part from one plan** in one call: boxes, cylinders (straight or slanted), prisms, revolves (any axis position), fillets/chamfers and repeats, and any box/cylinder/prism can be tilted with `rotate`. The plan is checked before anything is built, every step is verified, and the size is compared with `expect`. On failure it names the step. A part that comes out in separate pieces is refused (the steps are named) and not saved. `save_as="project/part"` saves it into `Documents\SolidWorks Assistant\project`, replacing an older version even if it is open. |
| `plan_machine` | `project`, `goal`, `parts` (one "name: description" per line), `notes` | Writes the plan of a multi-part machine into the project's shared design (`design.json`). Every tool keeps that file up to date (parts, sizes, shafts and bores, assembly, joints, failures), and the assistant puts its summary into every request, so whichever AI model answers continues the same plan. |
| `make_engine` | `project`, `layout` (inline/v/boxer), `cylinders`, `bank_angle`, `bore`, `stroke`, `heads` | **Builds a complete working engine in one call:** block, crankshaft, rods, pistons and heads, every shaft exactly in its bore, assembled and connected. `move_mechanism` then turns it with the exact slider-crank motion (each piston travels one stroke). |
| `make_assembly` | `project`, `parts="all"`, `name` | Puts a project's saved parts into one assembly. Parts are modeled in the machine's own coordinates, so they are placed at the origin with no mates. |
| `list_project` | `project=""` | Lists the parts and assemblies of a project (or all projects), to continue a multi-part job. |
| `manage_documents` | `action` (list/activate/close), `name`, `discard_unsaved=false` | Lists, activates or closes SolidWorks windows (`name="all"`, `"others"` = all but the active one, `"new"` = never-saved parts). Never closes unsaved work unless told to discard it. Parts saved with `build_part(save_as=...)` close their own window unless `keep_open=true`. |
| `connect_parts` | `fixed_part=""` | Turns an assembly into a mechanism: adds a concentric mate for every shaft that sits in a bore of another part (same axis, radius within 1 mm). Fixes the base part and every part with no joint. |
| `move_mechanism` | `part`, `degrees=360`, `steps=36` | Turns a part around its joint, live in SolidWorks, and reports how far every other part moved (e.g. a piston's stroke). Engines from `make_engine` move by their exact motion. |
| `make_motion_study` | `part`, `rpm=60`, `seconds=5`, `kind` (animation/basic) | Creates a SolidWorks Motion Study with a rotary motor, calculates and plays it. The motor constants are read from this PC's SolidWorks type libraries. |
| `new_part` | none | Creates an empty part from the default template. |
| `make_box` | `mode` (add/cut), `x/y/z_min_mm`, `x/y/z_max_mm` | Adds or cuts a block between world coordinates: plates, ribs, pockets, slots. |
| `make_cylinder` | `mode`, start and end centers (`start_x_mm` … `end_z_mm`), `diameter_mm` | Adds a boss or rod, or cuts a hole, along X, Y or Z, or slanted in a plane parallel to Front, Top or Right. |
| `make_prism` | `mode`, `axis`, `points_mm` ("a,b; a,b; …"), `start_mm`, `end_mm` | Adds or cuts any straight-sided outline (L, T, U, triangle) pushed along an axis. |
| `repeat_last_shape` | `copies`, `step_x/y/z_mm` | Copies the last shape in a row. A second repeat copies the whole group, so a grid takes two calls. |
| `repeat_last_shape_around` | `copies`, `angle_step_deg`, center | Copies the last shape or group around a circle: bolt circles, spokes. |
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
