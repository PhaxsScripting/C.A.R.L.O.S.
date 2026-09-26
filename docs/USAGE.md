# Using Carlos

Start the core and UI with the [setup guide](SETUP.md). You can type in the control
center before setting up a microphone or voice model.

## Check things rq

From the repo root, with your venv active and the core running:

```sh
PYTHONPATH=carlos/core python3 -m ev.cli health
PYTHONPATH=carlos/core python3 -m ev.cli ask "what is my CPU usage?"
PYTHONPATH=carlos/core python3 -m ev.cli tools
PYTHONPATH=carlos/core python3 -m ev.cli events --limit 20
```

After a user install, `evctl` runs the same commands without the Python prefix.
`health` shows what's available. An available model still needs a real request
to check how well it runs on your hardware.

## Voice controls

These also work with `python3 -m ev.cli` from source:

| Command | What it does |
| --- | --- |
| `test-microphone` | Starts a microphone check |
| `test-transcription` | Starts a speech recognition check |
| `test-wake --seconds 30` | Opens a timed wake word test |
| `listen` / `stop-listening` | Starts or stops a manual capture |
| `resume-wake` / `pause-wake` | Enables or pauses wake listening |
| `stop-speaking` | Interrupts the current spoken reply |
| `privacy-on` / `privacy-off` | Turns voice privacy mode on or off |
| `latency` | Shows recent request timings |

If you get text back but no speech, check `health` for TTS availability and make
sure the configured Piper runtime and voice exist. Check the system output and
volume too. For missed speech, test the microphone first, then transcription,
then wake detection. That makes it easier to find which part is messing up.

## Tasks and permissions

Carlos can open apps, inspect the desktop, work with files, and run supported
tasks through registered tools. Some actions need approval. Read the target and
requested change before approving anything.

Use `plans` for plans, `agent-tasks` for task history, and `security` for the
current permission settings. `stop` shuts down the core. `--help` lists the rest
of the CLI commands.

## Keep your stuff local

Don't put keys, recordings, screenshots, memory databases or private logs in this
repo. The default provider is offline. Cloud providers need your own account and
configuration; read the relevant provider settings before enabling one.
Local speech and chat still need separately installed models.
