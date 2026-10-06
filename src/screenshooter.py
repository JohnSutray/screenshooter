#!/usr/bin/env python3
"""Screenshooter: Win+Shift+S-style screen snipping for KDE Plasma on Wayland.

Flow:  hotkey -> the screen freezes -> drag a region -> the PNG goes to the clipboard
       and a thumbnail pops up in the corner -> click it to open a frameless editor
       (pen, arrow, rectangle, ellipse, text, select/move/resize, Ctrl+C, Ctrl+S).

The app runs as a daemon (Gio.Application + D-Bus activation), so further calls of
`screenshooter` only wake the running instance and the reaction is instant.
"""

import datetime
import io
import math
import os
import shutil
import subprocess
import sys
import threading

# Обёртка (bin/screenshooter) подгружает libgtk4-layer-shell через LD_PRELOAD: библиотека
# должна загрузиться раньше libwayland-client. В процессе она уже есть, а дочерним
# процессам (wl-copy, помощник снимка) переменная не нужна.
if os.environ.get("SCREENSHOOTER_PRELOAD") and os.environ.get("LD_PRELOAD") == os.environ["SCREENSHOOTER_PRELOAD"]:
    del os.environ["LD_PRELOAD"]
os.environ.pop("SCREENSHOOTER_PRELOAD", None)

APP_ID = "io.github.johnsutray.Screenshooter"


def register_with_portal():
    """Вне Flatpak портал не знает, кто мы: представляемся сами (xdg-desktop-portal ≥ 1.19).

    Звать до импорта Gtk: PyGObject инициализирует GTK 4 прямо при импорте, GTK тут же
    обращается к порталу, и соединение навсегда остаётся с пустым app ID.
    """
    if os.path.exists("/.flatpak-info"):
        return
    try:
        from gi.repository import Gio as _Gio, GLib as _GLib

        _Gio.bus_get_sync(_Gio.BusType.SESSION).call_sync(
            "org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop", "org.freedesktop.host.portal.Registry",
            "Register", _GLib.Variant("(sa{sv})", (APP_ID, {})), None, _Gio.DBusCallFlags.NONE, 3000, None,
        )
    except Exception as e:
        print("portal registry unavailable:", e, file=sys.stderr)


if __name__ == "__main__":
    register_with_portal()

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")

import cairo  # noqa: E402
from gi.repository import Gdk, Gio, GLib, GObject, Graphene, Gsk, Gtk, Pango, PangoCairo  # noqa: E402
try:
    gi.require_version("Gtk4LayerShell", "1.0")
    from gi.repository import Gtk4LayerShell as LayerShell  # noqa: E402
except (ValueError, ImportError):
    LayerShell = None  # без layer-shell работаем запасным путём (окно на весь экран + уведомление)

VERSION = "0.2.0"
DESKTOP_ID = APP_ID + ".desktop"
DESKTOP_ID_FULL = APP_ID + "-fullscreen.desktop"  # отдельный desktop-файл: kglobalaccel кэширует файл компонента

CONFIG_PATH = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "screenshooter.conf")
# Помощник для снимка (см. grab.c) лежит рядом со скриптом: KWin не включает в кадр окна
# самого запросившего процесса.
GRAB_HELPER = os.path.join(os.path.dirname(os.path.realpath(__file__)), "screenshooter-grab")

IN_FLATPAK = os.path.exists("/.flatpak-info")
# Для тестов и отладки: SCREENSHOOTER_BACKEND=portal всегда идёт через порталы,
# SCREENSHOOTER_NO_LAYER_SHELL=1 включает запасной путь без layer-shell.
FORCE_PORTAL = os.environ.get("SCREENSHOOTER_BACKEND") == "portal"
NO_LAYER_SHELL = os.environ.get("SCREENSHOOTER_NO_LAYER_SHELL") == "1"
# Сквозные тесты в CI: там KWin рисует без OpenGL и снимать экран не умеет, кадр даёт тест.
TEST_FRAME = os.environ.get("SCREENSHOOTER_TEST_FRAME")
MIN_GTK = (4, 12)


# --------------------------------------------------------------------------- i18n


def _ui_lang():
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        v = os.environ.get(var)
        if v:
            return v.split(":")[0][:2].lower()
    return "en"


_RU = {
    "Capture a screen region": "Снимок области экрана",
    "Capture the whole screen": "Снимок всего экрана",
    "Red": "Красный",
    "Orange": "Оранжевый",
    "Yellow": "Жёлтый",
    "Green": "Зелёный",
    "Blue": "Синий",
    "Purple": "Фиолетовый",
    "White": "Белый",
    "Black": "Чёрный",
    "Screenshots": "Снимки экрана",
    "Screenshot_%Y%m%d_%H%M%S.png": "Снимок экрана_%Y%m%d_%H%M%S.png",
    "Select an area  •  Esc to cancel": "Выделите область  •  Esc — отмена",
    "Close": "Закрыть",
    "Select (V): drag to move, corners to resize, Delete to remove": "Выделение (V): тащить, менять размер, Delete — удалить",
    "Pen (1)": "Карандаш (1)",
    "Arrow (2)": "Стрелка (2)",
    "Rectangle (3)": "Прямоугольник (3)",
    "Ellipse (4)": "Эллипс (4)",
    "Text (5): click to type, Enter for a new line": "Текст (5): клик — печатать, Enter — новая строка",
    "Screenshot": "Снимок экрана",
    "Close (Esc)": "Закрыть (Esc)",
    "Resize window": "Изменить размер окна",
    "Custom color": "Другой цвет",
    "Line width": "Толщина линии",
    "Undo (Ctrl+Z)": "Отменить (Ctrl+Z)",
    "Redo (Ctrl+Y)": "Вернуть (Ctrl+Y)",
    "Cut out a part of this screenshot": "Вырезать кусок снимка",
    "Copy": "Копировать",
    "Copy to clipboard (Ctrl+C)": "В буфер обмена (Ctrl+C)",
    "Save": "Сохранить",
    "Save to the Screenshots folder (Ctrl+S)": "В папку «Снимки экрана» (Ctrl+S)",
    "Drag window": "Перетащить окно",
    "✓ Copied to clipboard": "✓ Скопировано в буфер обмена",
    "✓ Saved: %s": "✓ Сохранено: %s",
    "Save failed: %s": "Ошибка сохранения: %s",
    "Couldn't take a screenshot: %s": "Не удалось сделать снимок экрана: %s",
    "✓ Cut out and copied": "✓ Вырезано и скопировано",
    "%s: disabled in the config": "%s: клавиша не назначена (так в конфиге)",
    "%s: can't parse %r (%s)": "%s: не понимаю %r (%s)",
    "Taking %s away from %s / %s": "Снимаю %s с %s / %s",
    "%s — %s: assigned": "%s — %s: назначена",
    "%s — %s: NOT assigned (%r)": "%s — %s: НЕ назначена (%r)",
    "Hotkeys removed.": "Горячие клавиши сняты.",
    "Screenshot copied": "Снимок скопирован",
    "Click to annotate": "Нажмите, чтобы порисовать",
    "Keep running so screenshot hotkeys react instantly": "Работать в фоне, чтобы клавиши снимков срабатывали мгновенно",
    "Shortcuts are managed by your desktop: change or remove them in System Settings.":
        "Клавишами управляет рабочий стол: менять и удалять их нужно в Параметрах системы.",
    "Shortcuts requested from the desktop; confirm them if it asks.":
        "Клавиши запрошены у рабочего стола; подтвердите, если он спросит.",
}
_TR = _RU if _ui_lang() == "ru" else {}


def tr(text):
    return _TR.get(text, text)


