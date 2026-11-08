"""
A £0 line whose Comments say someone else does it ("client direct", "by others", any case, anywhere in the
comment) is By others and lands on the pack's Not allowed list, in both built-in layouts. Every name is invented.
"""
import copy

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase

from handoverpack import engine, mapping, pack as packs, sample, sample_select, services
from handoverpack.models import HandoverPack, PricingSettings
from handoverpack.sample_select import line
from tenancy.context import org_context

from .test_views import Base

PHRASES = ['Supplied CLIENT DIRECT on their own order', 'Fitted by Others (landlord\'s contractor)',
           'client direct', 'BY OTHERS']


def standard_file(comment):
    s = copy.deepcopy(sample.DEMO)
    e6 = next(ln for ln in s['trades']['Electrical']['lines'] if ln['ref'] == 'E6')
    e6['slots'] = [(sample.D('0'), None, comment), (sample.D('0'), None, '')]
    return sample.build(s), 'E6'


def select_file(comment):
    s = copy.deepcopy(sample_select.DEMO)
    s['trades']['Electrical']['lines'].append(
        line('E9', 'Data cabling to dental chairs', 40, 'nr', (0, None, comment), (0, None, ''), (0, None, '')))
    return sample_select.build(s), 'E9'


class WordListTests(SimpleTestCase):
    def test_both_presets_carry_both_phrases_and_keep_the_old_words(self):
        for key in mapping.PRESETS:
            words = mapping.get(key)['by_others_words']
            with self.subTest(key):
                self.assertIn('client direct', words)
                self.assertIn('by others', words)
                self.assertTrue(set(engine.BY_OTHERS_WORDS) <= set(words))

    def test_a_bad_word_list_is_refused(self):
        for key in mapping.PRESETS:
            m = mapping.get(key)
            m['by_others_words'] = 'by others'
            with self.subTest(key), self.assertRaises(mapping.MappingError):
                mapping.validate(m) if key == 'standard' else mapping.validate_select(m)


class NotAllowedTests(Base):
    def check(self, preset, build):
        with org_context(self.org_a):
            PricingSettings.objects.update_or_create(organization=self.org_a, defaults={'preset': preset})
        for phrase in PHRASES:
            data, ref = build(phrase)
            with self.subTest(preset=preset, phrase=phrase), org_context(self.org_a):
                wb = services.upload(self.bid, self.eve, SimpleUploadedFile('p.xlsx', data))
                self.assertEqual(wb.mapping_key, preset)
                ctx = packs.build(self.bid, HandoverPack.objects.get(project=self.bid), wb)
                row = next(x for x in ctx['excluded'] if x['section'] == 'Electrical' and x['ref'] == ref)
                self.assertEqual(row['cls'], engine.BY_OTHERS)
                self.assertEqual(row['label'], 'By others')
                self.assertEqual(row['why'], phrase)
                self.assertNotIn(ref, [x['ref'] for x in ctx['scope'] if x['section'] == 'Electrical'])

    def test_standard_layout(self):
        self.check('standard', standard_file)

    def test_supplier_select_layout(self):
        self.check('supplier-select', select_file)
