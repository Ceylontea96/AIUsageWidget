"""Small text stays readable: WCAG contrast of the widget's text colours."""
import unittest

import widget_theme as theme


def luminance(colour):
    channels = [int(colour.lstrip('#')[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(a, b):
    light, dark = sorted((luminance(a), luminance(b)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


# WCAG AA for text under 18 px, which is all of the widget's text but the hero.
SMALL_TEXT = 4.5


class ContrastTests(unittest.TestCase):
    def test_the_chip_label_reads_on_every_chip_colour(self):
        # The label lies across the fill and the empty track, so both count.
        fills = [*theme.CHIP_OK.values(), theme.CHIP_WARN, theme.CHIP_DANGER, theme.CHIP_STALE, theme.CHIP_TRACK]
        for fill in fills:
            with self.subTest(fill=fill):
                self.assertGreaterEqual(contrast(theme.CHIP_FG, fill), SMALL_TEXT)

    def test_dim_and_muted_text_read_on_the_window_and_the_cards(self):
        for colour in (theme.DIM, theme.MUTED, theme.STATUS_FG):
            for background in (theme.BG, theme.CARD):
                with self.subTest(colour=colour, background=background):
                    self.assertGreaterEqual(contrast(colour, background), SMALL_TEXT)

    def test_dim_stays_below_muted(self):
        # Three levels of text: the brighter dim must not catch up with muted.
        self.assertLess(contrast(theme.DIM, theme.CARD), contrast(theme.MUTED, theme.CARD))


if __name__ == '__main__':
    unittest.main()