def debug(*args):
    if os.environ.get("SCREENSHOOTER_DEBUG"):
        print("[debug]", *args, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- hotkeys

# Действия для KGlobalAccel: ключ в конфиге → (desktop-файл, действие, клавиша по умолчанию, подпись).
HOTKEY_ACTIONS = {
    "region": (DESKTOP_ID, "_launch", "Meta+Shift+S", tr("Capture a screen region")),
    "fullscreen": (DESKTOP_ID_FULL, "_launch", "Shift+Print", tr("Capture the whole screen")),
}
DEFAULT_CONFIG = """[hotkeys]
# KDE key syntax: Meta+Shift+S, Shift+Print, Ctrl+Alt+P, F15 ... "none" disables an action.
# Apply changes with: screenshooter install-hotkey
region = Meta+Shift+S
fullscreen = Shift+Print
"""

# Коды клавиш Qt (QKeySequence как int): модификаторы | клавиша.
_QT_MODS = {"meta": 0x10000000, "win": 0x10000000, "super": 0x10000000, "ctrl": 0x04000000, "control": 0x04000000, "alt": 0x08000000, "shift": 0x02000000}
_QT_KEYS = {
    "escape": 0x01000000, "esc": 0x01000000, "tab": 0x01000001, "backspace": 0x01000003, "return": 0x01000004, "enter": 0x01000005,
    "insert": 0x01000006, "ins": 0x01000006, "delete": 0x01000007, "del": 0x01000007, "pause": 0x01000008, "print": 0x01000009,
    "sysreq": 0x0100000a, "home": 0x01000010, "end": 0x01000011, "left": 0x01000012, "up": 0x01000013, "right": 0x01000014,
    "down": 0x01000015, "pageup": 0x01000016, "pgup": 0x01000016, "pagedown": 0x01000017, "pgdown": 0x01000017,
    "space": 0x20, "menu": 0x01000055, "scrolllock": 0x01000026, "numlock": 0x01000025,
}


# Чему Qt (и KWin, у него та же таблица) сопоставляет keysym'ы, которые xkb вешает на F13–F24.
_XF86_TO_QT = {
    "XF86Tools": 0x010000F1, "XF86Reload": 0x010000E6, "XF86Mail": 0x010000A0, "XF86Shop": 0x010000BE,
    "XF86MailForward": 0x010000FB, "XF86MyComputer": 0x010000A2, "XF86Calculator": 0x010000A3,
}


def physical_fkey_name(n):
    """Имя keysym, которое физическая клавиша F13–F24 выдаёт в раскладке по умолчанию.

    В раскладке xkb (symbols/inet) клавиши FK13–FK18 выдают не F13–F18, а XF86Tools, XF86Launch5…
    Композиторы сравнивают глобальные шорткаты по keysym, поэтому «F15» в конфиге должна
    превращаться в то, что реально приходит с клавиатуры.
    """
    try:
        import ctypes

        x = ctypes.CDLL("libxkbcommon.so.0")
        x.xkb_context_new.restype = ctypes.c_void_p
        x.xkb_keymap_new_from_names.restype = ctypes.c_void_p
        x.xkb_keymap_new_from_names.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
        x.xkb_state_new.restype = ctypes.c_void_p
        x.xkb_state_new.argtypes = [ctypes.c_void_p]
        x.xkb_state_key_get_one_sym.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        x.xkb_keysym_get_name.argtypes = [ctypes.c_uint, ctypes.c_char_p, ctypes.c_size_t]
        ctx = x.xkb_context_new(0)
        km = x.xkb_keymap_new_from_names(ctx, None, 0)
        st = x.xkb_state_new(km)
        keycode = 183 + (n - 13) + 8  # KEY_F13 = 183 (evdev) + смещение X
        sym = x.xkb_state_key_get_one_sym(st, keycode)
        buf = ctypes.create_string_buffer(64)
        x.xkb_keysym_get_name(sym, buf, 64)
        return buf.value.decode() or "F%d" % n
    except Exception as e:
        print("xkbcommon unavailable (%s), treating F%d as a plain F-key" % (e, n), file=sys.stderr)
        return "F%d" % n


def physical_fkey_qt(n):
    """Qt-код для физической клавиши F13–F24 (для kglobalaccel)."""
    plain = 0x01000030 + n - 1
    name = physical_fkey_name(n)
    if name == "F%d" % n:
        return plain
    if name in _XF86_TO_QT:
        return _XF86_TO_QT[name]
    if name.startswith("XF86Launch") and len(name) == 11 and name[10] in "0123456789ABCDEF":
        return 0x010000A2 + int(name[10], 16)  # KWin: XF86LaunchN → Qt::Key_LaunchN (проверено нажатием)
    print("F%d produces %s in this keymap; no known Qt code, using plain F%d" % (n, name, n), file=sys.stderr)
    return plain


def parse_qt_key(text):
    """'Meta+Shift+S' / 'F15' / 'Print' → int в кодировке Qt. ValueError, если не разобрать."""
    parts = [p.strip() for p in text.replace(" ", "").split("+") if p.strip()]
    if not parts:
        raise ValueError("empty key combination")
    code = 0
    for mod in parts[:-1]:
        if mod.lower() not in _QT_MODS:
            raise ValueError("unknown modifier %r" % mod)
        code |= _QT_MODS[mod.lower()]
    key = parts[-1]
    kl = key.lower()
    if kl in _QT_KEYS:
        code |= _QT_KEYS[kl]
    elif len(kl) > 1 and kl[0] == "f" and kl[1:].isdigit() and 13 <= int(kl[1:]) <= 24:
        code |= physical_fkey_qt(int(kl[1:]))
    elif len(kl) > 1 and kl[0] == "f" and kl[1:].isdigit() and 1 <= int(kl[1:]) <= 35:
        code |= 0x01000030 + int(kl[1:]) - 1
    elif len(key) == 1:
        code |= ord(key.upper())
    else:
        raise ValueError("unknown key %r" % key)
    return code


# Имена keysym для клавиш из _QT_KEYS — для формата триггеров портала GlobalShortcuts.
_XDG_KEYS = {
    "escape": "Escape", "esc": "Escape", "tab": "Tab", "backspace": "BackSpace", "return": "Return", "enter": "Return",
    "insert": "Insert", "ins": "Insert", "delete": "Delete", "del": "Delete", "pause": "Pause", "print": "Print",
    "sysreq": "Sys_Req", "home": "Home", "end": "End", "left": "Left", "up": "Up", "right": "Right", "down": "Down",
    "pageup": "Page_Up", "pgup": "Page_Up", "pagedown": "Page_Down", "pgdown": "Page_Down", "space": "space",
    "menu": "Menu", "scrolllock": "Scroll_Lock", "numlock": "Num_Lock",
}
_XDG_MODS = {"meta": "LOGO", "win": "LOGO", "super": "LOGO", "ctrl": "CTRL", "control": "CTRL", "alt": "ALT", "shift": "SHIFT"}


def xdg_trigger(text):
    """'Meta+Shift+S' → 'LOGO+SHIFT+s' (формат preferred_trigger портала GlobalShortcuts)."""
    parts = [p.strip() for p in text.replace(" ", "").split("+") if p.strip()]
    if not parts:
        raise ValueError("empty key combination")
    mods = []
    for mod in parts[:-1]:
        if mod.lower() not in _XDG_MODS:
            raise ValueError("unknown modifier %r" % mod)
        mods.append(_XDG_MODS[mod.lower()])
    key = parts[-1]
    kl = key.lower()
    if kl in _XDG_KEYS:
        name = _XDG_KEYS[kl]
    elif len(kl) > 1 and kl[0] == "f" and kl[1:].isdigit() and 13 <= int(kl[1:]) <= 24:
        name = physical_fkey_name(int(kl[1:]))
    elif len(kl) > 1 and kl[0] == "f" and kl[1:].isdigit() and 1 <= int(kl[1:]) <= 35:
        name = "F%d" % int(kl[1:])
    elif len(key) == 1:
        name = kl
    else:
        raise ValueError("unknown key %r" % key)
    return "+".join(mods + [name])


def write_default_config():
    if not os.path.exists(CONFIG_PATH):
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write(DEFAULT_CONFIG)


def load_hotkeys():
    import configparser

    keys = {cfg: default for cfg, (_d, _a, default, _f) in HOTKEY_ACTIONS.items()}
    cp = configparser.ConfigParser()
    try:
        cp.read(CONFIG_PATH, encoding="utf-8")
        for cfg in keys:
            if cp.has_option("hotkeys", cfg):
                keys[cfg] = cp.get("hotkeys", cfg).strip()
    except Exception as e:
        print("Can't read %s: %s" % (CONFIG_PATH, e), file=sys.stderr)
    return keys

THUMB_MAX_W, THUMB_MAX_H = 360, 220
THUMB_TIMEOUT_MS = 7000

PALETTE = [
    ("#ff3b30", tr("Red")),
    ("#ff9500", tr("Orange")),
    ("#ffd60a", tr("Yellow")),
    ("#34c759", tr("Green")),
    ("#0a84ff", tr("Blue")),
    ("#bf5af2", tr("Purple")),
    ("#ffffff", tr("White")),
    ("#111111", tr("Black")),
]

CSS = b"""
window.shooter-transparent { background: transparent; }

.thumb-frame {
    background: #1b1b1d;
    border: 1px solid rgba(255,255,255,0.18);
    border-radius: 12px;
    padding: 6px;
    margin: 18px;
    box-shadow: 0 8px 28px rgba(0,0,0,0.65);
}
.thumb-close {
    min-width: 22px; min-height: 22px; padding: 0;
    border-radius: 11px;
    background: rgba(0,0,0,0.65);
    color: white;
    border: 1px solid rgba(255,255,255,0.25);
    margin: 10px;
}
.thumb-close:hover { background: #d33; }

.editor-frame {
    background: #151517;
    border: 1px solid rgba(255,255,255,0.14);
    border-radius: 10px;
}
.editor-toolbar {
    background: rgba(36,36,40,0.97);
    border: 1px solid rgba(255,255,255,0.13);
    border-radius: 14px;
    padding: 7px 10px;
    box-shadow: 0 8px 24px rgba(0,0,0,0.55);
}
.grip {
    color: rgba(255,255,255,0.55);
    font-size: 16px;
    padding: 1px 7px;
    margin-left: 2px;
    border-radius: 6px;
    background: rgba(255,255,255,0.07);
}
.grip:hover { color: white; background: rgba(255,255,255,0.15); }
.corner-close {
    color: rgba(255,255,255,0.6);
    font-size: 14px;
    min-width: 26px; min-height: 26px; padding: 0;
    border-radius: 7px;
    border: 1px solid rgba(255,255,255,0.08);
    background: rgba(255,255,255,0.07);
    box-shadow: none;
}
.corner-close:hover { color: white; background: #d33; border-color: #d33; }
.editor-toolbar button {
    min-height: 28px;
    min-width: 28px;
    padding: 0 9px;
    border-radius: 7px;
    border: 1px solid rgba(255,255,255,0.06);
    background: rgba(255,255,255,0.06);
    color: #e8e8e8;
    box-shadow: none;
    text-shadow: none;
    -gtk-icon-shadow: none;
}
.editor-toolbar button:hover { background: rgba(255,255,255,0.13); }
.editor-toolbar button:active { background: rgba(255,255,255,0.20); }
.editor-toolbar button.tool-btn { font-size: 15px; padding: 0 6px; }
.editor-toolbar button.tool-btn:checked {
    background: #0a84ff;
    border-color: #0a84ff;
    color: white;
}
.editor-toolbar button.primary { background: rgba(10,132,255,0.22); border-color: rgba(10,132,255,0.5); }
.editor-toolbar button.primary:hover { background: rgba(10,132,255,0.38); }
.editor-toolbar separator {
    background: rgba(255,255,255,0.10);
    min-width: 1px;
    margin: 4px 8px;
}
.editor-toolbar button.swatch {
    min-width: 18px; min-height: 18px;
    padding: 0; margin: 0 2px;
    border-radius: 9px;
    border: 2px solid rgba(255,255,255,0.18);
    background-clip: padding-box;
    box-shadow: none;
}
.editor-toolbar button.swatch:hover { border-color: rgba(255,255,255,0.6); }
.editor-toolbar button.swatch:checked { border-color: white; box-shadow: 0 0 0 2px #0a84ff; }
.editor-toolbar colorswatch, .editor-toolbar colorswatch > overlay {
    min-width: 18px; min-height: 18px;
    border-radius: 9px;
    border: 2px solid rgba(255,255,255,0.18);
    box-shadow: none;
}
.editor-toolbar scale { min-width: 90px; }
.editor-toolbar scale trough { min-height: 4px; background: rgba(255,255,255,0.15); }
.editor-toolbar scale highlight { background: #0a84ff; }
.editor-toolbar scale slider { min-width: 20px; min-height: 20px; background: white; box-shadow: 0 1px 3px rgba(0,0,0,0.5); }
.editor-title { color: #d8d8d8; font-weight: bold; margin: 0 10px 0 2px; }
.toast {
    background: rgba(10,132,255,0.95);
    color: white;
    font-weight: bold;
    padding: 8px 18px;
    border-radius: 20px;
    box-shadow: 0 4px 16px rgba(0,0,0,0.5);
}
.editor-title { color: #cfcfcf; font-weight: bold; margin: 0 8px; }
"""


# --------------------------------------------------------------------------- utils


def rgba(r, g, b, a=1.0):
    c = Gdk.RGBA()
    c.red, c.green, c.blue, c.alpha = r, g, b, a
    return c


def hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))


def grect(x, y, w, h):
    return Graphene.Rect().init(x, y, w, h)


def latin_key(display, keyval, keycode):
    """Keyval латинской раскладки для физической клавиши: Ctrl+C должен работать и в русской."""
    k = Gdk.keyval_to_lower(keyval)
    if k < 0x80:
        return k
    ok, _keys, keyvals = display.map_keycode(keycode)
    if ok:
        for kv in keyvals:
            kv = Gdk.keyval_to_lower(kv)
            if Gdk.KEY_a <= kv <= Gdk.KEY_z or Gdk.KEY_0 <= kv <= Gdk.KEY_9:
                return kv
    return k


def surface_to_texture(surface):
    """cairo ARGB32 (premultiplied, little-endian BGRA) -> Gdk.Texture."""
    surface.flush()
    fmt = surface.get_format()
    if fmt == cairo.FORMAT_ARGB32:
        mem = Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED
    elif fmt == cairo.FORMAT_RGB24:
        mem = Gdk.MemoryFormat.B8G8R8X8
    else:
        raise ValueError("unsupported cairo format %r" % fmt)
    data = GLib.Bytes.new(bytes(surface.get_data()))  # bytes(): PyGObject из memoryview делает это в 60 раз дольше
    return Gdk.MemoryTexture.new(surface.get_width(), surface.get_height(), mem, data, surface.get_stride())


