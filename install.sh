#!/usr/bin/env bash
# Per-user install from source into ~/.local (no root needed).
#   ./install.sh              install, register hotkeys, start the background instance
#   ./install.sh --uninstall  remove everything again
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

ID=io.github.johnsutray.Screenshooter
OBJPATH=/io/github/johnsutray/Screenshooter
VARS=(PREFIX="$HOME/.local" AUTOSTARTDIR="${XDG_CONFIG_HOME:-$HOME/.config}/autostart")
BIN=$HOME/.local/bin/screenshooter

running() { busctl --user list 2>/dev/null | grep -q "^$ID "; }
stop_instance() {
    if running; then
        gdbus call --session --dest "$ID" --object-path "$OBJPATH" \
            --method org.freedesktop.Application.ActivateAction quit '[]' '{}' >/dev/null 2>&1 || true
        sleep 0.3
    fi
}

if [[ ${1:-} == --uninstall ]]; then
    [[ -x $BIN ]] && "$BIN" uninstall-hotkey || true
    stop_instance
    make "${VARS[@]}" uninstall >/dev/null
    kbuildsycoca6 --noincremental >/dev/null 2>&1 || true
    echo "Screenshooter removed. Your config stays in ~/.config/screenshooter.conf."
    exit 0
fi

stop_instance
make "${VARS[@]}" >/dev/null
make "${VARS[@]}" install >/dev/null
kbuildsycoca6 --noincremental >/dev/null 2>&1 || true
"$BIN" install-hotkey
setsid -f "$BIN" daemon >/dev/null 2>&1
sleep 0.8
if running; then
    echo "Done. Press your region hotkey to take a screenshot."
else
    echo "The background instance didn't start; try: $BIN daemon" >&2
    exit 1
fi
