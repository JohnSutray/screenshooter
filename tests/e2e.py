#!/usr/bin/env python3
"""End-to-end test inside a headless KWin.

    tests/run-e2e.sh            # starts a virtual KWin with its own D-Bus session and runs this

Uses the installed `screenshooter` from PATH. Windows are checked through KWin itself: a small
KWin script reports every window back to this process over D-Bus. That covers the layer-shell
overlay, the thumbnail and the editor anywhere, including CI machines without a GPU.

When KWin composites with OpenGL, frames are grabbed too and checked pixel by pixel, and the
app captures the real screen. Without OpenGL KWin can't take screenshots at all, so the app gets
a prepared frame through SCREENSHOOTER_TEST_FRAME instead.

Writes PASS/FAIL lines to stdout and the overall result to $E2E_RESULT.
"""
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib  # noqa: E402

APP_ID = "io.github.johnsutray.Screenshooter"
OBJ = "/io/github/johnsutray/Screenshooter"
CANVAS_ID = "test.WhiteCanvas"
REPORTER = "test.ScreenshooterE2E"
META_SHIFT_S = 0x12000053

failures = []
work = tempfile.mkdtemp(prefix="screenshooter-e2e-")
bus = Gio.bus_get_sync(Gio.BusType.SESSION)
ctx = GLib.MainContext.default()


def check(name, ok, detail=""):
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, ("  (%s)" % detail) if detail else ""), flush=True)
    if not ok:
        failures.append(name)
    return ok


def skip(name, why):
    print("SKIP  %s  (%s)" % (name, why), flush=True)


def pump(seconds):
    """Sleep while still answering D-Bus calls (KWin scripts report back to us)."""
    end = time.time() + seconds
    while time.time() < end:
        ctx.iteration(False)
        time.sleep(0.02)


def wait_for(fn, timeout=8.0, step=0.2):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        pump(step)
    return fn()


def has_owner(name):
    return bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner",
                         GLib.Variant("(s)", (name,)), None, 0, 2000, None).unpack()[0]


def action(name):
    bus.call_sync(APP_ID, OBJ, "org.freedesktop.Application", "ActivateAction",
                  GLib.Variant("(sava{sv})", (name, [], {})), None, 0, 5000, None)


# --------------------------------------------------------------------------- windows, through KWin

KWIN_SCRIPT = """
const ws = workspace.stackingOrder;
const out = [];
for (let i = 0; i < ws.length; i++) {
    const w = ws[i];
    out.push([w.resourceClass, w.caption, Math.round(w.frameGeometry.width), Math.round(w.frameGeometry.height)].join("|"));
}
callDBus("%s", "/", "%s", "Report", out.join("\\n"));
""" % (REPORTER, REPORTER)
_reports = []
_script = {"path": None, "n": 0}


def _on_report(_conn, _sender, _path, _iface, _method, params, invocation):
    _reports.append(params.unpack()[0])
    invocation.return_value(None)


def setup_reporter():
    xml = '<node><interface name="%s"><method name="Report"><arg type="s" direction="in"/></method></interface></node>' % REPORTER
    bus.register_object("/", Gio.DBusNodeInfo.new_for_xml(xml).interfaces[0], _on_report, None, None)
    Gio.bus_own_name_on_connection(bus, REPORTER, Gio.BusNameOwnerFlags.NONE, None, None)
    _script["path"] = os.path.join(work, "windows.js")
    with open(_script["path"], "w") as f:
        f.write(KWIN_SCRIPT)
    pump(0.3)


def windows():
    """[(resource_class, caption, width, height)] of every window KWin manages."""
    _script["n"] += 1
    name = "screenshooter-e2e-%d" % _script["n"]
    _reports.clear()
    sid = bus.call_sync("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", "loadScript",
                        GLib.Variant("(ss)", (_script["path"], name)), None, 0, 3000, None).unpack()[0]
    bus.call_sync("org.kde.KWin", "/Scripting/Script%d" % sid, "org.kde.kwin.Script", "run", None, None, 0, 3000, None)
    wait_for(lambda: _reports, 3, 0.05)
    bus.call_sync("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", "unloadScript",
                  GLib.Variant("(s)", (name,)), None, 0, 3000, None)
    out = []
    for line in (_reports[0].splitlines() if _reports else []):
        parts = line.split("|")
        out.append((parts[0], "|".join(parts[1:-2]), int(parts[-2]), int(parts[-1])))
    return out


def surfaces(ws):
    """Layer-shell surfaces carry no app ID: everything except the canvas and regular app windows."""
    return [w for w in ws if w[0] not in (APP_ID, CANVAS_ID)]


# --------------------------------------------------------------------------- frames (OpenGL only)

last_grab_error = [""]


def libdir():
    launcher = open(shutil.which("screenshooter")).read()
    return re.search(r"exec \S+ (\S+)/screenshooter\.py", launcher).group(1)


def grab():
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
    return statistics.mean(data[y * stride + x * 4 + 1] for y in range(y0, y1, 7) for x in range(x0, x1, 7))


def dark_pixels(region):
    w, h, stride, data = frame()
    x0, y0, x1, y1 = region
    return sum(1 for y in range(y0, y1, 3) for x in range(x0, x1, 3) if data[y * stride + x * 4 + 1] < 60)


