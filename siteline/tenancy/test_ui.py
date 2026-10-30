"""Template helpers: text colour on a company's brand colour."""
from django.test import SimpleTestCase

from .templatetags.ui import INK_DARK, INK_LIGHT, contrast_ratio, readable_ink, relative_luminance
from .tests import Base


class ReadableInkTests(SimpleTestCase):
    def test_relative_luminance_matches_wcag(self):
        self.assertAlmostEqual(relative_luminance('#000000'), 0)
        self.assertAlmostEqual(relative_luminance('#FFFFFF'), 1)
        self.assertAlmostEqual(relative_luminance('#808080'), 0.2159, places=4)
        self.assertAlmostEqual(contrast_ratio('#000000', '#ffffff'), 21)

    def test_picks_the_higher_contrast_ink(self):
        for colour, ink in [('#E8590C', INK_DARK),    # the default orange: 5.0:1 with dark ink, 3.6:1 with white
                            ('#FFD43B', INK_DARK),    # yellow
                            ('#A8E6CF', INK_DARK),    # mint
                            ('#FFFFFF', INK_DARK),
                            ('#1D5D9B', INK_LIGHT),   # navy
                            ('#2B8A3E', INK_LIGHT),   # dark green
                            ('#000000', INK_LIGHT)]:
            with self.subTest(colour):
                self.assertEqual(readable_ink(colour), ink)
                other = INK_LIGHT if ink == INK_DARK else INK_DARK
                self.assertGreaterEqual(contrast_ratio(colour, ink), contrast_ratio(colour, other))

    def test_bad_values_fall_back_to_white(self):
        for value in ('', None, 'orange', '#FFF', '#GGGGGG'):
            with self.subTest(value):
                self.assertEqual(readable_ink(value), INK_LIGHT)


class BrandInkInPageTests(Base):
    def test_page_sets_accent_ink_from_brand_colour(self):
        self.org_a.brand_colour = '#FFD43B'
        self.org_a.save()
        self.login(self.alice)
        r = self.client.get(self.url('project_list', self.org_a))
        self.assertContains(r, f'--accent: #FFD43B;--accent-ink: {INK_DARK};')


