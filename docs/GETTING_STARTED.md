# Getting started: SolidWorks Assistant

The SolidWorks Assistant builds and edits SolidWorks parts from plain-language descriptions,
for example "a 100 x 60 x 12 mm plate with four 8 mm holes". It runs on **OpenCode**, which
talks to AI models on the internet (free ones included); your SolidWorks files stay on your PC.

You don't need to know how to code.

## What you need

- A Windows PC with SolidWorks (2020 or newer).
- An internet connection.
- About 10 minutes for the first setup.

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
2. installs the SolidWorks tools;
3. installs **OpenCode**, or updates it to the newest version;
4. sets OpenCode up for SolidWorks (the "solidworks" agent);
5. asks whether to connect another AI provider (you can say no: OpenCode's own free models
   work without any account or key);
6. offers two desktop shortcuts: **SolidWorks Assistant** (to use it) and
   **SolidWorks Assistant - Tools** (everything else, see below).

### More AI models (optional)

OpenCode's free models are enough to start. If you have an account with another provider
(OpenRouter, OpenAI, Google Gemini, Groq ...), open the Tools menu and choose **5**: pick the
provider in the list and paste its key. OpenCode keeps the key. Some free services may use
what you type to train their AI; if your designs are confidential, check the provider's terms.

## 3. Use it

1. Open SolidWorks.
2. Double-click **SolidWorks Assistant** on your desktop (or in the folder).
3. OpenCode starts in the background and a page opens in your browser. Type what you want and
   press **Enter**.

While it works, you see each step ("Building the part...", "Saving..."), then a short answer.

### Good ways to ask

- **Give sizes in millimeters:** "80 x 50 x 8 mm plate".
- **Say where things go:** "4 holes of 6 mm, 10 mm in from each side".
- **Point at things:** click a face in SolidWorks, then write "make this 3 mm thicker".
- **Ask to save:** "save it as C:\Parts\plate.SLDPRT" or "export a STEP file".
- **Engines:** just ask: "make a V4 engine and show it running". The assistant builds the whole
  engine (block, crankshaft, rods, pistons, heads) in one go, puts it together and can turn it or
  make a motion study. It takes a few minutes in SolidWorks.
- **Other machines with several parts:** say so, and give the project a new name. Each part is saved
  into `Documents\SolidWorks Assistant\<project>`, and an assembly is made at the end.

If the result is not right, just say what to change: "the holes should be 8 mm, not 6".

### On the page

- **Steps:** every step line can be opened to see what was sent to SolidWorks (a part's plan is
  shown as numbered steps) and what came back.
- **Questions from OpenCode:** before something outside SolidWorks (for example a command on your
  PC) OpenCode asks first. Choose **Allow once**, **Always allow** or **Refuse**.
- **Stop:** stops the current request.
- **Models:** the AI models OpenCode can use. **Use** picks one for your next messages;
  **Agent's model** goes back to the default.
- **New conversation:** start fresh. It also uses less of your free quota.
- **Rating:** after a build a small card asks "How did it turn out?" (1-5 stars, quick tags,
  a comment). Every request is recorded on this PC (what you asked, the steps, which AI model did
  them, the result and a picture). Nothing is sent anywhere by itself.
- **Share builds:** packs those records and your ratings into one zip on your Desktop. Send it to
  the developer (for example upload it to the shared Google Drive folder). It contains no API keys,
  no file paths and no user names. Builds you rated 4 or 5 stars are also shown to the AI as
  examples when you ask for something similar.

### In a terminal instead

Tools menu option **2** opens OpenCode itself in the window, already set to the SolidWorks agent.
Type the same requests there. (It is OpenCode's normal interface: `/models` changes the model,
`/new` starts a new conversation.)

## The Tools menu

Double-click **SolidWorks Assistant - Tools** (on the desktop, or in the folder). It opens a
menu: type a number and press Enter. No commands to remember.

| Number | What it does |
|--------|--------------|
| 1 | Start the assistant in your browser |
| 2 | Start OpenCode in the menu window, with the SolidWorks agent |
| 3 | **Check SolidWorks works.** It builds a few test parts in a temp folder (your files are not touched), then packs the logs. Takes 1 to 3 minutes. |
| 4 | **Collect all logs.** It makes one zip on your Desktop, opens a window with it selected, and copies its path. Drag that zip into the chat with the developer. |
| 5 | Connect an AI provider in OpenCode (optional) |
| 6 | Show OpenCode's version, the connected providers and the models |
| 7 | Pack your build records and ratings to share |
| 8 | **Update everything:** the assistant and OpenCode (needs Git; otherwise download the ZIP again and run the installer) |

If the assistant is not installed yet, the menu installs it first.

## If something goes wrong

| You see | What to do |
|---------|-----------|
| "OpenCode is not installed" | Double-click **Install SolidWorks Assistant.bat**. |
| A model says "rate limited" or "no credits" | That model's limit is used up for now. Pick another under **Models**, or wait. |
| "SolidWorks is starting" | Wait 30 seconds and ask again. |
| "SolidWorks is busy" or no answer | Look at SolidWorks: close any open dialog box, then try again. |
| The page says it lost the connection | The black window was closed. Double-click **SolidWorks Assistant** again. |
| Anything else | Close everything and start again. If it keeps happening, open the **Tools** menu, choose **4** (collect logs) and send the zip to the project owner. |

Every conversation is recorded on this PC (in `%LOCALAPPDATA%\sw_mcp`) so problems can be
investigated. Tools menu option **4** (or **Collect logs.bat**) packs them, with OpenCode's own
logs, into one zip. It never contains your API keys.

## Good to know

- Free AI models are good at plates, brackets, flanges, housings and similar parts. Complex
  machines come out as simplified but correctly proportioned models.
- The assistant never deletes your files, and never overwrites one without asking.
- Only your messages (and the tool results) go to the AI provider. Your SolidWorks files are
  never uploaded.
