# SolidWorks MCP Server: session handoff

Last updated: 2026-09-28. Read this first when resuming.

## 1. Original prompt (verbatim)

> I want to build a highly optimized, Python-based MCP server for SolidWorks. I have placed four reference implementations in the references/ directory (covering TS, C#, and Python).
>
> Your Task: Synthesize the best parts of these repos into a single, unified Python MCP server using win32com.client and the official Python mcp SDK.
>
> Architectural Rules:
>
> Foundation: Use pure Python (no C# DLLs, no Node.js). Handle COM threading properly with pythoncom.CoInitialize().
>
> Resilience (From the TS repo): Implement a 'Complexity Analyzer' or circuit-breaker. If a COM call fails, catch the error gracefully and return a clear string to the LLM rather than crashing the server.
>
> Context Builder (From the C# repo): Create a lightweight get_selection_context tool that reads dimensions, mates, or surface normals and returns them as a minimal JSON string, rather than dumping the whole feature tree.
>
> Target Audience (Crucial): This server will be consumed by small, free open-weight models (like Qwen 7B/32B). Therefore, the MCP @mcp.tool() schemas must be extremely atomic, flat, and heavily documented. Do not require the LLM to write raw VBA or Python macros.
>
> First Step: Review the reference files, propose a file structure for our new server, and list the first 5 atomic tools you plan to implement. Wait for my approval before coding.

## 2. User answers and decisions

- **SolidWorks is not installed on this dev PC.** Build everything here. The user tests on another PC that has SolidWorks.
- **Versions:** must work on *any recent* SolidWorks. The user mentioned a photo of their version, but it never came through.
- **Client:** **OpenCode** with its free hosted models, not Ollama. Their model list: MiMo-V2.6-Flash, Muse Spark 1.3, Ling 3.0 Flash, Nemotron 3.5 Lightning, Nemotron 3 Ultra, Big Pickle.
- **Machine:** OpenCode, the MCP server and SolidWorks all run on the same PC, so **stdio** is the default transport and HTTP is optional.
- **Approval:** the user approved the plan below ("go").

## 3. Approved plan

### What to take from each reference repo (`references/`, 3 folders, not 4)

- **solidworks-mcp (Python):**
  - Take: one dedicated worker thread for all COM calls, the pywin32 VARIANT/byref patterns, and reading mates by walking the MateGroup.
  - Avoid: tools that raise exceptions, and property-vs-method ambiguity.
- **SolidworksMCP-TS:**
  - Take: the circuit breaker (CLOSED → OPEN → HALF_OPEN), a mock SolidWorks for tests, errors returned as strings, and mm at the tool boundary.
  - Drop: VBA macro generation, and the parameter-count "complexity analyzer". It only existed because Node COM bridges break above ~12 arguments; pywin32 has no such limit.
- **mcp-server-solidworks (C# + Python):**
  - Take: selection readback (face normal, area, surface type; edge geometry), rounding to 1 µm, error codes with fix hints, never launching a second SolidWorks, forcing SolidWorks visible, and `structured_output=False`.
  - Drop: the state_version/idempotency guard, the feature-graph IR, and JSON-string parameters.

### Resilience

- **Pre-flight checks:** each tool declares what it needs (an open document, a part, an active sketch). Arguments are range-checked before any COM call.
- **Error classifier:** sorts COM errors (by HRESULT) into disconnected, busy, timeout, SolidWorks exception, API mismatch and internal.
- **Circuit breaker:** only counts infrastructure failures (disconnected, busy, timeout). Model mistakes such as "face not found" never trip it.
- **Retries:** only idempotent tools auto-retry. Write tools never do.
- **Error shape:** tools never raise. Every result is minified JSON, either `{"ok":true,...}` or `{"ok":false,"error":CODE,"message":...,"fix":...}`.

### Rules for small models (enforced by `tests/test_tool_schemas.py`)

- Parameters are only str, float, int, bool or Literal. No nested objects or JSON strings. At most 5 parameters per tool.
- Units go in parameter names (`depth_mm`, `angle_deg`).
- Docstrings follow: purpose → "Use when…" → one example. Each tool has a token budget.
- Everything is a tool, not a resource. Many clients ignore resources.
- **Tool names have NO `sw_` prefix.** OpenCode already prefixes them with the server name, giving `solidworks_get_status`.

### OpenCode specifics (checked against opencode.ai docs)

- **Local server:** add to `opencode.json` as `"mcp": {"solidworks": {"type": "local", "command": [...], "enabled": true, "environment": {...}, "timeout": 5000}}`. Remote servers use `"type": "remote", "url": ...`.
- **Tool names:** MCP tools are exposed as `<servername>_<tool>`.
- **Agent file:** a custom agent goes in `.opencode/agents/solidworks.md` with frontmatter `description`, `mode: primary`, `permission:` (`bash: deny`, `edit: deny`, `webfetch: deny`, `solidworks_*: allow`), plus instructions in the body. This stops the model from writing its own COM scripts.
- **Startup timeout:** OpenCode waits only about 5 s for the tool list, so the server must not touch SolidWorks at startup. Connect lazily on the first tool call.
- **Launching SolidWorks:** `get_status` should start SolidWorks in the background and reply "starting, call again in ~30 s" rather than blocking.

### Version independence

- Late binding only: no makepy or type-library dependency.
- Our own integer constants.
- The ProgID with no version number: `SldWorks.Application`.
- Only API calls present since about SW 2020, trying newer calls first and falling back to older ones.
- Default planes found by feature-tree position, so localized installs (French, German…) work.
- `get_status` reports the year and SP from `RevisionNumber()` (major + 1992 = year, e.g. 33 → 2025).

### File structure

```
MCPSolidworks/
├── pyproject.toml              # mcp, pywin32; entry point: python -m sw_mcp [--http --port 8765]
├── README.md                   # install, OpenCode config, test-PC checklist
├── opencode.json               # local MCP server entry
├── .opencode/agents/solidworks.md
├── install.ps1                 # creates the venv on the SolidWorks PC and prints the OpenCode snippet
├── docs/model-test-prompts.md  # 5 scripted prompts to compare the free models
├── src/sw_mcp/
│   ├── __main__.py  server.py  config.py
│   ├── core/  com_worker.py  connection.py  resilience.py  errors.py  com_utils.py
│   ├── sw/    constants.py  selection.py  geometry.py  (SolidWorks logic, no MCP code)
│   └── tools/ session.py  documents.py  context.py  dimensions.py
├── tests/  fakes/fake_sw.py  test_tool_schemas.py  test_resilience.py  test_com_worker.py  test_context.py  test_tools.py
└── scripts/smoke_test.py       # run on the SolidWorks PC → PASS/FAIL report to paste back
```

### First 5 tools: edit an existing model

| # | Tool | Parameters | Purpose |
|---|---|---|---|
| 1 | `get_status` | none | Attach, or launch once. Reports the SolidWorks version and SP, the active document and its type, the active sketch and the breaker state. "Call first and after any error." |
| 2 | `open_document` | `file_path` | Opens a part, assembly or drawing silently and decodes the load errors into plain words. |
| 3 | `get_selection_context` | `max_items=5` | Returns what the user clicked as small JSON: face (surface kind, normal, area, owning feature and its dimensions), cylinder (radius, axis), edge (kind, length, radius), dimension (name, value, unit), mate (type, components, value), component, vertex. |
| 4 | `set_dimension` | `dimension_name`, `new_value` | Takes a name like `D1@Boss-Extrude1` from tool 3, rebuilds, restores the old value if the rebuild fails, and returns old → new. |
| 5 | `save_document` | `save_as_path=""` (+ maybe `overwrite=False`) | Saves, or does Save As / export by file extension, and reports the save errors. |

Next batch after these: `new_part`, `create_sketch(plane)`, `sketch_rectangle`, `sketch_circle`, `extrude`, `cut`, `fillet_selected_edges`, `list_mates`, `export`.

### Testing

- **On this PC:** unit tests use a fake COM object tree. pywin32 is installed, so the real worker thread and CoInitialize run here.
- **On the SolidWorks PC:** `scripts/smoke_test.py` starts the real server over stdio, the way OpenCode will. It runs each tool, prints PASS/FAIL, and writes a report for the user to paste back.

## 4. Progress so far

- [x] Reviewed the 3 reference repos.
- [x] Proposed the plan and received approval.
- [x] Checked the OpenCode MCP and agent config format.
- [x] Created `.venv` (Python 3.12.0) with **mcp 2.2.0**, **pywin32 312**, pytest and pytest-asyncio.
- [x] Checked the mcp 2.2.0 API:
  - `FastMCP` was renamed to **`MCPServer`**: `from mcp.server import MCPServer`. There is no `mcp.server.fastmcp` any more.
  - `mcp.tool(name=, title=, description=, annotations=, icons=, meta=, structured_output=)`.
  - `mcp.run("stdio")` or `mcp.run("streamable-http", host=, port=)`.
  - **Sync tool functions run in a worker thread** via `anyio.to_thread.run_sync`, so a sync tool can block while waiting on the COM worker.
  - The tool schema comes from `inspect.signature(fn, eval_str=True)`, which honours `__signature__` and `__wrapped__`. The description comes from `fn.__doc__`.
  - Validation errors come back to the model as a ToolError automatically.
  - For tests: `from mcp import Client`; `async with Client(mcp_server_instance) as c: await c.call_tool(name, args)` connects in-process.
- [x] Wrote `pyproject.toml` and `src/sw_mcp/` (the core, `sw/`, the 5 tools, `server.py`, `__main__.py`). Installed with `pip install -e .`.
- [x] Wrote the tests (`tests/fakes/fake_sw.py`, plus test files for com_utils, com_worker, resilience, tools and tool schemas): **59 passing** (`.venv/Scripts/python -m pytest -q`).
- [x] Wrote `scripts/smoke_test.py`. Run here, it starts the real server over stdio in 1.5 s and reports NO_SOLIDWORKS cleanly.
- [x] Wrote `opencode.json`, `.opencode/agents/solidworks.md` (denies bash, edit, webfetch, websearch and task; allows `solidworks_*`), `install.ps1` (tested here: writes opencode.json as valid JSON with no BOM), `docs/model-test-prompts.md`, `README.md` and `.gitignore`.
- [x] Pushed to the private repo https://github.com/llamauser/solidworks-mcp-python (`references/` is excluded).
- [x] **Changed after the user's first OpenCode run:** OpenCode's free tier rejects custom agents with "OpenCode's free tier can only be used from within OpenCode" (anomalyco/opencode#50806, #49592; only `agent=build` is accepted). The custom agent `.opencode/agents/solidworks.md` was **removed**. `opencode.json` (and `install.ps1`) now reconfigure the built-in `build` agent instead: prompt `{file:./.opencode/solidworks-prompt.md}`, temperature 0.1, and the same deny/allow permissions. The notes above about a custom agent file are superseded.
- [x] **Smoke test on the user's SolidWorks PC: 15/15 passed** (2026-09-28). Setup: SOLIDWORKS **2024 SP1**, **French** install (the extrude is named `Boss.-Extru.1`), Python 3.14.7. The Windows user there is `rima`. The server started in 1.0 s, and tool calls took 10–1300 ms.
  - Verified: `IFace2.Normal` points **out of the material** (top face gives [0,1,0]). Opening, SaveAs export (STEP), Save, the `SystemValue` setter with rebuild, dimension naming (`D1@<feature>`), face and edge readback, and plane selection by tree position on a French install.
  - Still unverified: the selection-type readers for mates, components, dimensions and sketch entities (21, 20, 14, 10, 11), and assemblies in general.
- [x] The `build` override with a custom `prompt` still failed with the same free-tier error. The free model **worked in another folder but not in this project**. Working theory: the free tier rejects requests whose system prompt is not OpenCode's own (custom agents, `title` and `compaction` fail for the same reason). **Change:** removed the `prompt` and `temperature` override. The rules moved to `AGENTS.md`, which OpenCode loads automatically, and `opencode.json` now only sets `agent.build.permission`.
- [x] Bisect result: OpenCode's free tier only worked with no opencode.json at all, meaning it rejects requests from sessions that have MCP tools attached. **The user switched to OpenRouter** (connected in OpenCode).
- [x] **Current OpenCode setup (supersedes everything above):**
  - The custom agent `.opencode/agents/solidworks.md` is restored: `mode: primary`, `model: openrouter/nvidia/nemotron-3-super-120b-a12b:free` (switched from qwen3.8-27b:free, which was "rate-limited upstream" at its only provider, ModelRun), temperature 0.1, `steps: 12`. It denies every built-in tool and allows `solidworks_*`, and uses its own short prompt.
  - `opencode.json` (also written by install.ps1) sets `default_agent: solidworks`, `small_model: openrouter/liquid/lfm-2.5-2.6b:free`, `share: disabled`, `compaction.prune: true`, plus the mcp block.
  - AGENTS.md was deleted; it would duplicate the agent prompt.
  - The server no longer sends MCP `instructions` by default (`SW_MCP_INSTRUCTIONS=0`).
  - Goal: few requests and tokens on OpenRouter's free limits.
- [x] **Modeling tools added (the user wants description → plan → build → correct result):**
  - **Tools:** `new_part`, `make_box` (min/max per axis), `make_cylinder` (two end centers + diameter), `make_prism` (axis + "a,b; a,b" outline + start/end), `finish_edges` (fillet/chamfer by filter: vertical/top/bottom/parallel_x/parallel_z/circular/all), `undo_last_feature`, `get_model_summary`. Each shape tool has `mode` add/cut.
  - **Coordinates:** everything is in world mm (X right, Y up, Z toward viewer). The logic is in `src/sw_mcp/sw/modeling.py`, the wrappers in `tools/modeling.py`.
  - **Build flow:** select the default plane by tree position (Front=Z, Top=Y, Right=X) → map world points to sketch coordinates with SolidWorks' `ModelToSketchTransform` via MathUtility (a fallback table is used if that fails) → draw → FeatureExtrusion3 / FeatureCut4 with a start offset.
  - **Self-check:** each result is verified by `placement_ok()`: most of the new feature's face-box centers must lie in the requested region. A bad attempt is deleted (DeleteSelection2 absorbed) and retried with reversed direction or flipped offset (and, for the fallback table only, a mirrored axis). The log records the attempts.
  - **Tool schemas:** pydantic `title` fields are stripped (`server._slim_schemas`). There are 12 tools in about 10 KB of schema, and MAX_PARAMS is now 8.
  - **Agent:** it now plans (numbered plan with coordinates, worked example), `steps: 40`, and the order is base → added shapes → outer fillets → pockets/holes.
  - **Tests:** a fake modeler (`tests/fakes/fake_modeler.py`) with knobs `reverse_convention` / `mirror_second_axis` proves the self-correction. 78 tests pass.
  - **Smoke test:** `build_checks()` builds a plate with an R3 fillet, 4 holes, a pocket, a boss, a prism cut and an undo, and checks the volumes (expected about 20.9k mm³).
- [ ] **Waiting on the user:** run the smoke test again (the build section is new and unverified on real SolidWorks), then try the build prompts B1–B4 in docs/model-test-prompts.md.
- **Unverified on real SolidWorks:** MathUtility.CreatePoint with a VT_R8 array, FeatureCut4/FeatureExtrusion3 with a start offset, FeatureFillet3 with None arrays, IPartDoc methods (GetBodies2/GetPartBox) through late binding, IFeature.GetFaces, and IFace2.GetBox.

### Next batch after the smoke report
`new_part`, `create_sketch(plane: front|top|right)` (by tree position), `sketch_rectangle`, `sketch_circle`, `extrude`, `cut`, `fillet_selected_edges`, `list_mates`. `build_block()` in the smoke test is a working starting point for the modelling calls.

## 5. How to resume

Tell Claude: "Read prompt.md and continue the build from section 4."