def _rounded_rect(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def crop_surface(src, x, y, w, h):
    sw, sh = src.get_width(), src.get_height()
    x = max(0, min(sw - 1, x))
    y = max(0, min(sh - 1, y))
    w = max(1, min(sw - x, w))
    h = max(1, min(sh - y, h))
    out = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    cr = cairo.Context(out)
    cr.set_source_surface(src, -x, -y)
    cr.paint()
    out.flush()
    return out


def surface_to_png(surface):
    bio = io.BytesIO()
    surface.write_to_png(bio)
    return bio.getvalue()


def copy_to_clipboard(surface):
    """Кладёт картинку в буфер обмена через GTK.

    Работает, только пока у нас свежее событие ввода (клик, клавиша): так устроен Wayland.
    PNG и другие форматы GTK кодирует сам, лениво и в фоновом потоке, когда их попросят.
    """
    # Тип значения должен быть ровно Gdk.Texture (не MemoryTexture): сериализаторы
    # image/png и т.п. GTK ищет по точному GType.
    value = GObject.Value(Gdk.Texture, surface_to_texture(surface))
    Gdk.Display.get_default().get_clipboard().set_content(Gdk.ContentProvider.new_for_value(value))


def copy_to_clipboard_background(surface):
    """Для снимков без нашего ввода (всего экрана по клавише): GTK-буфер Wayland тогда не примет,
    а wl-copy, который работает через протокол data-control, примет. Кодируем в фоне."""
    display = Gdk.Display.get_default()
    if display is not None and display.get_name().startswith(":"):
        copy_to_clipboard(surface)  # X11: буфер можно занять в любой момент, wl-copy не нужен
        return
    wl_copy = shutil.which("wl-copy")
    if not wl_copy:
        print("wl-copy not found; the clipboard may stay unchanged", file=sys.stderr)
        copy_to_clipboard(surface)
        return
    texture = surface_to_texture(surface)

    def work():
        try:
            data = texture.save_to_png_bytes().get_data()  # GTK отпускает GIL, интерфейс не замирает
            # wl-copy уходит в фон держать буфер: не отдаём ему наши stdout/stderr.
            subprocess.run([wl_copy, "-t", "image/png"], input=data, check=True, timeout=10,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            print("wl-copy failed:", e, file=sys.stderr)

    threading.Thread(target=work, daemon=True).start()


def screenshots_dir():
    try:
        base = subprocess.run(["xdg-user-dir", "PICTURES"], capture_output=True, text=True, timeout=3).stdout.strip()
    except Exception:
        base = ""
    if not base or not os.path.isdir(base):
        base = os.path.expanduser("~/Pictures")
    path = os.path.join(base, tr("Screenshots"))
    os.makedirs(path, exist_ok=True)
    return path


def save_png(png_bytes):
    name = datetime.datetime.now().strftime(tr("Screenshot_%Y%m%d_%H%M%S.png"))
    path = os.path.join(screenshots_dir(), name)
    with open(path, "wb") as f:
        f.write(png_bytes)
    return path


# --------------------------------------------------------------------------- D-Bus and portals

PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
_portal_counter = [0]


def session_bus():
    return Gio.bus_get_sync(Gio.BusType.SESSION)


def bus_has_owner(name):
    try:
        res = session_bus().call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner",
            GLib.Variant("(s)", (name,)), GLib.VariantType.new("(b)"), Gio.DBusCallFlags.NONE, 1000, None,
        )
        return res.unpack()[0]
    except Exception:
        return False


def kde_capture_available():
    """Быстрый снимок через KWin: только вне песочницы и только в KDE."""
    return not IN_FLATPAK and not FORCE_PORTAL and bus_has_owner("org.kde.KWin")


def kde_hotkeys_available():
    return not IN_FLATPAK and not FORCE_PORTAL and bus_has_owner("org.kde.kglobalaccel")


def portal_request(iface, method, signature, args, options, callback):
    """Вызов метода портала, который отвечает сигналом Response. callback(code, results):
    code 0 — успех, 1 — пользователь отказался, 2 — ошибка."""
    bus = session_bus()
    _portal_counter[0] += 1
    token = "screenshooter%d_%d" % (os.getpid(), _portal_counter[0])
    sender = bus.get_unique_name()[1:].replace(".", "_")
    handle = "%s/request/%s/%s" % (PORTAL_PATH, sender, token)
    sub = [0]

    def on_response(_conn, _sender, _path, _iface, _signal, params):
        bus.signal_unsubscribe(sub[0])
        code, results = params.unpack()
        callback(code, results)

    sub[0] = bus.signal_subscribe(
        PORTAL_BUS, "org.freedesktop.portal.Request", "Response", handle, None, Gio.DBusSignalFlags.NONE, on_response
    )
    opts = dict(options)
    opts["handle_token"] = GLib.Variant("s", token)

    def on_reply(conn, res):
        try:
            conn.call_finish(res)
        except Exception as e:
            bus.signal_unsubscribe(sub[0])
            callback(2, {"error": str(e)})

    bus.call(
        PORTAL_BUS, PORTAL_PATH, iface, method, GLib.Variant(signature, tuple(args) + (opts,)),
        None, Gio.DBusCallFlags.NONE, -1, None, on_reply,
    )


# --------------------------------------------------------------------------- capture


class Capture:
    """Полный кадр рабочего пространства: cairo-поверхность + GPU-текстура + масштаб."""

    def __init__(self, buf, width, height, stride, fmt, scale):
        self.buf = buf  # bytearray, держим живым — на него смотрит cairo
        self.width, self.height, self.stride = width, height, stride
        cfmt = cairo.FORMAT_ARGB32 if fmt in (5, 6) else cairo.FORMAT_RGB24
        if fmt == 5:  # QImage::Format_ARGB32 (не premultiplied) — приведём на месте
            _premultiply_inplace(buf, width, height, stride)
        self.surface = cairo.ImageSurface.create_for_data(buf, cfmt, width, height, stride)
        self.texture = surface_to_texture(self.surface)
        self.scale = scale  # пикселей кадра на логическую единицу

    @classmethod
    def from_kwin(cls, include_cursor=False):
        bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        rfd, wfd = os.pipe()
        fdlist = Gio.UnixFDList.new()
        idx = fdlist.append(wfd)  # append делает dup — свой конец закрываем сразу
        os.close(wfd)
        opts = GLib.Variant(
            "a{sv}",
            {
                "include-cursor": GLib.Variant("b", include_cursor),
                "native-resolution": GLib.Variant("b", True),
            },
        )
        params = GLib.Variant.new_tuple(opts, GLib.Variant.new_handle(idx))
        try:
            reply, _ = bus.call_with_unix_fd_list_sync(
                "org.kde.KWin",
                "/org/kde/KWin/ScreenShot2",
                "org.kde.KWin.ScreenShot2",
                "CaptureWorkspace",
                params,
                None,
                Gio.DBusCallFlags.NONE,
                5000,
                fdlist,
                None,
            )
        except Exception:
            os.close(rfd)
            raise
        finally:
            del fdlist  # закрывает dup'нутый write-конец, иначе не будет EOF
        meta = reply.unpack()[0]
        w, h, stride, fmt = meta["width"], meta["height"], meta["stride"], meta["format"]
        need = stride * h
        buf = bytearray(need)
        view = memoryview(buf)
        got = 0
        try:
            while got < need:
                n = os.readv(rfd, [view[got:]])
                if n == 0:
                    break
                got += n
        finally:
            os.close(rfd)
        if got < need:
            raise RuntimeError("KWin returned truncated image (%d of %d bytes)" % (got, need))
        return cls(buf, w, h, stride, fmt, float(meta.get("scale", 1.0)))

    @classmethod
    def from_helper(cls, include_cursor=False):
        """Снимок через отдельный процесс, чтобы в кадр попали и наши окна (редактор, миниатюра)."""
        rfd, wfd = os.pipe()
        try:
            proc = subprocess.Popen(
                [GRAB_HELPER] + (["--cursor"] if include_cursor else []), stdout=wfd, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL
            )
        finally:
            os.close(wfd)
        try:
            line = proc.stderr.readline().decode("utf-8", "replace").strip()
            if not line.startswith("META "):
                rest = proc.stderr.read().decode("utf-8", "replace")
                raise RuntimeError("helper: %s %s" % (line, rest.strip()))
            w, h, stride, fmt, scale = line.split()[1:6]
            w, h, stride, fmt, scale = int(w), int(h), int(stride), int(fmt), float(scale)
            need = stride * h
            buf = bytearray(need)
            view = memoryview(buf)
            got = 0
            while got < need:
                n = os.readv(rfd, [view[got:]])
                if n == 0:
                    break
                got += n
        finally:
            os.close(rfd)
            proc.stderr.close()
            proc.wait(timeout=5)
        if got < need:
            raise RuntimeError("helper returned truncated image (%d of %d bytes)" % (got, need))
        return cls(buf, w, h, stride, fmt, scale)

    @classmethod
    def grab_kde(cls, include_cursor=False):
        """Быстрый путь KDE: помощник (в кадр попадают и наши окна), иначе KWin из этого процесса."""
        if os.access(GRAB_HELPER, os.X_OK):
            try:
                return cls.from_helper(include_cursor)
            except Exception as e:
                print("grab helper failed, capturing in-process:", e, file=sys.stderr)
        return cls.from_kwin(include_cursor)

    @classmethod
    def grab_async(cls, done, failed):
        """done(capture) или failed(message|None). KWin отвечает сразу, портал — асинхронно."""
        if TEST_FRAME:
            done(cls.from_png(TEST_FRAME))
            return
        if kde_capture_available():
            try:
                done(cls.grab_kde())
                return
            except Exception as e:
                print("KWin capture failed, trying the portal:", e, file=sys.stderr)

        def on_response(code, results):
            if code != 0:
                failed(None if code == 1 else results.get("error", "screenshot portal error %d" % code))
                return
            path = GLib.filename_from_uri(results["uri"])[0]
            cap = err = None
            try:
                cap = cls.from_png(path)
            except Exception as e:
                err = str(e)
            finally:
                # Портал сохраняет файл в «Изображения»: это наш временный кадр, убираем.
                try:
                    os.unlink(path)
                except OSError:
                    pass
            if cap is None:
                failed(err)
            else:
                done(cap)

        portal_request(
            "org.freedesktop.portal.Screenshot", "Screenshot", "(sa{sv})", ("",),
            {"interactive": GLib.Variant("b", False)}, on_response,
        )

    @classmethod
    def from_png(cls, path):
        src = cairo.ImageSurface.create_from_png(path)
        w, h = src.get_width(), src.get_height()
        dst = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
        cr = cairo.Context(dst)
        cr.set_source_surface(src, 0, 0)
        cr.paint()
        dst.flush()
        buf = bytearray(dst.get_data())
        return cls(buf, w, h, dst.get_stride(), 6, 1.0)

    def crop(self, x, y, w, h):
        return crop_surface(self.surface, x, y, w, h)


def _premultiply_inplace(buf, width, height, stride):
    for row in range(height):
        base = row * stride
        for px in range(width):
            i = base + px * 4
            a = buf[i + 3]
            if a != 255:
                buf[i] = buf[i] * a // 255
                buf[i + 1] = buf[i + 1] * a // 255
                buf[i + 2] = buf[i + 2] * a // 255


# --------------------------------------------------------------------------- selection overlay


class SelectCanvas(Gtk.Widget):
    """Замороженный экран с затемнением, перекрестием и рамкой выделения."""

    __gtype_name__ = "ShooterSelectCanvas"

    def __init__(self, overlay):
        super().__init__()
        self.overlay = overlay
        self.cap = overlay.cap
        self.cursor = None  # (x, y) в логических координатах виджета
        self.drag_start = None
        self.drag_cur = None
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_cursor(Gdk.Cursor.new_from_name("crosshair"))

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        motion.connect("leave", lambda *_: self._set_cursor(None))
        self.add_controller(motion)

        drag = Gtk.GestureDrag()
        drag.set_button(1)
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.add_controller(drag)

        rclick = Gtk.GestureClick()
        rclick.set_button(3)
        rclick.connect("pressed", lambda *_: self.overlay.app.cancel_capture())
        self.add_controller(rclick)

        self.layout = self.create_pango_layout("")
        self.layout.set_font_description(Pango.FontDescription.from_string("Sans Bold 11"))

    # --- input
    def _set_cursor(self, pos):
        self.cursor = pos
        self.queue_draw()

    def _on_motion(self, _c, x, y):
        self._set_cursor((x, y))

    def _on_drag_begin(self, _g, x, y):
        self.drag_start = (x, y)
        self.drag_cur = (x, y)
        self.overlay.app.selection_started(self.overlay)
        self.queue_draw()

    def _on_drag_update(self, _g, dx, dy):
        if self.drag_start is None:
            return
        self.drag_cur = (self.drag_start[0] + dx, self.drag_start[1] + dy)
        self.cursor = self.drag_cur
        self.queue_draw()

    def _on_drag_end(self, _g, dx, dy):
        if self.drag_start is None:
            return
        self.drag_cur = (self.drag_start[0] + dx, self.drag_start[1] + dy)
        rect = self.selection()
        self.drag_start = self.drag_cur = None
        if rect and rect[2] >= 3 and rect[3] >= 3:
            self.overlay.app.finish_capture(self.overlay, rect)
        else:
            self.queue_draw()

    def selection(self):
        if self.drag_start is None or self.drag_cur is None:
            return None
        (x0, y0), (x1, y1) = self.drag_start, self.drag_cur
        w, h = self.get_width(), self.get_height()
        x0, x1 = max(0, min(w, x0)), max(0, min(w, x1))
        y0, y1 = max(0, min(h, y0)), max(0, min(h, y1))
        return (min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0))

    # --- painting
    def do_snapshot(self, snap):
        w, h = self.get_width(), self.get_height()
        cap, s = self.cap, self.cap.scale
        ox, oy = self.overlay.origin

        snap.push_clip(grect(0, 0, w, h))
        snap.append_texture(cap.texture, grect(-ox, -oy, cap.width / s, cap.height / s))
        snap.pop()

        dim = rgba(0, 0, 0, 0.45)
        sel = self.selection()
        if sel is None:
            snap.append_color(dim, grect(0, 0, w, h))
        else:
            x, y, sw, sh = sel
            snap.append_color(dim, grect(0, 0, w, y))
            snap.append_color(dim, grect(0, y + sh, w, h - y - sh))
            snap.append_color(dim, grect(0, y, x, sh))
            snap.append_color(dim, grect(x + sw, y, w - x - sw, sh))
            rr = Gsk.RoundedRect()
            rr.init_from_rect(grect(x - 1, y - 1, sw + 2, sh + 2), 0)
            snap.append_border(rr, [1.5] * 4, [rgba(1, 1, 1, 0.95)] * 4)
            self._label(snap, "%d × %d" % (round(sw * s), round(sh * s)), x, y + sh + 8, w, h)

        if self.cursor is not None and sel is None:
            cx, cy = self.cursor
            line = rgba(1, 1, 1, 0.55)
            snap.append_color(line, grect(0, cy, w, 1))
            snap.append_color(line, grect(cx, 0, 1, h))
        if sel is None:
            self._label(snap, tr("Select an area  •  Esc to cancel"), None, 24, w, h)

    def _label(self, snap, text, x, y, w, h):
        self.layout.set_text(text, -1)
        _, logical = self.layout.get_pixel_extents()
        pad = 6
        bw, bh = logical.width + pad * 2, logical.height + pad * 2
        if x is None:  # по центру
            x = (w - bw) / 2
        x = max(4, min(w - bw - 4, x))
        y = max(4, min(h - bh - 4, y))
        rr = Gsk.RoundedRect()
        rr.init_from_rect(grect(x, y, bw, bh), 6)
        snap.push_rounded_clip(rr)
        snap.append_color(rgba(0, 0, 0, 0.75), grect(x, y, bw, bh))
        snap.pop()
        snap.save()
        snap.translate(Graphene.Point().init(x + pad - logical.x, y + pad - logical.y))
        snap.append_layout(self.layout, rgba(1, 1, 1, 1))
        snap.restore()


