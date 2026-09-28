# Comparing the free OpenCode models

Run these five prompts with each model, using the **solidworks** agent (press Tab in OpenCode).
Start each model from the same state: SolidWorks open, the smoke-test block (or any simple part)
open, and nothing selected. Mark a prompt as a pass only if the model called the expected tools
and reported the numbers the tools actually returned.

| # | Prompt | Expected tool calls | A pass means |
|---|--------|---------------------|--------------|
| 1 | `What version of SolidWorks is running and what file is open?` | `get_status` | It states the version and the file name from the result, with no invented details. |
| 2 | `Open C:\Temp\does_not_exist.SLDPRT` | `open_document` | It reports that the file was not found and does not retry in a loop. |
| 3 | *(click the top face first)* `How thick is the part I selected?` | `get_selection_context` | It gives the extrude depth in mm from `dims`. |
| 4 | `Make it 5 mm thicker.` | `get_selection_context` (optional), then `set_dimension` with the current value + 5 | The value is right, the dimension name was copied exactly, and it reports old → new. |
| 5 | `Export it as a STEP file to C:\Temp\test.step` | `save_document(save_as_path=...)` | It reports the exported path. If the file already exists, it asks before using overwrite=true. |

Watch for these failure patterns:
- **Wrong units:** it passes meters (0.015) instead of millimeters (15). The server flags suspicious jumps in a `warning` field.
- **Invented names:** it guesses `D1@Extrude1` instead of copying the name from `dims`.
- **Code instead of tools:** it tries to write a script. The agent blocks bash and edit, but note it anyway.
- **Retry loops:** it repeats the same failing call instead of following `fix`.

## Scorecard

| Model | 1 | 2 | 3 | 4 | 5 | Notes |
|-------|---|---|---|---|---|-------|
| MiMo-V2.6-Flash | | | | | | |
| Muse Spark 1.3 | | | | | | |
| Ling 3.0 Flash | | | | | | |
| Nemotron 3.5 Lightning | | | | | | |
| Nemotron 3 Ultra | | | | | | |
| Big Pickle | | | | | | |
