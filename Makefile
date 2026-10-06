# Screenshooter — build and install.
#
#   make                                   build
#   sudo make install                      install to /usr/local
#   make PREFIX=/usr DESTDIR=pkg install   staged install for packaging
#   ./install.sh                           per-user install into ~/.local (no root)
#   make PREFIX=/app FLATPAK=1 install     inside a Flatpak build: portals only, no KDE extras

ID       ?= io.github.johnsutray.Screenshooter
PREFIX   ?= /usr/local
BINDIR   ?= $(PREFIX)/bin
LIBDIR   ?= $(PREFIX)/lib/screenshooter
DATADIR  ?= $(PREFIX)/share
APPSDIR  ?= $(DATADIR)/applications
DBUSDIR  ?= $(DATADIR)/dbus-1/services
ICONDIR  ?= $(DATADIR)/icons/hicolor/scalable/apps
AUTOSTARTDIR ?= /etc/xdg/autostart
METAINFODIR ?= $(DATADIR)/metainfo
PYTHON   ?= /usr/bin/python3
FLATPAK  ?=
VERSION  := $(shell sed -n 's/^VERSION = "\(.*\)"/\1/p' src/screenshooter.py)

PKG_CONFIG ?= pkg-config
# Where libgtk4-layer-shell.so.0 lives: from pkg-config if the -dev/-devel package is there,
# otherwise from the dynamic linker cache.
LAYER_SHELL_LIB ?= $(or $(addsuffix /libgtk4-layer-shell.so.0,$(shell $(PKG_CONFIG) --variable=libdir gtk4-layer-shell-0 2>/dev/null)),\
                        $(shell /sbin/ldconfig -p 2>/dev/null | awk '/libgtk4-layer-shell\.so\.0 /{print $$NF; exit}'))
GIO_CFLAGS := $(shell $(PKG_CONFIG) --cflags gio-unix-2.0)
GIO_LIBS   := $(shell $(PKG_CONFIG) --libs gio-unix-2.0)
OBJPATH := $(subst .,/,/$(ID))

CFLAGS ?= -O2 -g
CFLAGS += -Wall -Wextra

B := build
GENERATED := $(B)/screenshooter $(B)/$(ID).desktop $(B)/$(ID).service $(B)/$(ID).metainfo.xml
ifeq ($(FLATPAK),)
# KDE extras: whole-screen hotkey entry, capture helper, autostart. In Flatpak the portals
# (GlobalShortcuts, Background) cover these.
GENERATED += $(B)/$(ID)-fullscreen.desktop $(B)/$(ID)-grab.desktop $(B)/$(ID)-autostart.desktop
HELPER := $(B)/screenshooter-grab
endif

SUBST = sed -e 's|@ID@|$(ID)|g' -e 's|@OBJPATH@|$(OBJPATH)|g' -e 's|@BINDIR@|$(BINDIR)|g' \
            -e 's|@LIBDIR@|$(LIBDIR)|g' -e 's|@PYTHON@|$(PYTHON)|g' -e 's|@LAYER_SHELL_LIB@|$(LAYER_SHELL_LIB)|g' \
            -e 's|@VERSION@|$(VERSION)|g'

.PHONY: all install uninstall clean check
all: $(HELPER) $(GENERATED)

# Generated files embed install paths: rebuild them whenever those paths change.
VARS_STAMP := $(B)/.vars
VARS_NOW := $(ID)|$(BINDIR)|$(LIBDIR)|$(PYTHON)|$(LAYER_SHELL_LIB)|$(FLATPAK)|$(VERSION)
$(shell mkdir -p $(B); [ "$$(cat $(VARS_STAMP) 2>/dev/null)" = '$(VARS_NOW)' ] || echo '$(VARS_NOW)' > $(VARS_STAMP))

$(B):
	mkdir -p $@

