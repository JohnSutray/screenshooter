#!/usr/bin/env bash
# Run tests/e2e.py inside a virtual (headless) KWin with its own D-Bus session and config dir.
# Safe to run inside a desktop session: nothing touches the real session, its windows or hotkeys.
set -euo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/config" "$work/runtime"
chmod 700 "$work/runtime"

export XDG_CONFIG_HOME=$work/config
export XDG_RUNTIME_DIR=$work/runtime
export E2E_RESULT=$work/result
unset WAYLAND_DISPLAY DISPLAY
# KWin picks a software renderer when there is no GPU (containers, CI).
export LIBGL_ALWAYS_SOFTWARE=${LIBGL_ALWAYS_SOFTWARE:-1}

dbus-run-session -- kwin_wayland --virtual --no-lockscreen --width 1280 --height 800 \
    --socket "screenshooter-e2e-$$" --exit-with-session "python3 $here/e2e.py" 2>"$work/kwin.log" || true

if [[ -s $E2E_RESULT ]]; then
    cat "$E2E_RESULT"
    [[ $(cat "$E2E_RESULT") == PASS ]]
else
    echo "e2e did not finish; KWin log:" >&2
    tail -40 "$work/kwin.log" >&2
    exit 1
fi
