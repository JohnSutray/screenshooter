#!/usr/bin/env python3
"""Unit tests that need no display: key parsing, shape geometry, export bounds, i18n."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import screenshooter as S  # noqa: E402


class KeyParsing(unittest.TestCase):
    def test_qt_codes(self):
        self.assertEqual(S.parse_qt_key("Meta+Shift+S"), 0x12000053)
        self.assertEqual(S.parse_qt_key("Shift+Print"), 0x02000000 | 0x01000009)
        self.assertEqual(S.parse_qt_key("ctrl + alt + p"), 0x0C000050)
        self.assertEqual(S.parse_qt_key("F1"), 0x01000030)
        self.assertEqual(S.parse_qt_key("F12"), 0x0100003B)

    def test_qt_rejects_garbage(self):
        for bad in ("", "Hyper+S", "Meta+Shift+NoSuchKey"):
            with self.assertRaises(ValueError):
                S.parse_qt_key(bad)

    def test_xdg_triggers(self):
        self.assertEqual(S.xdg_trigger("Meta+Shift+S"), "LOGO+SHIFT+s")
        self.assertEqual(S.xdg_trigger("Shift+Print"), "SHIFT+Print")
        self.assertEqual(S.xdg_trigger("Ctrl+Alt+PageDown"), "CTRL+ALT+Page_Down")
        self.assertEqual(S.xdg_trigger("F5"), "F5")

    def test_high_function_keys_follow_the_keymap(self):
        # F13–F24 map to whatever the keymap really emits; both paths must agree on it.
        name = S.physical_fkey_name(15)
        self.assertTrue(name)
        self.assertEqual(S.xdg_trigger("F15"), name)
        if name == "F15":
            self.assertEqual(S.parse_qt_key("F15"), 0x0100003E)
        else:
            self.assertNotEqual(S.parse_qt_key("F15"), 0x0100003E)


class Shapes(unittest.TestCase):
    def test_rect_bbox_includes_stroke(self):
        s = S.Shape("rect", (1, 0, 0, 1), 4, (10, 10))
        s.add((50, 30))
        self.assertEqual(s.geo_bbox(), (10, 10, 50, 30))
        x0, y0, x1, y1 = s.bbox()
        self.assertLess(x0, 10)
        self.assertGreater(x1, 50)

    def test_move_and_fit(self):
        s = S.Shape("ellipse", (1, 0, 0, 1), 2, (0, 0))
        s.add((10, 20))
        s.move(5, 5)
        self.assertEqual(s.geo_bbox(), (5, 5, 15, 25))
        s.fit_to((0, 0, 40, 40))
        self.assertEqual(s.geo_bbox(), (0, 0, 40, 40))

    def test_text_scales_with_height(self):
        t = S.Shape("text", (1, 1, 1, 1), 4, (0, 0), "Hi\nthere", 20)
        x0, y0, x1, y1 = t.geo_bbox()
        t.fit_to((0, 0, x1, y0 + (y1 - y0) * 2))
        self.assertAlmostEqual(t.size, 40, delta=0.5)

    def test_hit_testing(self):
        line = S.Shape("pen", (1, 0, 0, 1), 4, (0, 0))
        line.add((100, 0))
        self.assertTrue(line.hit(50, 1, 2))
        self.assertFalse(line.hit(50, 30, 2))

    def test_copy_is_independent(self):
        a = S.Shape("arrow", (1, 0, 0, 1), 4, (0, 0))
        a.add((10, 10))
        b = a.copy()
        b.move(100, 100)
        self.assertEqual(a.points[0], (0, 0))


class I18n(unittest.TestCase):
    def test_every_russian_string_is_used(self):
        src = open(S.__file__, encoding="utf-8").read()
        for key in S._RU:
            self.assertIn('"%s"' % key.replace("\\", "\\\\"), src, key)


if __name__ == "__main__":
    unittest.main(verbosity=1)
