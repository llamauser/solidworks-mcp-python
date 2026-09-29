# Comparing free models

Run these prompts with each model, using the **solidworks** agent (the default in this folder).
To switch models, use **Models** on the page, or `/models` in OpenCode (Tools menu option 2).
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
- **Code instead of tools:** it tries to write a script. The agent blocks shell and file tools, but note it anyway.
- **Retry loops:** it repeats the same failing call instead of following `fix`.

## Scorecard

| Model | 1 | 2 | 3 | 4 | 5 | Notes |
|-------|---|---|---|---|---|-------|
| opencode/nemotron-3-ultra-free (default) | | | | | | |
| openrouter/qwen/qwen3.8-27b:free | | | | | | |
| openrouter/nvidia/nemotron-3-super-120b-a12b:free | | | | | | |
| openrouter/nvidia/nemotron-3.5-lightning:free | | | | | | |
| openrouter/google/gemma-4-31b-it:free | | | | | | |
| openrouter/inclusionai/ling-3.0-flash-fin:free | | | | | | |
| openrouter/thinkingmachines/inkling-small:free | | | | | | |

These are free OpenRouter models that supported tool calling on 2026-09-28. The list changes
often, so check openrouter.ai/models (filter: free, tools) for the current one.

## Build prompts (new part from a description)

Start a new session for each prompt. Pass means the model wrote a plan, the final
`get_model_summary` matches the request, and `bodies` is 1.

| # | Prompt | Expected result |
|---|--------|-----------------|
| B1 | `Make a 100 x 60 x 12 mm base plate with rounded corners (R8) and four 8 mm mounting holes, 10 mm in from each side.` | Size 100 x 12 x 60. Four through holes at x ±40, z ±20. |
| B2 | `Make an L-shaped bracket: base 80 x 40 x 6 mm, a vertical wall 50 mm tall and 6 mm thick on one long edge, and two 6.5 mm holes in the base.` | Size 80 x 50 x 40 (or 80 x 56 x 40 if the wall sits on top of the base). One body. |
| B3 | `Make a round flange: 90 mm diameter, 10 mm thick, a 30 mm center bore, and 6 holes of 8 mm on a 70 mm bolt circle.` | Size 90 x 10 x 90. Seven holes. The bolt-circle centers are 35 mm from the center, 60 degrees apart. |
| B4 | `A small electronics box: 70 x 40 x 25 mm outside, hollow with 2 mm walls and an open top, with 1 mm chamfers on the outside vertical edges.` | Outer box, then an inner pocket cut (x -33..33, y 2..25, z -18..18). Size 70 x 25 x 40. |
| B5 | `Make a simplified V-twin engine block: a 160 x 110 x 90 mm block with two 60 mm cylinder bores, one tilted 45 degrees left and one 45 degrees right, meeting at the crankshaft axis, and a 30 mm crankshaft bore along the length.` | One body. Two tilted cylinder cuts (rotate about z) and one cylinder cut along the block. |
| B6 | `Make a simple V4 engine as an assembly: block with 4 bores in two banks at 90 degrees, crankshaft, 4 pistons, and two cylinder heads. Save it as project "V4 engine 2".` | ONE `make_engine(project="V4 engine 2", layout="v", cylinders=4, bank_angle=90)` call: 12 parts, an assembly and 13 joints. |
| B7 | *(right after B6)* `Make it move: turn the crankshaft one full turn and tell me how far each piston travels. Then make a motion study at 600 rpm for 5 seconds.` | `move_mechanism(part="crankshaft")` reports each piston's travel (70 mm = the stroke), then `make_motion_study`. |
| B8 | `Make a hinge: a base plate 80x40x6 with two knuckles, a pin through them, and a flap with one knuckle between them. Project "Hinge". Then make it swing.` | Parts built with `save_as="Hinge/..."`, the pin coaxial with all knuckles; `make_assembly`, `connect_parts`, then `move_mechanism(part="flap", degrees=90)`. |