# --------------------------------------------------------------------------- the test


def compositing_type():
    try:
        return bus.call_sync("org.kde.KWin", "/Compositor", "org.freedesktop.DBus.Properties", "Get",
                             GLib.Variant("(ss)", ("org.kde.kwin.Compositing", "compositingType")),
                             None, 0, 3000, None).unpack()[0]
    except Exception as e:
        return "unknown (%s)" % e


def test_frame_png(width, height):
    import cairo

    surf = cairo.ImageSurface(cairo.FORMAT_RGB24, width, height)
    cr = cairo.Context(surf)
    g = cairo.LinearGradient(0, 0, width, height)
    g.add_color_stop_rgb(0, 0.9, 0.5, 0.1)
    g.add_color_stop_rgb(1, 0.1, 0.4, 0.9)
    cr.set_source(g)
    cr.paint()
    path = os.path.join(work, "frame.png")
    surf.write_to_png(path)
    return path


def main():
    setup_reporter()
    comp = compositing_type()
    gl = str(comp).startswith("gl")
    print("kwin compositing: %s%s" % (comp, "" if gl else "; window checks only, the app gets a test frame"), flush=True)

    # A white fullscreen window gives every overlay something visible to darken.
    canvas = subprocess.Popen([sys.executable, "-c", """
import gi; gi.require_version('Gtk', '4.0')
from gi.repository import Gtk
p = Gtk.CssProvider(); p.load_from_string('window { background: white; }')
def act(app):
    w = Gtk.ApplicationWindow(application=app); w.fullscreen(); w.present()
    Gtk.StyleContext.add_provider_for_display(w.get_display(), p, 800)
a = Gtk.Application(application_id='%s'); a.connect('activate', act); a.run([])
""" % CANVAS_ID])
    canvas_win = wait_for(lambda: [w for w in windows() if w[0] == CANVAS_ID], 20)
    check("KWin reports windows through a script", bool(canvas_win), str(windows()))
    sw, sh = (canvas_win[0][2], canvas_win[0][3]) if canvas_win else (1280, 800)

    base = None
    if gl:
        base = wait_for(lambda: (lambda m: m if m > 200 else None)(mean()), 20) or mean()
        check("white canvas is visible in grabbed frames", base > 200, "mean %.0f" % base)

    env = dict(os.environ)
    if not gl:
        env["SCREENSHOOTER_TEST_FRAME"] = test_frame_png(sw, sh)
    daemon = subprocess.Popen(["screenshooter", "daemon"], env=env)
    check("background instance owns its D-Bus name", wait_for(lambda: has_owner(APP_ID)))

    config = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "screenshooter.conf")
    check("first start writes the config", wait_for(lambda: os.path.exists(config), 5))
    holders = wait_for(lambda: bus.call_sync("org.kde.kglobalaccel", "/kglobalaccel", "org.kde.KGlobalAccel",
                                             "getGlobalShortcutsByKey", GLib.Variant("(i)", (META_SHIFT_S,)),
                                             None, 0, 3000, None).unpack()[0], 5)
    check("first start registers Meta+Shift+S", any(r[2] == APP_ID + ".desktop" for r in holders), str([r[2] for r in holders]))

    # Region capture: a fullscreen layer-shell overlay.
    def overlays():
        return [w for w in surfaces(windows()) if (w[2], w[3]) == (sw, sh)]

    action("capture")
    check("region capture opens a fullscreen overlay", bool(wait_for(overlays, 6)), str(windows()))
    if gl:
        dim = wait_for(lambda: (lambda m: m if m < base * 0.75 else None)(mean()), 6)
        check("the overlay darkens the frozen frame", bool(dim), "mean %.0f -> %s" % (base, dim))
    action("cancel")
    check("cancel closes the overlay", wait_for(lambda: not overlays(), 5))

    # Whole screen: thumbnail in the corner and PNG in the clipboard.
    corner = (sw - 420, sh - 300, sw, sh)
    corner_base = dark_pixels(corner) if gl else 0
    action("fullscreen")
    thumb = wait_for(lambda: [w for w in surfaces(windows()) if w[2] < sw / 2 and w[3] < sh / 2], 6)
    check("whole-screen capture shows a thumbnail", bool(thumb), str(windows()))
    if gl:
        n = wait_for(lambda: (lambda d: d if d > corner_base + 300 else None)(dark_pixels(corner)), 6)
        check("the thumbnail is drawn in the corner", bool(n), "dark pixels %d -> %s" % (corner_base, n))
    if shutil.which("wl-paste"):
        types = wait_for(lambda: subprocess.run(["wl-paste", "--list-types"], capture_output=True, text=True, timeout=5).stdout, 6)
        check("whole-screen capture lands in the clipboard as PNG", "image/png" in (types or ""), (types or "").split()[:3])
    else:
        skip("clipboard", "wl-paste not installed")

    # Editor for the last shot.
    action("edit-last")
    check("the editor window opens", bool(wait_for(lambda: [w for w in windows() if w[0] == APP_ID], 6)), str(windows()))
    if gl:
        darker = wait_for(lambda: (lambda m: m if m < base * 0.9 else None)(mean()), 6)
        check("the editor is drawn", bool(darker), "mean %.0f -> %s" % (base, darker))

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
    shutil.rmtree(work, ignore_errors=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
