# Getting started: SolidWorks Assistant

The SolidWorks Assistant builds and edits SolidWorks parts from plain-language descriptions,
for example "a 100 x 60 x 12 mm plate with four 8 mm holes". It uses free AI models on the
internet; your SolidWorks files stay on your PC.

You don't need to know how to code.

## What you need

- A Windows PC with SolidWorks (2020 or newer).
- An internet connection.
- About 15 minutes for the first setup.

## 1. Get the files

Ask the project owner for access, then use either way:

- **With Git:** open PowerShell and run
  `git clone https://github.com/llamauser/solidworks-mcp-python.git`
- **Without Git:** on the GitHub page, click **Code → Download ZIP**, then unzip it to a folder
  such as `Documents\SolidWorks Assistant app`.

## 2. Install

Open the folder and double-click **Install SolidWorks Assistant.bat**.

A black window opens and:
1. installs Python if it is missing (it asks first);
2. installs the assistant;
3. starts the **AI provider setup**, described next;
4. offers two desktop shortcuts: **SolidWorks Assistant** (to use it) and
   **SolidWorks Assistant - Tools** (everything else, see below).

### Connecting AI providers

The assistant needs at least one AI provider. The providers below have free tiers. For each one
the setup window shows:

- what you get for free,
- what happens to your data. **Read this.** Some free services may use what you type to train
  their AI. If your designs are confidential, choose the providers marked "does not train on
  your data", or run a model on your own PC (LM Studio or Ollama).

Press **Enter** to set a provider up, or **s** to skip it. When you set one up:
1. A web page opens. Sign in (or create a free account) and create an **API key**. That is
   just a long password for programs; copy it.
2. Go back to the black window and paste the key (right-click, or Ctrl+V). It stays hidden.
3. The key is tested and saved safely in Windows Credential Manager.

Setting up **two or three** providers is best: when one reaches its daily free limit, the
assistant switches to another automatically. **OpenRouter** and **Groq** are good first choices.

You can add or change providers later: open the folder, then double-click
**Install SolidWorks Assistant.bat** again.

## 3. Use it

1. Open SolidWorks.
2. Double-click **SolidWorks Assistant** on your desktop (or in the folder).
3. A page opens in your browser. Type what you want and press **Enter**.

While it works, you see each step ("Building the part...", "Saving..."), then a short answer.

### Good ways to ask

- **Give sizes in millimeters:** "80 x 50 x 8 mm plate".
- **Say where things go:** "4 holes of 6 mm, 10 mm in from each side".
- **Point at things:** click a face in SolidWorks, then write "make this 3 mm thicker".
- **Ask to save:** "save it as C:\Parts\plate.SLDPRT" or "export a STEP file".
- **Machines with several parts:** say so, and give the project a name. For example: "make a
  simple V4 engine as an assembly, project name V4 engine". Each part is saved into
  `Documents\SolidWorks Assistant\V4 engine`, and an assembly is made at the end.

If the result is not right, just say what to change: "the holes should be 8 mm, not 6".

### Buttons on the page

- **New conversation:** start fresh. It also uses less of your free quota.
- **Models:** see which AI models are connected. Choose one to try first, or go back to
  automatic.

## The Tools menu

Double-click **SolidWorks Assistant - Tools** (on the desktop, or in the folder). It opens a
menu: type a number and press Enter. No commands to remember.

| Number | What it does |
|--------|--------------|
| 1 / 2 | Start the assistant in your browser, or in the menu window |
| 3 | **Check SolidWorks works.** It builds a few test parts in a temp folder (your files are not touched), then packs the logs. Takes 1 to 3 minutes. |
| 4 | **Collect all logs.** It makes one zip on your Desktop, opens a window with it selected, and copies its path. Drag that zip into the chat with the developer. |
| 5 / 6 | Connect or change AI providers, or see which ones are connected |
| 7 | Score the AI models on SolidWorks tasks (uses some free requests) |
| 8 | Update to the latest version (needs Git; otherwise download the ZIP again) |

If the assistant is not installed yet, the menu installs it first.

## If something goes wrong

| You see | What to do |
|---------|-----------|
| "No AI model is connected yet" | Double-click **Install SolidWorks Assistant.bat** and connect at least one provider. |
| "All connected models are busy or rate-limited" | The free limits are used up for now. Wait the time it says, or connect another provider. |
| "SolidWorks is starting" | Wait 30 seconds and ask again. |
| "SolidWorks is busy" or no answer | Look at SolidWorks: close any open dialog box, then try again. |
| The page says it lost the connection | The black window was closed. Double-click **SolidWorks Assistant** again. |
| Anything else | Close everything and start again. If it keeps happening, open the **Tools** menu, choose **4** (collect logs) and send the zip to the project owner. |

Every conversation is recorded on this PC (in `%LOCALAPPDATA%\sw_mcp`) so problems can be
investigated. Tools menu option **4** (or **Collect logs.bat**) packs them into one zip. It never contains
your API keys.

## Good to know

- Free AI models are good at plates, brackets, flanges, housings and similar parts. Complex
  machines come out as simplified but correctly proportioned models.
- The assistant never deletes your files, and never overwrites one without asking.
- Only your messages (and the tool results) go to the AI provider. Your SolidWorks files are
  never uploaded.