class Overlay(Gtk.Window):
    """Полноэкранное окно на слое overlay для одного монитора."""

    def __init__(self, app, cap, monitor):
        super().__init__(application=app, title="Screenshooter overlay")
        self.app, self.cap = app, cap
        geo = monitor.get_geometry()
        self.origin = (geo.x, geo.y)
        self.monitor = monitor

        if app.layer_shell:
            LayerShell.init_for_window(self)
            LayerShell.set_namespace(self, "screenshooter-select")
            LayerShell.set_layer(self, LayerShell.Layer.OVERLAY)
            for edge in (LayerShell.Edge.TOP, LayerShell.Edge.BOTTOM, LayerShell.Edge.LEFT, LayerShell.Edge.RIGHT):
                LayerShell.set_anchor(self, edge, True)
            LayerShell.set_exclusive_zone(self, -1)
            LayerShell.set_keyboard_mode(self, LayerShell.KeyboardMode.EXCLUSIVE)
            LayerShell.set_monitor(self, monitor)
        else:
            # Запасной путь (например, GNOME): обычное окно на весь экран нужного монитора.
            self.set_decorated(False)
            self.fullscreen_on_monitor(monitor)

        self.canvas = SelectCanvas(self)
        self.set_child(self.canvas)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)

    def _on_key(self, _c, keyval, _code, _state):
        if keyval == Gdk.KEY_Escape:
            self.app.cancel_capture()
            return True
        return False


# --------------------------------------------------------------------------- thumbnail


class Thumbnail(Gtk.Window):
    """Миниатюра свежего снимка в углу экрана, поверх всего. Клик — редактор."""

    def __init__(self, app, shot, monitor):
        super().__init__(application=app, title="Screenshooter thumbnail")
        self.app, self.shot = app, shot
        self.add_css_class("shooter-transparent")
        self.timer = None

        LayerShell.init_for_window(self)
        LayerShell.set_namespace(self, "screenshooter-thumb")
        LayerShell.set_layer(self, LayerShell.Layer.OVERLAY)
        LayerShell.set_anchor(self, LayerShell.Edge.BOTTOM, True)
        LayerShell.set_anchor(self, LayerShell.Edge.RIGHT, True)
        LayerShell.set_margin(self, LayerShell.Edge.BOTTOM, 12)
        LayerShell.set_margin(self, LayerShell.Edge.RIGHT, 12)
        LayerShell.set_keyboard_mode(self, LayerShell.KeyboardMode.NONE)
        if monitor is not None:
            LayerShell.set_monitor(self, monitor)

        iw, ih = shot.surface.get_width(), shot.surface.get_height()
        k = min(THUMB_MAX_W / iw, THUMB_MAX_H / ih, 1.0)
        tw, th = max(64, int(iw * k)), max(40, int(ih * k))

        # Уменьшаем заранее: естественный размер Gtk.Picture — размер текстуры,
        # а окно layer-shell растёт до естественного размера содержимого.
        small = cairo.ImageSurface(cairo.FORMAT_ARGB32, tw, th)
        cr = cairo.Context(small)
        cr.scale(tw / iw, th / ih)
        cr.set_source_surface(shot.surface, 0, 0)
        cr.get_source().set_filter(cairo.FILTER_GOOD)
        cr.paint()
        pic = Gtk.Picture.new_for_paintable(surface_to_texture(small))
        pic.set_content_fit(Gtk.ContentFit.CONTAIN)
        pic.set_can_shrink(False)
        pic.set_size_request(tw, th)

        frame = Gtk.Box()
        frame.add_css_class("thumb-frame")
        frame.append(pic)
        frame.set_cursor(Gdk.Cursor.new_from_name("pointer"))

        close = Gtk.Button(label="✕")
        close.add_css_class("thumb-close")
        close.set_halign(Gtk.Align.END)
        close.set_valign(Gtk.Align.START)
        close.set_tooltip_text(tr("Close"))
        close.connect("clicked", lambda *_: self.dismiss())

        ov = Gtk.Overlay()
        ov.set_child(frame)
        ov.add_overlay(close)

        self.revealer = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.CROSSFADE, transition_duration=180)
        self.revealer.set_child(ov)
        self.set_child(self.revealer)

        click = Gtk.GestureClick()
        click.set_button(1)
        click.connect("released", self._on_click)
        frame.add_controller(click)

        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda *_: self._stop_timer())
        motion.connect("leave", lambda *_: self._start_timer())
        ov.add_controller(motion)

        self.connect("map", lambda *_: GLib.idle_add(self.revealer.set_reveal_child, True))
        self._start_timer()

    def _start_timer(self):
        self._stop_timer()
        self.timer = GLib.timeout_add(THUMB_TIMEOUT_MS, self._timeout)

    def _stop_timer(self):
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = None

    def _timeout(self):
        self.timer = None
        self.dismiss()
        return False

    def _on_click(self, _g, _n, _x, _y):
        self._stop_timer()
        shot, monitor = self.shot, self.get_monitor_safe()
        self.dismiss()
        self.app.open_editor(shot, monitor)

    def get_monitor_safe(self):
        try:
            return LayerShell.get_monitor(self)
        except Exception:
            return None

    def dismiss(self):
        self._stop_timer()
        if self.app.thumbnail is self:
            self.app.thumbnail = None
        self.destroy()


# --------------------------------------------------------------------------- editor

CANVAS_BG = (0.082, 0.082, 0.090)  # #151517 — фон свободного поля вокруг картинки
MARGIN = 90  # свободное поле вокруг картинки при открытии (px окна)
TOOLBAR_SPACE = 84  # место под островок панели снизу
HANDLE = 8  # размер квадратика-хендла выделения
TEXT_DEFAULT_SIZE = 28.0

_scratch_cr = cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1))


def text_layout(cr, text, size):
    layout = PangoCairo.create_layout(cr)
    fd = Pango.FontDescription("Sans Bold")
    fd.set_absolute_size(size * Pango.SCALE)
    layout.set_font_description(fd)
    layout.set_text(text if text else " ", -1)  # пустой текст — хотя бы высота строки
    return layout


def text_size(text, size):
    _, logical = text_layout(_scratch_cr, text, size).get_pixel_extents()
    return max(4, logical.width), max(4, logical.height)


def _seg_dist(px, py, a, b):
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    if l2 < 1e-9:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


