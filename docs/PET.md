# Carlos Pet

Little guy. Big desk responsibilities.

Carlos Pet is a separate, lightweight companion in the Carlos UI build. It works
on KDE Plasma Wayland and can hang out even when the main assistant is stopped.
It doesn't start the core, load a model, use a microphone or call a cloud service.

## Get him on your desktop

Build the UI with the [setup guide](SETUP.md), including Qt DBus development
files and LayerShellQt. Then install just the pet:

```sh
sh carlos/scripts/install-pet.sh
carlos-pet
```

Or run `carlos/build/ui/ev-pet` straight from the checkout. The full Carlos user
installer includes the pet too. Both add a **Carlos Pet** app menu entry; the
control center's tray menu also has **Desktop Pet**. The standard Carlos installer
starts the pet, and the Carlos UI brings it back
at login through its existing startup entry. Turn off **Launch with Carlos** in
the pet menu if you don't want that. Installing only the pet leaves the running
assistant and login startup alone.

## Things he does

- Comments on the kind of app you're using: coding, browsing, games, music,
  drawing, chat, files or the terminal.
- Waits for an app category to settle for eight seconds. Automatic comments
  are at least 90 seconds apart and disappear after about eight seconds.
- Reacts when you click him. Drag his body to move him; he remembers the spot.
- Stays above windows, including fullscreen apps, by default. Screen locking
  and Carlos privacy modes still hide him. You can enable fullscreen hiding
  from the pet menu.
- Uses the running Carlos core's small panel status reply to show a thinking
  light and respect privacy mode. Older cores without the privacy field won't
  provide that signal; quiet mode and screen-lock hiding still work.

The pet starts on the screen under your mouse. Opening Carlos Pet again or
choosing **Show Carlos Pet** from the tray brings him to that screen. There is
also a **Move to screen** menu for picking a monitor directly.

Right-click the pet for **Quiet mode**, **Reduced motion**, **Hide during
fullscreen apps**, a 15-minute nap, position reset, or quit. The tray icon brings
him back after hiding or a nap. `carlos-pet --quit` also exits cleanly.

Quiet mode stops the comments, including pat replies. Reduced motion stops the
blinking and pat animation. The transparent area around him passes clicks through.
He never takes keyboard focus just by appearing.

## What the comments know

Comments use the active app's resource class from KWin. The pet maps it to a small
category and picks a local phrase. KWin also supplies the monitor name for placement
and pointer positions while you drag; the pointer listener stops when you let go.
It doesn't read window titles, documents,
browser history, screenshots or typed text. No activity history is saved.
Comments are playful guesses about the app category, not claims that he saw you
finish a task or understood what was on screen.

Only KWin's current D-Bus owner can submit app observations. If KWin is unavailable,
app-aware comments stop. A lock service that cannot answer leaves the pet hidden.
Privacy changes from a running core are checked every 15 seconds. No commands
are sent to the core. Opening the main Carlos window requires an explicit click.

## Settings and removing it

Preferences live in the Qt settings file `Carlos/DesktopPet.conf` under your XDG
config directory. To remove a pet-only install, quit it and remove these files:

- `~/.local/share/ev/app/bin/ev-pet`
- `~/.local/bin/carlos-pet`
- `~/.local/share/applications/carlos-pet.desktop`

Use your XDG data directory instead if customized. The pet installer prints a
backup directory with the previous files and a manifest for restoring them. Keep
that backup if you might want to undo an update. It never changes Plasma panels,
wallpaper, shortcuts or the existing login startup entries.
