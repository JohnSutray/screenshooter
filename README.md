# Screenshooter

Snipping tool for **KDE Plasma on Wayland** that works like **Win+Shift+S** on Windows.

[Русская версия](README.ru.md)

![Editor](docs/editor.png)

- **Meta+Shift+S**: the screen freezes and you drag a region. The PNG is already in the clipboard.
- A thumbnail pops up in the corner over everything, even fullscreen games. Click it to annotate.
- **Shift+Print**: grab the whole screen, then the same thumbnail and editor.
- **Editor**: pen, arrow, rectangle, ellipse and text, any color and line width.
  Select, move, resize and delete anything you drew, with full undo.
- **Draw outside the image.** The canvas around the screenshot is free space. It only ends up
  in the result if you actually drew there:

  ![Export](docs/editor-export.png)

- **Cut a piece out of a screenshot**: press the region hotkey while the editor is focused.
- **Ctrl+C** copies the result, **Ctrl+S** saves it to `~/Pictures/Screenshots`.
- Hotkeys work in any keyboard layout. The UI is English or Russian, following your locale.

## Install

### Arch Linux, CachyOS, EndeavourOS, Manjaro (AUR)

```bash
yay -S screenshooter
```

Then run `screenshooter` once, or log out and back in. On the first start it registers its
global shortcuts in KDE. The keys are taken over from Spectacle if it holds them.

### From source, for the current user

Needs `python-gobject`, `python-cairo`, `gtk4-layer-shell`, `wl-clipboard`, `python-pillow`,
a C compiler and `pkg-config`.

```bash
git clone https://github.com/JohnSutray/screenshooter
cd screenshooter
./install.sh
```

Everything goes to `~/.local`. Remove it with `./install.sh --uninstall`.

## Usage

| Key | In the editor |
|---|---|
| `V` | Select: click a shape, drag it, drag a corner to resize, `Delete` to remove |
| `1` `2` `3` `4` | Pen (default), arrow, rectangle, ellipse |
| `5` | Text: click and type, `Enter` for a new line, `Esc` to finish. Drag a corner to scale it |
| `Ctrl+Z`, `Ctrl+Y` | Undo, redo |
| `Ctrl+C` | Copy the result as PNG |
| `Ctrl+S` | Save to `~/Pictures/Screenshots` |
| region hotkey | Cut a piece out of the current screenshot |
| `Esc` | Deselect, then close |

Color and line width also apply to the selected shape. Drag the window by the ⠿ handle or by
empty space on the toolbar, resize it from the bottom-right corner.

In the region overlay, `Esc` or a right click cancels.

## Configuration

Hotkeys live in `~/.config/screenshooter.conf`:

```ini
[hotkeys]
region = Meta+Shift+S
fullscreen = Shift+Print
```

Use KDE key syntax such as `Print`, `Ctrl+Alt+P` or `F15`, or `none` to disable an action.
Apply changes with:

```bash
screenshooter install-hotkey
```

F13 to F24 are handled too. The default keymap sends `XF86Launch5` and similar for them,
and Screenshooter registers whatever your keyboard actually produces.

## How it works

- `src/screenshooter.py`: the whole app in Python, GTK4 and cairo. It runs as a background
  instance, so a hotkey reacts instantly.
- The region overlay and the thumbnail are layer-shell surfaces, which keeps them above
  every window.
- Frames come from KWin's `org.kde.KWin.ScreenShot2` D-Bus interface through a tiny C helper,
  `src/grab.c`. KWin leaves the requesting client's own windows out of a screenshot. A separate
  process is what lets the editor appear in your next capture.
- Global shortcuts are registered with KDE's `kglobalaccel` over D-Bus, the same way System
  Settings does it.

## Requirements

KDE Plasma 6 on Wayland. Other desktops lack the KWin screenshot interface and `kglobalaccel`.

## License

[MIT](LICENSE)