class Shape:
    """Фигура в координатах картинки (могут быть и за её пределами)."""

    __slots__ = ("tool", "color", "width", "points", "text", "size")

    def __init__(self, tool, color, width, p0, text="", size=TEXT_DEFAULT_SIZE):
        self.tool, self.color, self.width = tool, color, width
        self.points = [p0]
        self.text, self.size = text, size

    def copy(self):
        s = Shape(self.tool, self.color, self.width, self.points[0], self.text, self.size)
        s.points = list(self.points)
        return s

    def add(self, p):
        if self.tool == "pen":
            self.points.append(p)
        elif len(self.points) == 1:
            self.points.append(p)
        else:
            self.points[1] = p

    # --- геометрия
    def geo_bbox(self):
        if self.tool == "text":
            x, y = self.points[0]
            w, h = text_size(self.text, self.size)
            return (x, y, x + w, y + h)
        xs = [p[0] for p in self.points]
        ys = [p[1] for p in self.points]
        return (min(xs), min(ys), max(xs), max(ys))

    def pad(self):
        if self.tool == "text":
            return 2.0
        if self.tool == "arrow":
            return max(12.0, self.width * 3.5) * 0.5 + self.width
        return self.width / 2 + 1

    def bbox(self):
        x0, y0, x1, y1 = self.geo_bbox()
        p = self.pad()
        return (x0 - p, y0 - p, x1 + p, y1 + p)

    def hit(self, x, y, tol):
        x0, y0, x1, y1 = self.bbox()
        if not (x0 - tol <= x <= x1 + tol and y0 - tol <= y <= y1 + tol):
            return False
        if self.tool in ("rect", "ellipse", "text"):
            return True
        pts = self.points
        r = self.width / 2 + tol
        if len(pts) == 1:
            return math.hypot(x - pts[0][0], y - pts[0][1]) <= r
        return any(_seg_dist(x, y, a, b) <= r for a, b in zip(pts, pts[1:]))

    def move(self, dx, dy):
        self.points = [(px + dx, py + dy) for px, py in self.points]

    def fit_to(self, nb):
        """Вписать фигуру в новый geo-bbox (x0, y0, x1, y1). Текст масштабируется по высоте."""
        ox0, oy0, ox1, oy1 = self.geo_bbox()
        nx0, ny0, nx1, ny1 = nb
        if self.tool == "text":
            oh, nh = oy1 - oy0, ny1 - ny0
            if oh > 1e-6:
                self.size = max(6.0, self.size * nh / oh)
            self.points = [(nx0, ny0)]
            return
        ow, oh = ox1 - ox0, oy1 - oy0
        kx = (nx1 - nx0) / ow if ow > 1e-6 else 1.0
        ky = (ny1 - ny0) / oh if oh > 1e-6 else 1.0
        self.points = [(nx0 + (px - ox0) * kx, ny0 + (py - oy0) * ky) for px, py in self.points]

    # --- отрисовка
    def draw(self, cr, caret=False):
        cr.save()
        cr.set_source_rgba(*self.color)
        cr.set_line_width(self.width)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        p0 = self.points[0]
        p1 = self.points[-1]
        if self.tool == "text":
            x, y = p0
            layout = text_layout(cr, self.text, self.size)
            cr.move_to(x, y)
            PangoCairo.layout_path(cr, layout)
            cr.set_source_rgba(0, 0, 0, 0.55)  # тёмный ореол — читается на любом фоне
            cr.set_line_width(max(2.0, self.size / 9))
            cr.stroke_preserve()
            cr.set_source_rgba(*self.color)
            cr.fill()
            if caret:
                pos = layout.index_to_pos(len(self.text.encode("utf-8")))
                cx, cy, ch = x + pos.x / Pango.SCALE, y + pos.y / Pango.SCALE, pos.height / Pango.SCALE
                cr.set_line_width(max(1.5, self.size / 14))
                cr.move_to(cx + 1, cy)
                cr.line_to(cx + 1, cy + ch)
                cr.stroke()
        elif self.tool == "pen":
            cr.move_to(*p0)
            if len(self.points) == 1:
                cr.line_to(p0[0] + 0.01, p0[1])
            for p in self.points[1:]:
                cr.line_to(*p)
            cr.stroke()
        elif self.tool == "rect":
            x, y = min(p0[0], p1[0]), min(p0[1], p1[1])
            w, h = abs(p1[0] - p0[0]), abs(p1[1] - p0[1])
            cr.rectangle(x, y, w, h)
            cr.stroke()
        elif self.tool == "ellipse":
            cx, cy = (p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2
            rx, ry = max(0.5, abs(p1[0] - p0[0]) / 2), max(0.5, abs(p1[1] - p0[1]) / 2)
            cr.save()
            cr.translate(cx, cy)
            cr.scale(rx, ry)
            cr.arc(0, 0, 1, 0, 2 * math.pi)
            cr.restore()
            cr.stroke()
        elif self.tool == "arrow":
            dx, dy = p1[0] - p0[0], p1[1] - p0[1]
            length = math.hypot(dx, dy)
            if length >= 1:
                ang = math.atan2(dy, dx)
                head = max(12.0, self.width * 3.5)
                bx, by = p1[0] - math.cos(ang) * head * 0.8, p1[1] - math.sin(ang) * head * 0.8
                cr.move_to(*p0)
                cr.line_to(bx, by)
                cr.stroke()
                spread = math.radians(26)
                cr.move_to(*p1)
                cr.line_to(p1[0] - head * math.cos(ang - spread), p1[1] - head * math.sin(ang - spread))
                cr.line_to(p1[0] - head * math.cos(ang + spread), p1[1] - head * math.sin(ang + spread))
                cr.close_path()
                cr.fill()
        cr.restore()


class Shot:
    """Результат выделения: исходная поверхность + фигуры поверх + история."""

    def __init__(self, surface):
        self.surface = surface
        self.shapes = []
        self.undo = []  # снимки списка фигур
        self.redo = []

    @property
    def size(self):
        return self.surface.get_width(), self.surface.get_height()


class Editor(Gtk.Window):
    TOOLS = [
        ("select", "↖", tr("Select (V): drag to move, corners to resize, Delete to remove")),
        ("pen", "✎", tr("Pen (1)")),
        ("arrow", "➜", tr("Arrow (2)")),
        ("rect", "▭", tr("Rectangle (3)")),
        ("ellipse", "◯", tr("Ellipse (4)")),
        ("text", "T", tr("Text (5): click to type, Enter for a new line")),
    ]
    TOOL_KEYS = {Gdk.KEY_1: "pen", Gdk.KEY_2: "arrow", Gdk.KEY_3: "rect", Gdk.KEY_4: "ellipse", Gdk.KEY_5: "text", Gdk.KEY_v: "select"}

    def __init__(self, app, shot, monitor=None):
        super().__init__(application=app, title=tr("Screenshot"))
        self.set_icon_name(APP_ID)
        self.app, self.shot = app, shot
        self.tool = "pen"
        self.color = hex_to_rgb(PALETTE[0][0]) + (1.0,)
        self.stroke = 4.0
        self.current = None  # фигура, которую сейчас рисуют
        self.sel = None  # индекс выделенной фигуры
        self.editing = None  # индекс текста, который сейчас набирают
        self.mode = None  # что делает текущий drag: draw | move | resize | crop
        self.drag_start = self.drag_last = None
        self.drag_handle = None
        self.resize_orig = None
        self.resize_bbox = None
        self.undo_pending = False  # отложенный снимок истории для move/resize
        self.status_timer = None
        self.cropping = False
        self.crop_start = self.crop_cur = None

        self.set_decorated(False)
        self.set_resizable(True)
        self.add_css_class("shooter-transparent")

        iw, ih = shot.size
        if monitor is None:
            monitor = Gdk.Display.get_default().get_monitors().get_item(0)
        geo = monitor.get_geometry()
        avail_w = geo.width * 0.9 - 2 * MARGIN
        avail_h = geo.height * 0.9 - MARGIN - TOOLBAR_SPACE
        self.zoom = max(0.05, min(1.0, avail_w / iw, avail_h / ih))
        self.set_default_size(int(iw * self.zoom + 2 * MARGIN), int(ih * self.zoom + MARGIN + TOOLBAR_SPACE))

        frame = Gtk.Overlay()
        frame.add_css_class("editor-frame")
        frame.set_overflow(Gtk.Overflow.HIDDEN)
        self.set_child(frame)

        self.area = Gtk.DrawingArea(hexpand=True, vexpand=True)
        self.area.set_draw_func(self._draw)
        self.area.set_cursor(Gdk.Cursor.new_from_name("crosshair"))
        frame.set_child(self.area)

        toolbar = self._build_toolbar()
        frame.add_overlay(toolbar)
        frame.set_measure_overlay(toolbar, True)  # окно не уже панели

        # Тост поверх картинки (не измеряется — не растягивает окно).
        self.toast = Gtk.Label()
        self.toast.add_css_class("toast")
        self.toast_rev = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.CROSSFADE, transition_duration=150)
        self.toast_rev.set_child(self.toast)
        self.toast_rev.set_halign(Gtk.Align.CENTER)
        self.toast_rev.set_valign(Gtk.Align.START)
        self.toast_rev.set_margin_top(14)
        self.toast_rev.set_can_target(False)
        frame.add_overlay(self.toast_rev)

        # Крестик «закрыть» в правом верхнем углу.
        close = Gtk.Button(label="✕")
        close.add_css_class("corner-close")
        close.set_halign(Gtk.Align.END)
        close.set_valign(Gtk.Align.START)
        close.set_margin_top(8)
        close.set_margin_end(8)
        close.set_tooltip_text(tr("Close (Esc)"))
        close.connect("clicked", lambda *_: self.close())
        frame.add_overlay(close)

        # Уголок «изменить размер» справа внизу.
        rz = Gtk.DrawingArea()
        rz.set_size_request(18, 18)
        rz.set_draw_func(self._draw_resize_corner)
        rz.set_halign(Gtk.Align.END)
        rz.set_valign(Gtk.Align.END)
        rz.set_margin_end(4)
        rz.set_margin_bottom(4)
        rz.set_cursor(Gdk.Cursor.new_from_name("se-resize"))
        rz.set_tooltip_text(tr("Resize window"))
        rz_drag = Gtk.GestureDrag()
        rz_drag.set_button(1)
        rz_drag.connect("drag-begin", self._resize_window_begin, rz)
        rz.add_controller(rz_drag)
        frame.add_overlay(rz)

        drag = Gtk.GestureDrag()
        drag.set_button(1)
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        drag.connect("drag-end", self._drag_end)
        self.area.add_controller(drag)

        self.im = Gtk.IMMulticontext()
        self.im.set_client_widget(self)
        self.im.connect("commit", self._im_commit)
        keys = Gtk.EventControllerKey()
        keys.set_im_context(self.im)
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)

    # ------------------------------------------------------------------ панель
    def _build_toolbar(self):
        handle = Gtk.WindowHandle()  # за пустые места островка можно таскать окно
        handle.set_halign(Gtk.Align.CENTER)
        handle.set_valign(Gtk.Align.END)
        handle.set_margin_bottom(14)
        handle.set_margin_start(18)  # островок измеряется — эти поля задают минимальную ширину окна
        handle.set_margin_end(18)
        bar = Gtk.Box(spacing=5)
        bar.add_css_class("editor-toolbar")
        handle.set_child(bar)

        self.tool_buttons = {}
        group = None
        for key, glyph, name in self.TOOLS:
            b = Gtk.ToggleButton(label=glyph)
            b.add_css_class("tool-btn")
            b.set_tooltip_text(name)
            if group is None:
                group = b
            else:
                b.set_group(group)
            b.set_active(key == self.tool)
            b.connect("toggled", self._tool_toggled, key)
            bar.append(b)
            self.tool_buttons[key] = b

        bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        self.swatches = []
        sw_group = None
        for i, (hx, name) in enumerate(PALETTE):
            b = Gtk.ToggleButton()
            b.add_css_class("swatch")
            b.set_valign(Gtk.Align.CENTER)
            b.set_tooltip_text(name)
            b.add_css_class("swatch-%d" % i)  # цвет задаёт общий CSS (см. App.do_startup)
            if sw_group is None:
                sw_group = b
            else:
                b.set_group(sw_group)
            b.set_active(i == 0)
            b.connect("toggled", self._swatch_toggled, hx)
            bar.append(b)
            self.swatches.append(b)

        dlg = Gtk.ColorDialog(with_alpha=False)
        self.color_btn = Gtk.ColorDialogButton(dialog=dlg)
        self.color_btn.set_tooltip_text(tr("Custom color"))
        self.color_btn.set_valign(Gtk.Align.CENTER)
        c = Gdk.RGBA()
        c.parse(PALETTE[0][0])
        self.color_btn.set_rgba(c)
        self.color_btn.connect("notify::rgba", self._custom_color)
        bar.append(self.color_btn)

        bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        self.width_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 1, 20, 1)
        self.width_scale.set_value(self.stroke)
        self.width_scale.set_size_request(90, -1)
        self.width_scale.set_valign(Gtk.Align.CENTER)
        self.width_scale.set_draw_value(False)
        self.width_scale.set_tooltip_text(tr("Line width"))
        self.width_scale.connect("value-changed", self._stroke_changed)
        bar.append(self.width_scale)

        bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        for label, tip, cb in [
            ("↶", tr("Undo (Ctrl+Z)"), self.undo),
            ("↷", tr("Redo (Ctrl+Y)"), self.redo),
            ("✂", "%s (%s)" % (tr("Cut out a part of this screenshot"), load_hotkeys()["region"]), self.start_crop),
            (tr("Copy"), tr("Copy to clipboard (Ctrl+C)"), self.copy),
            (tr("Save"), tr("Save to the Screenshots folder (Ctrl+S)"), self.save),
        ]:
            b = Gtk.Button(label=label)
            b.set_tooltip_text(tip)
            if cb == self.copy:
                b.add_css_class("primary")
            b.connect("clicked", lambda _b, f=cb: f())
            bar.append(b)

        # Ручка «перетащить окно»: весь островок — WindowHandle, ⠿ лишь подсказывает, за что тянуть.
        grip = Gtk.Label(label="⠿")
        grip.add_css_class("grip")
        grip.set_tooltip_text(tr("Drag window"))
        grip.set_cursor(Gdk.Cursor.new_from_name("grab"))
        bar.append(grip)
        return handle

    def _tool_toggled(self, btn, key):
        if btn.get_active():
            self.finish_text()
            self.tool = key
            self.area.set_cursor(Gdk.Cursor.new_from_name("default" if key == "select" else "crosshair"))

    def _swatch_toggled(self, btn, hx):
        if btn.get_active():
            self._set_color(hex_to_rgb(hx) + (1.0,))

    def _custom_color(self, btn, _pspec):
        c = btn.get_rgba()
        self._set_color((c.red, c.green, c.blue, 1.0))

    def _set_color(self, color):
        self.color = color
        self._apply_to_selected("color", color)

    def _stroke_changed(self, scale):
        self.stroke = float(scale.get_value())
        self._apply_to_selected("width", self.stroke)

    def _apply_to_selected(self, attr, value):
        """Цвет/толщина меняются и у выделенной фигуры — так ожидаешь от редактора."""
        if self.sel is None or self.sel >= len(self.shot.shapes):
            return
        s = self.shot.shapes[self.sel]
        if getattr(s, attr) == value:
            return
        self.push_undo()
        setattr(s, attr, value)
        self.area.queue_draw()

    def flash(self, text, ms=2500):
        self.toast.set_text(text)
        self.toast_rev.set_reveal_child(True)
        if self.status_timer:
            GLib.source_remove(self.status_timer)
        self.status_timer = GLib.timeout_add(ms, self._clear_status)

    def _clear_status(self):
        self.status_timer = None
        self.toast_rev.set_reveal_child(False)
        return False

    # ------------------------------------------------------------------ координаты
    def img_origin(self):
        W, H = self.area.get_width(), self.area.get_height()
        iw, ih = self.shot.size
        return ((W - iw * self.zoom) / 2, max(16.0, (H - TOOLBAR_SPACE - ih * self.zoom) / 2 + 8))

    def to_img(self, x, y):
        ox, oy = self.img_origin()
        return ((x - ox) / self.zoom, (y - oy) / self.zoom)

    def to_win(self, x, y):
        ox, oy = self.img_origin()
        return (ox + x * self.zoom, oy + y * self.zoom)

    def canvas_extent(self):
        """Видимое поле холста в координатах картинки."""
        x0, y0 = self.to_img(0, 0)
        x1, y1 = self.to_img(self.area.get_width(), self.area.get_height())
        return (x0, y0, x1, y1)

    # ------------------------------------------------------------------ история
    def push_undo(self):
        self.shot.undo.append([s.copy() for s in self.shot.shapes])
        del self.shot.undo[:-100]
        self.shot.redo.clear()

    def undo(self):
        self.finish_text()
        if not self.shot.undo:
            return
        self.shot.redo.append([s.copy() for s in self.shot.shapes])
        self.shot.shapes = self.shot.undo.pop()
        self.sel = None
        self.area.queue_draw()

    def redo(self):
        self.finish_text()
        if not self.shot.redo:
            return
        self.shot.undo.append([s.copy() for s in self.shot.shapes])
        self.shot.shapes = self.shot.redo.pop()
        self.sel = None
        self.area.queue_draw()

    # ------------------------------------------------------------------ выделение
    def _shape_at(self, ix, iy):
        tol = 6 / self.zoom
        for i in range(len(self.shot.shapes) - 1, -1, -1):
            if self.shot.shapes[i].hit(ix, iy, tol):
                return i
        return None

    def _handles(self, shape):
        x0, y0, x1, y1 = shape.bbox()
        wx0, wy0 = self.to_win(x0, y0)
        wx1, wy1 = self.to_win(x1, y1)
        return {"nw": (wx0, wy0), "ne": (wx1, wy0), "sw": (wx0, wy1), "se": (wx1, wy1)}, (wx0, wy0, wx1, wy1)

    def _handle_at(self, x, y):
        if self.sel is None or self.sel >= len(self.shot.shapes):
            return None
        handles, _ = self._handles(self.shot.shapes[self.sel])
        r = HANDLE / 2 + 4
        for name, (hx, hy) in handles.items():
            if abs(x - hx) <= r and abs(y - hy) <= r:
                return name
        return None

    def delete_selected(self):
        if self.sel is None or self.sel >= len(self.shot.shapes):
            return
        self.finish_text()
        self.push_undo()
        del self.shot.shapes[self.sel]
        self.sel = None
        self.area.queue_draw()

    # ------------------------------------------------------------------ текст
    def start_text_edit(self, idx):
        self.editing = idx
        self.sel = idx
        self.im.focus_in()
        self.area.queue_draw()

    def finish_text(self):
        if self.editing is None:
            return
        idx, self.editing = self.editing, None
        self.im.focus_out()
        if idx < len(self.shot.shapes) and not self.shot.shapes[idx].text.strip():
            del self.shot.shapes[idx]
            if self.shot.undo:
                self.shot.undo.pop()  # пустой текст — как будто и не было
            self.sel = None
        self.area.queue_draw()

    def _im_commit(self, _im, text):
        if self.editing is None or self.editing >= len(self.shot.shapes):
            return
        self.shot.shapes[self.editing].text += text
        self.area.queue_draw()

    # ------------------------------------------------------------------ режим «вырезать кусок»
    def start_crop(self):
        self.finish_text()
        self.current = None
        self.cropping = True
        self.crop_start = self.crop_cur = None
        self.flash(tr("Select an area  •  Esc to cancel"), ms=120000)
        self.area.queue_draw()

    def stop_crop(self):
        self.cropping = False
        self.crop_start = self.crop_cur = None
        self._clear_status()
        if self.status_timer:
            GLib.source_remove(self.status_timer)
            self.status_timer = None
        self.area.queue_draw()

    def crop_rect(self):
        if self.crop_start is None or self.crop_cur is None:
            return None
        (x0, y0), (x1, y1) = self.crop_start, self.crop_cur
        w, h = self.area.get_width(), self.area.get_height()
        x0, x1 = max(0, min(w, x0)), max(0, min(w, x1))
        y0, y1 = max(0, min(h, y0)), max(0, min(h, y1))
        return (min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0))

    # ------------------------------------------------------------------ мышь
    def _drag_begin(self, _g, x, y):
        self.drag_start = self.drag_last = (x, y)
        self.undo_pending = False
        self.mode = None
        if self.cropping:
            self.mode = "crop"
            self.crop_start = self.crop_cur = (x, y)
            self.area.queue_draw()
            return
        shapes = self.shot.shapes
        ix, iy = self.to_img(x, y)
        if self.editing is not None:
            if self.editing < len(shapes) and shapes[self.editing].hit(ix, iy, 4 / self.zoom):
                return  # клик по набираемому тексту — просто продолжаем печатать
            self.finish_text()
        h = self._handle_at(x, y)
        if h is not None:
            self.mode = "resize"
            self.drag_handle = h
            self.resize_orig = shapes[self.sel].copy()
            self.resize_bbox = self.resize_orig.geo_bbox()
            self.undo_pending = True
            return
        if self.tool == "select":
            idx = self._shape_at(ix, iy)
            self.sel = idx
            if idx is not None:
                self.mode = "move"
                self.undo_pending = True
            self.area.queue_draw()
            return
        if self.tool == "text":
            idx = self._shape_at(ix, iy)
            if idx is not None and shapes[idx].tool == "text":
                self.push_undo()
                self.start_text_edit(idx)
            else:
                self.push_undo()
                shapes.append(Shape("text", self.color, self.stroke, (ix, iy), "", TEXT_DEFAULT_SIZE))
                self.start_text_edit(len(shapes) - 1)
            return
        self.sel = None
        self.mode = "draw"
        self.current = Shape(self.tool, self.color, self.stroke, (ix, iy))
        self.area.queue_draw()

    def _drag_update(self, _g, dx, dy):
        if self.drag_start is None or self.mode is None:
            return
        x, y = self.drag_start[0] + dx, self.drag_start[1] + dy
        if self.mode == "crop":
            self.crop_cur = (x, y)
        elif self.mode == "draw" and self.current is not None:
            self.current.add(self.to_img(x, y))
        elif self.mode == "move" and self.sel is not None and self.sel < len(self.shot.shapes):
            if self.undo_pending:
                self.push_undo()
                self.undo_pending = False
            lx, ly = self.drag_last
            self.shot.shapes[self.sel].move((x - lx) / self.zoom, (y - ly) / self.zoom)
        elif self.mode == "resize" and self.sel is not None and self.sel < len(self.shot.shapes):
            if self.undo_pending:
                self.push_undo()
                self.undo_pending = False
            x0, y0, x1, y1 = self.resize_bbox
            ix, iy = self.to_img(x, y)
            h = self.drag_handle
            if "w" in h:
                x0 = min(ix, x1 - 1)
            else:
                x1 = max(ix, x0 + 1)
            if "n" in h:
                y0 = min(iy, y1 - 1)
            else:
                y1 = max(iy, y0 + 1)
            s = self.resize_orig.copy()
            s.fit_to((x0, y0, x1, y1))
            self.shot.shapes[self.sel] = s
        self.drag_last = (x, y)
        self.area.queue_draw()

    def _drag_end(self, g, dx, dy):
        if self.drag_start is None:
            return
        self._drag_update(g, dx, dy)
        mode, self.mode = self.mode, None
        if mode == "crop":
            rect = self.crop_rect()
            if rect and rect[2] >= 3 and rect[3] >= 3:
                x, y = self.to_img(rect[0], rect[1])
                img_rect = (int(round(x)), int(round(y)), max(1, int(round(rect[2] / self.zoom))), max(1, int(round(rect[3] / self.zoom))))
                self.stop_crop()
                self.app.finish_subshot(self, img_rect)
            else:
                self.crop_start = self.crop_cur = None
        elif mode == "draw" and self.current is not None:
            self.push_undo()
            self.shot.shapes.append(self.current)
            self.sel = len(self.shot.shapes) - 1
            self.current = None
        self.resize_orig = None
        self.area.queue_draw()

    def _resize_window_begin(self, g, x, y, widget):
        ok, pt = widget.compute_point(self, Graphene.Point().init(x, y))
        if not ok:
            return
        surface = self.get_surface()
        if surface is not None:
            surface.begin_resize(Gdk.SurfaceEdge.SOUTH_EAST, g.get_device(), 1, pt.x, pt.y, g.get_current_event_time())

    # ------------------------------------------------------------------ отрисовка
    def _draw(self, _area, cr, W, H):
        cr.set_source_rgb(*CANVAS_BG)
        cr.paint()
        ox, oy = self.img_origin()
        iw, ih = self.shot.size
        cr.save()
        cr.translate(ox, oy)
        cr.scale(self.zoom, self.zoom)
        cr.set_source_surface(self.shot.surface, 0, 0)
        cr.paint()
        for i, s in enumerate(self.shot.shapes):
            s.draw(cr, caret=(i == self.editing))
        if self.current is not None:
            self.current.draw(cr)
        cr.restore()
        cr.set_source_rgba(1, 1, 1, 0.16)
        cr.set_line_width(1)
        cr.rectangle(ox - 0.5, oy - 0.5, iw * self.zoom + 1, ih * self.zoom + 1)
        cr.stroke()
        if self.sel is not None and self.sel < len(self.shot.shapes) and not self.cropping:
            self._draw_selection(cr, self.shot.shapes[self.sel])
        if self.cropping:
            self._draw_crop_overlay(cr, W, H)

    def _draw_selection(self, cr, shape):
        handles, (x0, y0, x1, y1) = self._handles(shape)
        cr.save()
        cr.set_line_width(1)
        cr.set_dash([4, 3])
        cr.set_source_rgba(1, 1, 1, 0.85)
        cr.rectangle(x0 + 0.5, y0 + 0.5, x1 - x0, y1 - y0)
        cr.stroke()
        cr.set_dash([])
        if self.editing is None:
            for hx, hy in handles.values():
                cr.rectangle(hx - HANDLE / 2, hy - HANDLE / 2, HANDLE, HANDLE)
                cr.set_source_rgb(1, 1, 1)
                cr.fill_preserve()
                cr.set_source_rgba(0, 0, 0, 0.7)
                cr.stroke()
        cr.restore()

    def _draw_resize_corner(self, _a, cr, w, h):
        cr.set_source_rgba(1, 1, 1, 0.35)
        for k in (3, 8, 13):
            cr.move_to(w - 2, h - 2 - k)
            cr.line_to(w - 2 - k, h - 2)
        cr.set_line_width(1.5)
        cr.stroke()

    def _draw_crop_overlay(self, cr, w, h):
        sel = self.crop_rect()
        cr.set_source_rgba(0, 0, 0, 0.45)
        if sel is None:
            cr.rectangle(0, 0, w, h)
            cr.fill()
            return
        x, y, sw, sh = sel
        cr.rectangle(0, 0, w, h)
        cr.rectangle(x, y + sh, sw, -sh)
        cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
        cr.fill()
        cr.set_source_rgba(1, 1, 1, 0.95)
        cr.set_line_width(1.5)
        cr.rectangle(x - 0.75, y - 0.75, sw + 1.5, sh + 1.5)
        cr.stroke()
        text = "%d × %d" % (round(sw / self.zoom), round(sh / self.zoom))
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(13)
        ext = cr.text_extents(text)
        pad = 6
        bw, bh = ext.width + pad * 2, ext.height + pad * 2
        lx = max(4, min(w - bw - 4, x))
        ly = y + sh + 8
        if ly + bh > h - 4:
            ly = max(4, y - bh - 8)
        cr.set_source_rgba(0, 0, 0, 0.75)
        _rounded_rect(cr, lx, ly, bw, bh, 6)
        cr.fill()
        cr.set_source_rgb(1, 1, 1)
        cr.move_to(lx + pad - ext.x_bearing, ly + pad - ext.y_bearing)
        cr.show_text(text)

    # ------------------------------------------------------------------ экспорт
    def export_rect(self):
        """Картинка целиком плюс поле вокруг, только если туда что-то нарисовали."""
        iw, ih = self.shot.size
        x0, y0, x1, y1 = 0.0, 0.0, float(iw), float(ih)
        for s in self.shot.shapes:
            bx0, by0, bx1, by1 = s.bbox()
            x0, y0, x1, y1 = min(x0, bx0), min(y0, by0), max(x1, bx1), max(y1, by1)
        cx0, cy0, cx1, cy1 = self.canvas_extent()
        x0, y0 = max(x0, cx0), max(y0, cy0)
        x1, y1 = min(x1, cx1), min(y1, cy1)
        x0, y0 = min(0, int(math.floor(x0))), min(0, int(math.floor(y0)))
        x1, y1 = max(iw, int(math.ceil(x1))), max(ih, int(math.ceil(y1)))
        return (x0, y0, x1 - x0, y1 - y0)

    def render_region(self, x, y, w, h):
        """Любой прямоугольник холста в координатах картинки (поле вокруг — фоном холста)."""
        out = cairo.ImageSurface(cairo.FORMAT_ARGB32, max(1, w), max(1, h))
        cr = cairo.Context(out)
        cr.set_source_rgb(*CANVAS_BG)
        cr.paint()
        cr.translate(-x, -y)
        cr.set_source_surface(self.shot.surface, 0, 0)
        cr.paint()
        for s in self.shot.shapes:
            s.draw(cr)
        out.flush()
        return out

    def export_surface(self):
        self.finish_text()
        return self.render_region(*self.export_rect())

    def copy(self):
        copy_to_clipboard(self.export_surface())
        self.flash(tr("✓ Copied to clipboard"))

    def save(self):
        try:
            path = save_png(surface_to_png(self.export_surface()))
            self.flash(tr("✓ Saved: %s") % os.path.basename(path), 4000)
        except Exception as e:
            self.flash(tr("Save failed: %s") % e, 5000)

    # ------------------------------------------------------------------ клавиатура
    def _on_key(self, _c, keyval, keycode, state):
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        k = latin_key(self.get_display(), keyval, keycode)
        shapes = self.shot.shapes
        if self.editing is not None and not ctrl:
            if self.editing >= len(shapes):
                self.editing = None
                return False
            s = shapes[self.editing]
            if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
                s.text += "\n"
            elif keyval == Gdk.KEY_BackSpace:
                s.text = s.text[:-1]
            elif keyval == Gdk.KEY_Escape:
                self.finish_text()
            else:
                return False  # остальное набирает IM (commit)
            self.area.queue_draw()
            return True
        if keyval == Gdk.KEY_Escape:
            if self.cropping:
                self.stop_crop()
            elif self.sel is not None:
                self.sel = None
                self.area.queue_draw()
            else:
                self.close()
        elif keyval in (Gdk.KEY_Delete, Gdk.KEY_BackSpace) and not ctrl:
            self.delete_selected()
        elif ctrl and k == Gdk.KEY_c:
            self.copy()
        elif ctrl and k == Gdk.KEY_s:
            self.save()
        elif ctrl and k == Gdk.KEY_y:
            self.redo()
        elif ctrl and k == Gdk.KEY_z:
            self.redo() if shift else self.undo()
        elif not ctrl and k in self.TOOL_KEYS:
            self.tool_buttons[self.TOOL_KEYS[k]].set_active(True)
        else:
            return False
        return True


