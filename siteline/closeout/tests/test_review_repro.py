"""
Review of the close-out branch: one test per defect found, each failing before its fix.
"""
import shutil
import sys
import tempfile
import types
from datetime import date, timedelta
from unittest import mock

from django.test import override_settings
from django.utils import timezone

from contracts.models import MainContract
from projects.models import Document, DocumentRevision
from tenancy.context import org_context

from .. import goldenthread, rules, services
from ..models import HandoverItem, HandoverPack
from .base import CloseoutTestBase, pdf
from .test_closeout import kinds

MEDIA = tempfile.mkdtemp()


def tearDownModule():
    shutil.rmtree(MEDIA, ignore_errors=True)


@override_settings(MEDIA_ROOT=MEDIA)
class InternalDrawingsStayOutOfThePackTests(CloseoutTestBase):
    """Issuing a pack lets the client download every item: internal-only drawings must not be in it."""

    def test_internal_and_never_issued_drawings_are_not_handed_to_the_client(self):
        with org_context(self.org_a):
            internal = Document.objects.create(project=self.pa, title='Internal cost markup', reference='INT-01',
                                               category='DRAWING')
            internal.add_revision(DocumentRevision(revision='P01', file=pdf('int.pdf', b'%PDF-1.4 internal'),
                                                   original_name='int.pdf'))
            shared = Document.objects.create(project=self.pa, title='GA plan', reference='GA-01', category='DRAWING',
                                             visibility=Document.VIS_SHARED)
            shared.add_revision(DocumentRevision(revision='C01', file=pdf('ga.pdf', b'%PDF-1.4 ga'),
                                                 original_name='ga.pdf'))
            pack = services.build_pack(self.pa, self.pat)
            titles = set(pack.items.values_list('title', flat=True))
        self.assertIn('GA plan', titles)
        self.assertNotIn('Internal cost markup', titles)
        self.assertEqual(pack.status, HandoverPack.READY)


def fake_quality(**modules):
    """A stand-in `quality` app (it isn't installed on main) exposing only the given modules."""
    pkg = types.ModuleType('quality')
    pkg.__path__ = []
    mods = {'quality': pkg}
    for name, attrs in modules.items():
        m = types.ModuleType(f'quality.{name}')
        for k, v in attrs.items():
            setattr(m, k, v)
        mods[f'quality.{name}'] = m
    return mock.patch.dict(sys.modules, mods), mock.patch('closeout.goldenthread.quality_installed',
                                                          return_value=True)


class FireStoppingRegisterHookTests(CloseoutTestBase):
    """The golden thread reads stream A's quality.exports.register explicitly, not by guessing model names."""

    def test_register_rows_become_a_formula_safe_csv_snapshot(self):
        seen = []

        def register(project):
            seen.append(project)
            return ['Ref', 'Where', 'Installed by'], [['FS-001', '=HYPERLINK("x")', 'Acme Fire'], ['FS-002', 'Riser', '']]
        mods, installed = fake_quality(exports={'register': register})
        with mods, installed, org_context(self.org_a):
            items = goldenthread.quality_items(self.pa)
        self.assertEqual(seen, [self.pa])
        self.assertEqual(len(items), 1)
        src = items[0]
        self.assertEqual((src.category, src.title), (HandoverItem.FIRE_STOPPING, 'Fire-stopping register (2 rows)'))
        name, body = src.generated
        text = body.decode('utf-8-sig')
        self.assertTrue(name.endswith('.csv'))
        self.assertIn('FS-001', text)
        self.assertIn("'=HYPERLINK", text)  # formula-safe

    def test_installed_without_either_hook_is_empty_not_an_error(self):
        mods, installed = fake_quality()
        with mods, installed, org_context(self.org_a):
            self.assertEqual(goldenthread.quality_items(self.pa), [])

    def test_handover_hook_wins_over_the_register(self):
        mods, installed = fake_quality(handover={'golden_thread_items': lambda project: []},
                                       exports={'register': lambda project: self.fail('register was read')})
        with mods, installed, org_context(self.org_a):
            self.assertEqual(goldenthread.quality_items(self.pa), [])


class ContractSyncTests(CloseoutTestBase):
    """MainContract.practical_completion_on and the whole-works PC date stay in step both ways."""

    def test_setting_up_the_contract_after_pc_keeps_the_recorded_date(self):
        with org_context(self.org_a):
            self.contract.delete()
        s = self.section(pc=date(2026, 6, 1))
        with org_context(self.org_a):
            contract = MainContract.objects.create(project=self.pa, form='JCT_IC', defects_period_months=6)
            self.assertEqual(MainContract.objects.get(pk=contract.pk).practical_completion_on, date(2026, 6, 1))
        self.assertEqual(self.fresh(s).pc_date, date(2026, 6, 1))

    def test_pc_typed_only_on_the_contract_turns_snags_into_defects_and_reminds_the_pm(self):
        today = timezone.localdate()
        with org_context(self.org_a):
            self.contract.practical_completion_on = rules.add_months(today, -6) + timedelta(days=10)
            self.contract.save()
            self.pa.manager = self.pat
            self.pa.save()
        self.assertEqual(self.issue().issue_type, 'DEFECT')
        self.assertEqual(len(kinds(self, self.pat, 'defects_end')), 1)
        # clearing it on the contract clears it on the tab, and snags are snags again
        with org_context(self.org_a):
            self.contract.practical_completion_on = None
            self.contract.save()
            self.assertIsNone(services.whole_works(self.pa).pc_date)
        self.assertEqual(self.issue().issue_type, 'SNAG')
