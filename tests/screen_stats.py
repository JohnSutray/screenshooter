#!/usr/bin/env python3
"""Grab the screen with the capture helper and print simple statistics.

    tests/screen_stats.py mean               mean brightness of the whole screen
    tests/screen_stats.py region X Y W H     mean brightness of a region
    tests/screen_stats.py save PATH          save the frame as PNG
"""
import os
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import screenshooter as S  # noqa: E402


def main(argv):
    cap = S.Capture.grab_kde()
    if argv[0] == "save":
        cap.surface.write_to_png(argv[1])
        return
    surf = cap.surface if argv[0] == "mean" else cap.crop(*map(int, argv[1:5]))
    data = bytes(surf.get_data())
    print(round(statistics.mean(data[i] for i in range(0, len(data), 4 * 97)), 1))


if __name__ == "__main__":
    main(sys.argv[1:])