# --------------------------------------------------------------------------- application


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.overlays = []
        self.cap = None
        self.thumbnail = None
        self.editors = []
        self.last_shot = None
        self.grabbing = False
        self.layer_shell = False
        self.portal_hotkeys = None

    def do_startup(self):
        Gtk.Application.do_startup(self)
        self.hold()  # живём как демон
        for name, cb in (
            ("capture", self.on_capture),
            ("fullscreen", self.on_fullscreen),
            ("cancel", lambda *_: self.cancel_capture()),
            ("edit-last", lambda *_: self.last_shot and self.open_editor(self.last_shot)),
            ("quit", lambda *_: self.quit()),
        ):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", cb)
            self.add_action(act)
        # Селектор не слабее общего правила для кнопок панели, иначе фон кнопки перебьёт цвет.
        css = CSS + "".join(
            ".editor-toolbar button.swatch.swatch-%d { background-color: %s; }\n" % (i, hx) for i, (hx, _n) in enumerate(PALETTE)
        ).encode()
        prov = Gtk.CssProvider()
        prov.load_from_bytes(GLib.Bytes.new(css))
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        on_x11 = Gdk.Display.get_default().get_name().startswith(":")
        self.layer_shell = LayerShell is not None and not NO_LAYER_SHELL and not on_x11 and LayerShell.is_supported()
        if not self.layer_shell:
            print("layer-shell unavailable: fullscreen overlay window and notifications instead", file=sys.stderr)
        first_run = not os.path.exists(CONFIG_PATH)
        if kde_hotkeys_available():
            if first_run:
                # Первый запуск у этого пользователя: пакет ставится root'ом, а глобальные
                # клавиши — пользовательские, поэтому назначаем их сами.
                GLib.idle_add(self._first_run_kde)
        else:
            write_default_config()
            self.portal_hotkeys = PortalHotkeys(self)
            self.portal_hotkeys.start()
            if IN_FLATPAK and first_run:
                request_autostart()

    def _first_run_kde(self):
        try:
            install_hotkey()
        except Exception as e:
            print("hotkey registration failed:", e, file=sys.stderr)
        return False

    def do_command_line(self, cmdline):
        args = cmdline.get_arguments()[1:]
        cmd = args[0] if args else "capture"
        if cmd == "daemon":
            pass
        elif cmd in ("capture", "fullscreen", "cancel", "edit-last", "quit"):
            self.activate_action(cmd, None)
        elif cmd == "install-hotkey" and self.portal_hotkeys is not None:
            self.portal_hotkeys.bind()
            cmdline.print_literal(tr("Shortcuts requested from the desktop; confirm them if it asks.") + "\n")
        else:
            cmdline.printerr_literal(USAGE)
            return 2
        return 0

    def do_activate(self):
        self.on_capture()

    # --- capture flow
    def _grab_failed(self, message):
        self.grabbing = False
        if message:
            self.notify_error(tr("Couldn't take a screenshot: %s") % message)

    def on_fullscreen(self, *_):
        """Снимок всего экрана без выделения: буфер + миниатюра, дальше как обычно."""
        if self.grabbing:
            return
        if self.overlays:
            self._close_overlays()
        self.grabbing = True
        Capture.grab_async(self._fullscreen_ready, self._grab_failed)

    def _fullscreen_ready(self, cap):
        self.grabbing = False
        surface = cap.crop(0, 0, cap.width, cap.height)
        copy_to_clipboard_background(surface)
        monitors = Gdk.Display.get_default().get_monitors()
        monitor = monitors.get_item(0) if monitors.get_n_items() else None
        self.show_thumbnail(Shot(surface), monitor)

    def on_capture(self, *_):
        if self.overlays or self.grabbing:
            return
        # Клавиша снимка области, когда редактор в фокусе: вырезаем кусок уже снятого.
        for ed in self.editors:
            if ed.is_active():
                ed.start_crop()
                return
        self.grabbing = True
        Capture.grab_async(self._show_overlays, self._grab_failed)

    def _show_overlays(self, cap):
        self.grabbing = False
        self.cap = cap
        display = Gdk.Display.get_default()
        monitors = display.get_monitors()
        mons = [monitors.get_item(i) for i in range(monitors.get_n_items())]
        # Масштаб кадра к логическим координатам: ширина кадра / ширина рабочего пространства.
        if mons:
            union_w = max(m.get_geometry().x + m.get_geometry().width for m in mons)
            union_h = max(m.get_geometry().y + m.get_geometry().height for m in mons)
            cap.scale = max(cap.width / union_w, cap.height / union_h)
        for m in mons:
            ov = Overlay(self, cap, m)
            self.overlays.append(ov)
            ov.present()

    def selection_started(self, active):
        # Пока тянут рамку на одном мониторе, остальные просто затемнены.
        for ov in self.overlays:
            if ov is not active:
                ov.canvas.cursor = None
                ov.canvas.queue_draw()

    def cancel_capture(self):
        self._close_overlays()
        self.cap = None

    def _close_overlays(self):
        for ov in self.overlays:
            ov.destroy()
        self.overlays = []

    def finish_capture(self, overlay, rect):
        cap, s = self.cap, self.cap.scale
        ox, oy = overlay.origin
        x, y, w, h = rect
        px, py = int(round((ox + x) * s)), int(round((oy + y) * s))
        pw, ph = max(1, int(round(w * s))), max(1, int(round(h * s)))
        surface = cap.crop(px, py, pw, ph)
        shot = Shot(surface)
        # Буфер обмена выставляем, пока оверлей ещё в фокусе: Wayland любит свежий serial.
        copy_to_clipboard(surface)
        monitor = overlay.monitor
        self._close_overlays()
        self.cap = None
        self.show_thumbnail(shot, monitor)

    def finish_subshot(self, editor, rect):
        """Кусок из редактора ведёт себя как обычный снимок: буфер обмена + миниатюра."""
        surface = editor.render_region(*rect)
        copy_to_clipboard(surface)
        monitor = None
        try:
            monitor = Gdk.Display.get_default().get_monitor_at_surface(editor.get_surface())
        except Exception:
            pass
        editor.flash(tr("✓ Cut out and copied"))
        self.show_thumbnail(Shot(surface), monitor)

    def show_thumbnail(self, shot, monitor):
        self.last_shot = shot
        if self.thumbnail is not None:
            self.thumbnail.dismiss()
        if not self.layer_shell:
            # Без layer-shell окно в углу поверх всего не поставить: зовём через уведомление.
            n = Gio.Notification.new(tr("Screenshot copied"))
            n.set_body(tr("Click to annotate"))
            n.set_icon(Gio.ThemedIcon.new(APP_ID))
            n.set_default_action("app.edit-last")
            self.send_notification("shot", n)
            return
        self.thumbnail = Thumbnail(self, shot, monitor)
        self.thumbnail.present()

    def open_editor(self, shot, monitor=None):
        ed = Editor(self, shot, monitor)
        self.editors.append(ed)
        ed.connect("destroy", lambda w: self.editors.remove(w) if w in self.editors else None)
        ed.present()
        return ed

    def notify_error(self, text):
        print(text, file=sys.stderr)
        try:
            n = Gio.Notification.new("Screenshooter")
            n.set_body(text)
            self.send_notification("error", n)
        except Exception:
            pass


