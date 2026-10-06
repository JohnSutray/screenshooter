#!/usr/bin/env python3
"""Press a key combination through a virtual keyboard (/dev/uinput), like real hardware.

    tests/inject_key.py 125 42 31     # Meta+Shift+S (evdev key codes)
    tests/inject_key.py 185           # F15

Needs write access to /dev/uinput. For local end-to-end checks of global shortcuts.
"""
import fcntl
import os
import struct
import sys
import time

UI_SET_EVBIT, UI_SET_KEYBIT, UI_DEV_CREATE, UI_DEV_DESTROY, UI_DEV_SETUP = 0x40045564, 0x40045565, 0x5501, 0x5502, 0x405C5503
EV_SYN, EV_KEY = 0, 1


def main(keys):
    fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
    fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
    for k in range(1, 250):
        fcntl.ioctl(fd, UI_SET_KEYBIT, k)
    fcntl.ioctl(fd, UI_DEV_SETUP, struct.pack("HHHH80sI", 0x03, 0x1234, 0x5678, 1, b"screenshooter-test-kbd", 0))
    fcntl.ioctl(fd, UI_DEV_CREATE)
    time.sleep(1.5)  # the compositor has to pick up the new device

    def emit(code, value):
        os.write(fd, struct.pack("qqHHi", 0, 0, EV_KEY, code, value))
        os.write(fd, struct.pack("qqHHi", 0, 0, EV_SYN, 0, 0))
        time.sleep(0.03)

    for k in keys:
        emit(k, 1)
    for k in reversed(keys):
        emit(k, 0)
    time.sleep(0.5)
    fcntl.ioctl(fd, UI_DEV_DESTROY)
    os.close(fd)


if __name__ == "__main__":
    main([int(k) for k in sys.argv[1:]])
