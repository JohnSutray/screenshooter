/* screenshooter-grab: запросить у KWin кадр рабочего пространства и отдать его в stdout.
 *
 * Зачем отдельный процесс: KWin не включает в снимок окна того клиента, который этот
 * снимок запросил. Демон показывает редактор и миниатюру, поэтому снимать должен кто-то
 * другой. Метаданные (ширина, высота, stride, формат, масштаб) печатаются в stderr одной
 * строкой «META …», сырые пиксели KWin пишет прямо в fd 1.
 */
#include <gio/gio.h>
#include <gio/gunixfdlist.h>
#include <stdio.h>
#include <string.h>

int main(int argc, char **argv) {
    gboolean cursor = argc > 1 && strcmp(argv[1], "--cursor") == 0;
    GError *err = NULL;
    GDBusConnection *bus = g_bus_get_sync(G_BUS_TYPE_SESSION, NULL, &err);
    if (!bus) { fprintf(stderr, "ERR %s\n", err->message); return 1; }

    GUnixFDList *fdl = g_unix_fd_list_new();
    int idx = g_unix_fd_list_append(fdl, 1, &err);
    if (idx < 0) { fprintf(stderr, "ERR %s\n", err->message); return 1; }

    GVariantBuilder b;
    g_variant_builder_init(&b, G_VARIANT_TYPE("a{sv}"));
    g_variant_builder_add(&b, "{sv}", "include-cursor", g_variant_new_boolean(cursor));
    g_variant_builder_add(&b, "{sv}", "native-resolution", g_variant_new_boolean(TRUE));
    GVariant *params = g_variant_new("(a{sv}h)", &b, idx);

    GVariant *reply = g_dbus_connection_call_with_unix_fd_list_sync(
        bus, "org.kde.KWin", "/org/kde/KWin/ScreenShot2", "org.kde.KWin.ScreenShot2", "CaptureWorkspace",
        params, G_VARIANT_TYPE("(a{sv})"), G_DBUS_CALL_FLAGS_NONE, 5000, fdl, NULL, NULL, &err);
    if (!reply) { fprintf(stderr, "ERR %s\n", err->message); return 1; }

    GVariant *dict = NULL;
    g_variant_get(reply, "(@a{sv})", &dict);
    guint32 w = 0, h = 0, stride = 0, fmt = 0;
    double scale = 1.0;
    g_variant_lookup(dict, "width", "u", &w);
    g_variant_lookup(dict, "height", "u", &h);
    g_variant_lookup(dict, "stride", "u", &stride);
    g_variant_lookup(dict, "format", "u", &fmt);
    g_variant_lookup(dict, "scale", "d", &scale);
    fprintf(stderr, "META %u %u %u %u %g\n", w, h, stride, fmt, scale);
    fflush(stderr);
    /* KWin пишет пиксели в свою копию fd асинхронно; наш fd 1 можно закрывать. */
    return 0;
}