# --------------------------------------------------------------------------- hotkeys through the portal


class PortalHotkeys:
    """Глобальные клавиши через портал GlobalShortcuts: Flatpak и окружения без kglobalaccel.

    Сессию держит запущенный экземпляр; рабочий стол может один раз попросить подтверждения.
    """

    IDS = {"region": "capture", "fullscreen": "fullscreen"}  # ключ конфига → действие приложения

    def __init__(self, app):
        self.app = app
        self.session = None

    def start(self):
        portal_request(
            "org.freedesktop.portal.GlobalShortcuts", "CreateSession", "(a{sv})", (),
            {"session_handle_token": GLib.Variant("s", "screenshooter%d" % os.getpid())}, self._created,
        )

    def _created(self, code, results):
        if code != 0:
            print("GlobalShortcuts portal: CreateSession failed (%d): %s" % (code, results), file=sys.stderr)
            return
        self.session = results["session_handle"]
        session_bus().signal_subscribe(
            PORTAL_BUS, "org.freedesktop.portal.GlobalShortcuts", "Activated", PORTAL_PATH, None,
            Gio.DBusSignalFlags.NONE, self._activated,
        )
        self.bind()

    def _activated(self, _conn, _sender, _path, _iface, _signal, params):
        session, shortcut_id = params.unpack()[:2]
        if session == self.session and shortcut_id in self.IDS.values():
            self.app.activate_action(shortcut_id, None)

    def bind(self):
        if self.session is None:
            return
        keys = load_hotkeys()
        shortcuts = []
        for cfg, (_desktop, _action, _default, friendly) in HOTKEY_ACTIONS.items():
            text = keys[cfg]
            if not text or text.lower() in ("none", "off", "-"):
                continue
            opts = {"description": GLib.Variant("s", friendly)}
            try:
                opts["preferred_trigger"] = GLib.Variant("s", xdg_trigger(text))
            except ValueError as e:
                print(tr("%s: can't parse %r (%s)") % (friendly, text, e), file=sys.stderr)
            shortcuts.append((self.IDS[cfg], opts))
        portal_request(
            "org.freedesktop.portal.GlobalShortcuts", "BindShortcuts", "(oa(sa{sv})sa{sv})",
            (self.session, shortcuts, ""), {}, self._bound,
        )

    def _bound(self, code, results):
        if code != 0:
            print("GlobalShortcuts portal: BindShortcuts failed (%d): %s" % (code, results), file=sys.stderr)
            return
        for shortcut_id, info in results.get("shortcuts", []):
            print("shortcut %s: %s" % (shortcut_id, info.get("trigger_description", "?")), file=sys.stderr)