$(B)/screenshooter-grab: src/grab.c | $(B)
	$(CC) $(CPPFLAGS) $(CFLAGS) $(GIO_CFLAGS) -o $@ $< $(LDFLAGS) $(GIO_LIBS)

$(B)/screenshooter: src/screenshooter.in Makefile $(VARS_STAMP) | $(B)
	$(SUBST) $< > $@
	chmod 755 $@

$(B)/$(ID).desktop: data/screenshooter.desktop.in Makefile $(VARS_STAMP) | $(B)
ifeq ($(FLATPAK),)
	$(SUBST) $< > $@
else
	# Без X-KDE-Shortcuts: в Flatpak клавиши выдаёт портал, иначе KDE запустил бы снимок дважды.
	$(SUBST) $< | grep -v '^X-KDE-Shortcuts=' > $@
endif
$(B)/$(ID).metainfo.xml: data/screenshooter.metainfo.xml.in Makefile $(VARS_STAMP) | $(B)
	$(SUBST) $< > $@
$(B)/$(ID)-fullscreen.desktop: data/screenshooter-fullscreen.desktop.in Makefile $(VARS_STAMP) | $(B)
	$(SUBST) $< > $@
$(B)/$(ID)-grab.desktop: data/screenshooter-grab.desktop.in Makefile $(VARS_STAMP) | $(B)
	$(SUBST) $< > $@
$(B)/$(ID).service: data/screenshooter.service.in Makefile $(VARS_STAMP) | $(B)
	$(SUBST) $< > $@
$(B)/$(ID)-autostart.desktop: data/screenshooter-autostart.desktop.in Makefile $(VARS_STAMP) | $(B)
	$(SUBST) $< > $@

install: all
	install -Dm755 $(B)/screenshooter            $(DESTDIR)$(BINDIR)/screenshooter
	install -Dm644 src/screenshooter.py          $(DESTDIR)$(LIBDIR)/screenshooter.py
	install -Dm644 $(B)/$(ID).desktop            $(DESTDIR)$(APPSDIR)/$(ID).desktop
	install -Dm644 $(B)/$(ID).service            $(DESTDIR)$(DBUSDIR)/$(ID).service
	install -Dm644 $(B)/$(ID).metainfo.xml       $(DESTDIR)$(METAINFODIR)/$(ID).metainfo.xml
	install -Dm644 data/screenshooter.svg        $(DESTDIR)$(ICONDIR)/$(ID).svg
ifeq ($(FLATPAK),)
	install -Dm755 $(B)/screenshooter-grab       $(DESTDIR)$(LIBDIR)/screenshooter-grab
	install -Dm644 $(B)/$(ID)-fullscreen.desktop $(DESTDIR)$(APPSDIR)/$(ID)-fullscreen.desktop
	install -Dm644 $(B)/$(ID)-grab.desktop       $(DESTDIR)$(APPSDIR)/$(ID)-grab.desktop
	install -Dm644 $(B)/$(ID)-autostart.desktop  $(DESTDIR)$(AUTOSTARTDIR)/$(ID).desktop
endif

uninstall:
	rm -f $(DESTDIR)$(BINDIR)/screenshooter
	rm -rf $(DESTDIR)$(LIBDIR)
	rm -f $(DESTDIR)$(APPSDIR)/$(ID).desktop $(DESTDIR)$(APPSDIR)/$(ID)-fullscreen.desktop $(DESTDIR)$(APPSDIR)/$(ID)-grab.desktop
	rm -f $(DESTDIR)$(DBUSDIR)/$(ID).service $(DESTDIR)$(AUTOSTARTDIR)/$(ID).desktop $(DESTDIR)$(ICONDIR)/$(ID).svg
	rm -f $(DESTDIR)$(METAINFODIR)/$(ID).metainfo.xml

check: all
	$(PYTHON) -m py_compile src/screenshooter.py
	sh -n src/screenshooter.in
	$(PYTHON) tests/test_units.py

clean:
	rm -rf $(B)
