#!/usr/bin/env python3
"""End-to-end test inside a headless KWin.

    tests/run-e2e.sh            # starts a virtual KWin with its own D-Bus session and runs this

Uses the installed `screenshooter` from PATH. Every check looks at real frames grabbed from
KWin, so it covers the layer-shell overlay, the thumbnail, the clipboard and the editor.
Writes PASS/FAIL lines to stdout and the overall result to $E2E_RESULT.
"""
import os
import re
import shutil
import statistics
import subprocess
import sys
import time

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib  # noqa: E402

APP_ID = "io.github.johnsutray.Screenshooter"
OBJ = "/io/github/johnsutray/Screenshooter"
META_SHIFT_S = 0x12000053

failures = []


def check(name, ok, detail=""):
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, ("  (%s)" % detail) if detail else ""), flush=True)
    if not ok:
        failures.append(name)
    return ok


def wait_for(fn, timeout=8.0, step=0.2):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return fn()


bus = Gio.bus_get_sync(Gio.BusType.SESSION)


def has_owner(name):
    return bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner",
                         GLib.Variant("(s)", (name,)), None, 0, 2000, None).unpack()[0]


def action(name):
    bus.call_sync(APP_ID, OBJ, "org.freedesktop.Application", "ActivateAction",
                  GLib.Variant("(sava{sv})", (name, [], {})), None, 0, 5000, None)


def libdir():
    """LIBDIR of the installed app, read from the launcher script."""
    launcher = open(shutil.which("screenshooter")).read()
    return os.path.dirname(re.search(r"exec \S+ (\S+)/screenshooter\.py", launcher).group(1) + "/x")


last_grab_error = [""]


def grab():
    """(width, height, stride, bytes) of the current frame through the installed capture helper,
    or None while KWin can't provide one yet."""
    rfd, wfd = os.pipe()
    proc = subprocess.Popen([os.path.join(libdir(), "screenshooter-grab")], stdout=wfd, stderr=subprocess.PIPE)
    os.close(wfd)
    meta = proc.stderr.readline().decode().split()
    if not meta or meta[0] != "META":
        last_grab_error[0] = " ".join(meta)
        os.close(rfd)
        proc.wait()
        return None
    w, h, stride = int(meta[1]), int(meta[2]), int(meta[3])
    data = bytearray()
    while len(data) < stride * h:
        chunk = os.read(rfd, 1 << 20)
        if not chunk:
            break
        data += chunk
    os.close(rfd)
    proc.wait()
    return w, h, stride, bytes(data)


def frame():
    f = wait_for(grab, 10)
    if f is None:
        raise RuntimeError("no frame from KWin: " + last_grab_error[0])
    return f


def mean(region=None):
    w, h, stride, data = frame()
    x0, y0, x1, y1 = region or (0, 0, w, h)
    vals = []
    for y in range(y0, y1, 7):
        row = y * stride
        vals.extend(data[row + x * 4 + 1] for x in range(x0, x1, 7))  # green channel
    return statistics.mean(vals)


def dark_pixels(region):
    """How many sampled pixels in the region are dark (frames, overlays, editor chrome)."""
    w, h, stride, data = frame()
    x0, y0, x1, y1 = region
    return sum(1 for y in range(y0, y1, 3) for x in range(x0, x1, 3) if data[y * stride + x * 4 + 1] < 60)


def main():
    # A white fullscreen window gives every overlay something visible to darken.
    canvas = subprocess.Popen([sys.executable, "-c", """
import gi; gi.require_version('Gtk', '4.0')
from gi.repository import Gtk
p = Gtk.CssProvider(); p.load_from_string('window { background: white; }')
def act(app):
    w = Gtk.ApplicationWindow(application=app); w.fullscreen(); w.present()
    Gtk.StyleContext.add_provider_for_display(w.get_display(), p, 800)
a = Gtk.Application(application_id='test.WhiteCanvas'); a.connect('activate', act); a.run([])
"""])
    for prop in ("compositingType", "active"):
        try:
            v = bus.call_sync("org.kde.KWin", "/Compositor", "org.freedesktop.DBus.Properties", "Get",
                              GLib.Variant("(ss)", ("org.kde.kwin.Compositing", prop)), None, 0, 3000, None).unpack()[0]
            print("kwin compositor %s: %s" % (prop, v), flush=True)
        except Exception as e:
            print("kwin compositor %s: %s" % (prop, e), flush=True)
    first = wait_for(grab, 30)
    if not check("capture helper returns a frame", first is not None, last_grab_error[0]):
        return finish()
    w, h = first[0], first[1]
    # With software rendering a new window can take a few seconds to show up.
    base = wait_for(lambda: (lambda m: m if m > 200 else None)(mean()), 20) or mean()
    check("white canvas is visible", base > 200, "mean %.0f" % base)

    daemon = subprocess.Popen(["screenshooter", "daemon"])
    check("background instance owns its D-Bus name", wait_for(lambda: has_owner(APP_ID)))

    config = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "screenshooter.conf")
    check("first start writes the config", wait_for(lambda: os.path.exists(config), 5))
    holders = wait_for(lambda: bus.call_sync("org.kde.kglobalaccel", "/kglobalaccel", "org.kde.KGlobalAccel",
                                             "getGlobalShortcutsByKey", GLib.Variant("(i)", (META_SHIFT_S,)),
                                             None, 0, 3000, None).unpack()[0], 5)
    check("first start registers Meta+Shift+S", any(r[2] == APP_ID + ".desktop" for r in holders), str([r[2] for r in holders]))

    action("capture")
    dim = wait_for(lambda: (lambda m: m if m < base * 0.75 else None)(mean()), 6)
    check("region overlay darkens the screen", bool(dim), "mean %.0f -> %s" % (base, dim))
    action("cancel")
    back = wait_for(lambda: (lambda m: m if m > base * 0.95 else None)(mean()), 4)
    check("cancel removes the overlay", bool(back))

    corner = (w - 420, h - 300, w, h)
    corner_base = dark_pixels(corner)
    action("fullscreen")
    # The thumbnail shows the (white) screen inside a dark frame: look for the frame.
    thumb = wait_for(lambda: (lambda n: n if n > corner_base + 300 else None)(dark_pixels(corner)), 6)
    check("whole-screen capture shows a thumbnail", bool(thumb), "dark pixels in the corner %d -> %s" % (corner_base, thumb))
    if shutil.which("wl-paste"):
        types = wait_for(lambda: subprocess.run(["wl-paste", "--list-types"], capture_output=True, text=True).stdout, 5)
        check("whole-screen capture lands in the clipboard as PNG", "image/png" in (types or ""), (types or "").split()[:3])

    action("edit-last")
    editor = wait_for(lambda: (lambda m: m if m < base * 0.9 else None)(mean()), 6)
    check("the editor opens", bool(editor), "mean %.0f -> %s" % (base, editor))

    action("quit")
    daemon.wait(timeout=10)
    canvas.terminate()
    return finish()


def finish():
    result = "FAIL: " + ", ".join(failures) if failures else "PASS"
    print(result, flush=True)
    if os.environ.get("E2E_RESULT"):
        with open(os.environ["E2E_RESULT"], "w") as f:
            f.write(result + "\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