def request_autostart():
    """Flatpak: автозапуск фонового экземпляра просим у портала Background."""
    portal_request(
        "org.freedesktop.portal.Background", "RequestBackground", "(sa{sv})", ("",),
        {
            "reason": GLib.Variant("s", tr("Keep running so screenshot hotkeys react instantly")),
            "autostart": GLib.Variant("b", True),
            "commandline": GLib.Variant("as", ["screenshooter", "daemon"]),
        },
        lambda code, results: print("background portal: %d %s" % (code, results), file=sys.stderr),
    )


# --------------------------------------------------------------------------- hotkey setup (KGlobalAccel over DBus)


def _kga():
    return Gio.DBusProxy.new_for_bus_sync(
        Gio.BusType.SESSION, Gio.DBusProxyFlags.NONE, None, "org.kde.kglobalaccel", "/kglobalaccel", "org.kde.KGlobalAccel", None
    )


def hotkey_holders(proxy, key):
    res = proxy.call_sync("getGlobalShortcutsByKey", GLib.Variant("(i)", (key,)), Gio.DBusCallFlags.NONE, -1, None)
    return [(r[2], r[0]) for r in res.unpack()[0]]  # (component unique, action unique)


def _write_shortcut_config(desktop_id, action, text):
    # Демон пишет kglobalshortcutsrc сам, но с задержкой и не всегда — дублируем,
    # чтобы привязка пережила перезаход в сеанс.
    tool = shutil.which("kwriteconfig6") or shutil.which("kwriteconfig5")
    if not tool:
        return
    cmd = [tool, "--file", "kglobalshortcutsrc", "--group", "services", "--group", desktop_id, "--key", action]
    cmd += [text] if text else ["--delete"]
    subprocess.run(cmd, check=False)


def install_hotkey():
    write_default_config()
    proxy = _kga()
    keys = load_hotkeys()
    all_ok = True
    for cfg, (desktop_id, action, _default, friendly) in HOTKEY_ACTIONS.items():
        text = keys[cfg]
        if not text or text.lower() in ("none", "off", "-"):
            proxy.call_sync("unregister", GLib.Variant("(ss)", (desktop_id, action)), Gio.DBusCallFlags.NONE, -1, None)
            _write_shortcut_config(desktop_id, action, "")
            print(tr("%s: disabled in the config") % friendly)
            continue
        try:
            code = parse_qt_key(text)
        except ValueError as e:
            print(tr("%s: can't parse %r (%s)") % (friendly, text, e))
            all_ok = False
            continue
        debug("kglobalaccel getGlobalShortcutsByKey", hex(code))
        for comp, other in hotkey_holders(proxy, code):
            if comp == desktop_id and other == action:
                continue
            print(tr("Taking %s away from %s / %s") % (text, comp, other))
            proxy.call_sync("unregister", GLib.Variant("(ss)", (comp, other)), Gio.DBusCallFlags.NONE, -1, None)
        action_id = GLib.Variant("as", [desktop_id, action, "Screenshooter", friendly])
        debug("kglobalaccel doRegister", desktop_id, action)
        proxy.call_sync("doRegister", GLib.Variant.new_tuple(action_id), Gio.DBusCallFlags.NONE, -1, None)
        # Классический setShortcut с простыми int: он есть во всех версиях kglobalaccel, а более новый
        # setShortcutKeys (a(ai)) на некоторых сборках KWin роняет сам KWin при разборе ответа.
        # flags: SetPresent(2) | NoAutoloading(4) — назначить принудительно, не оглядываясь на старый конфиг.
        debug("kglobalaccel setShortcut", desktop_id, action, hex(code))
        res = proxy.call_sync(
            "setShortcut",
            GLib.Variant.new_tuple(action_id, GLib.Variant("ai", [code]), GLib.Variant("u", 2 | 4)),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
        )
        got = res.unpack()[0]
        debug("kglobalaccel setShortcut ->", [hex(k) for k in got])
        ok = code in got
        if ok:
            print(tr("%s — %s: assigned") % (friendly, text))
        else:
            print(tr("%s — %s: NOT assigned (%r)") % (friendly, text, got))
        all_ok = all_ok and ok
        if ok:
            _write_shortcut_config(desktop_id, action, text)
    return all_ok


def uninstall_hotkey():
    proxy = _kga()
    for desktop_id, action, _default, _friendly in HOTKEY_ACTIONS.values():
        proxy.call_sync("unregister", GLib.Variant("(ss)", (desktop_id, action)), Gio.DBusCallFlags.NONE, -1, None)
        _write_shortcut_config(desktop_id, action, "")
    print(tr("Hotkeys removed."))


USAGE = """usage: screenshooter [COMMAND]

  capture            capture a screen region (default)
  fullscreen         capture the whole screen
  cancel             close the region selection overlay
  daemon             start the background instance without capturing
  quit               stop the background instance
  edit-last          open the editor for the last screenshot
  install-hotkey     (re)register global shortcuts from ~/.config/screenshooter.conf
  uninstall-hotkey   remove the global shortcuts
  --version          print the version
"""


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd in ("-h", "--help", "help"):
        sys.stdout.write(USAGE)
        return 0
    if cmd in ("-V", "--version"):
        print("screenshooter", VERSION)
        return 0
    if (Gtk.get_major_version(), Gtk.get_minor_version()) < MIN_GTK:
        print("Screenshooter needs GTK %d.%d or newer, found %d.%d."
              % (MIN_GTK + (Gtk.get_major_version(), Gtk.get_minor_version())), file=sys.stderr)
        return 1
    if cmd in ("install-hotkey", "uninstall-hotkey") and not kde_hotkeys_available():
        if cmd == "uninstall-hotkey":
            print(tr("Shortcuts are managed by your desktop: change or remove them in System Settings."))
            return 0
        # install-hotkey в портальном режиме обрабатывает запущенный экземпляр (у него сессия портала).
    elif cmd == "install-hotkey":
        return 0 if install_hotkey() else 1
    elif cmd == "uninstall-hotkey":
        uninstall_hotkey()
        return 0
    app = App()
    return app.run(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
