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
- [ ] **Waiting on the user:** run `install.ps1` and `scripts/smoke_test.py` on the SolidWorks PC, then paste back `smoke_test_report.txt`.

### Not yet verified on real SolidWorks (check these in the smoke report)
- Whether `IFace2.Normal` points out of the material. The smoke test prints the top-face normal as INFO.
- `OpenDoc7` + `GetOpenDocSpec`, `Extension.SaveAs` with a null-dispatch ExportData, and the `dim.SystemValue` setter under pywin32 late binding. Fallbacks exist for each.
- The `IDimension.FullName` format, and whether `Parameter()` accepts the first two `@` segments.
- The selection type numbers for mates, components and dimensions (21, 20, 14).

### Next batch after the smoke report
`new_part`, `create_sketch(plane: front|top|right)` (by tree position), `sketch_rectangle`, `sketch_circle`, `extrude`, `cut`, `fillet_selected_edges`, `list_mates`. `build_block()` in the smoke test is a working starting point for the modelling calls.

## 5. How to resume

Tell Claude: "Read prompt.md and continue the build from section 4."
