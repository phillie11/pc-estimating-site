"""export_data / import_data: the whole-database move works with the fail-closed tenant managers."""
import gzip
import json
import os
import tempfile

from django.core.management import call_command

from directory.models import Subcontractor, Trade

from .context import org_context
from .tests import Base


class ExportDataTests(Base):
    def setUp(self):
        super().setUp()
        with org_context(self.org_a):
            self.sub_a.trades.add(Trade.objects.create(name='Specialist glazing'))  # many-to-many is what plain dumpdata trips on

    def export(self):
        path = os.path.join(tempfile.mkdtemp(), 'data.json.gz')
        call_command('export_data', output=path, verbosity=0)
        with gzip.open(path, 'rt') as f:
            return path, json.load(f)

    def test_exports_every_company_with_no_org_context(self):
        _, rows = self.export()
        names = {r['fields'].get('name') for r in rows if r['model'] == 'projects.project'}
        self.assertEqual(names, {'Alpha secret job', 'Bravo job'})
        sub = next(r for r in rows if r['model'] == 'directory.subcontractor')
        self.assertEqual(len(sub['fields']['trades']), 1)

    def test_import_restores_rows_and_many_to_many(self):
        path, _ = self.export()
        sub_pk = self.sub_a.pk
        Subcontractor.all_objects.all().delete()
        Trade.all_objects.all().delete()
        call_command('import_data', path, verbosity=0)
        restored = Subcontractor.all_objects.get(pk=sub_pk)
        self.assertEqual([t.name for t in restored.trades(manager='all_objects').all()], ['Specialist glazing'])
