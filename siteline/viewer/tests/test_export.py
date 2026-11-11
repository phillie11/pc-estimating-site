"""
The vector export of mark-ups (viewer/export.py, the viewer.markup_export job, the two views): shapes land at
the right page coordinates on a rotated page and on a page that isn't A4, only a fixed set of drawing
operators is ever written whatever a label says, hostile drawings are read safely, and who may ask for an
export and download it matches viewing the drawing (the other company, withdrawn revisions, the virus check).
"""
import io
import re
from unittest import mock

from django.test import SimpleTestCase
from pypdf import PdfReader
from pypdf.generic import ContentStream

from documents.tests.base import upload
from documents.tests.test_pdftext import assemble, hostile_pdf, real_pdf, stream_obj
from jobs.models import Job
from projects.models import DocumentRevision, FileScan
from tenancy.context import org_context
from tenancy.models import Notification
from viewer import export
from viewer.models import MarkupExport, MarkupLayer
from viewer.tasks import markup_export

from .test_markup import CLOUD, MarkupBase

ALLOWED_OPERATORS = {'q', 'Q', 'cm', 'w', 'J', 'j', 'RG', 'rg', 'm', 'l', 'c', 'h', 're', 'S', 'f', 'BT', 'ET',
                     'Tf', 'Tm', 'Tj', 'Tr'}


def drawing(media=(0, 0, 595, 842), rotate=None, crop=None, parent_rotate=None):
    """A one-page drawing with a line of text on it, built by hand, so the page box and turn are exactly known."""
    def box(b):
        return '[' + ' '.join(str(v) for v in b) + ']'
    page = f'<< /Type /Page /Parent 2 0 R /MediaBox {box(media)} /Contents 4 0 R ' \
           f'/Resources << /Font << /F1 5 0 R >> >>'
    page += f' /Rotate {rotate}' if rotate is not None else ''
    page += f' /CropBox {box(crop)}' if crop else ''
    return assemble([
        b'<< /Type /Catalog /Pages 2 0 R >>',
        f'<< /Type /Pages /Kids [3 0 R] /Count 1{f" /Rotate {parent_rotate}" if parent_rotate else ""} >>'.encode(),
        (page + ' >>').encode(),
        stream_obj(b'BT /F1 12 Tf 20 100 Td (Drawing text) Tj ET'),
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>'])


def tokens(content):
    return content.decode('ascii').split()


def user_space(content):
    """
    Read an overlay stream: apply its `cm` to every point it draws. Returns {'rects': [(x0, y0, x1, y1)],
    'lines': [((x, y), (x, y))], 'text': [(x, y)]} in the page's own coordinates.
    """
    a = b = c = d = e = f = None
    stack, out = [], {'rects': [], 'lines': [], 'text': []}
    last_move = None

    def at(x, y):
        return (a * x + c * y + e, b * x + d * y + f)
    for tok in tokens(content):
        try:
            stack.append(float(tok))
            continue
        except ValueError:
            pass
        if tok == 'cm' and a is None:
            a, b, c, d, e, f = stack[-6:]
        elif tok == 're':
            x, y, w, h = stack[-4:]
            pts = [at(x, y), at(x + w, y + h)]
            out['rects'].append((min(p[0] for p in pts), min(p[1] for p in pts),
                                 max(p[0] for p in pts), max(p[1] for p in pts)))
        elif tok == 'm':
            last_move = at(*stack[-2:])
        elif tok == 'l' and last_move is not None:
            out['lines'].append((last_move, at(*stack[-2:])))
            last_move = None
        elif tok == 'Tm':
            out['text'].append(at(*stack[-2:]))
        stack = []
    return out


def close(test, got, want, places=2):
    test.assertEqual(len(got), len(want), (got, want))
    for g, w in zip(got, want):
        test.assertAlmostEqual(g, w, places=places, msg=f'{got} != {want}')


class PageMatrixTests(SimpleTestCase):
    """The four corners of the displayed page land on the right corners of the page itself."""
    BOX = (10, 20, 610, 320)  # 600 wide, 300 high, not at the origin

    def corners(self, rotation):
        a, b, c, d, e, f = export.page_matrix(self.BOX, rotation)
        w, h = export.displayed_size(self.BOX, rotation)
        return [(round(a * x + c * y + e, 6), round(b * x + d * y + f, 6)) for x, y in ((0, 0), (w, 0), (w, h), (0, h))]

    def test_each_turn(self):
        # Displayed top left, top right, bottom right, bottom left; the page turned clockwise by the angle
        self.assertEqual(self.corners(0), [(10, 320), (610, 320), (610, 20), (10, 20)])
        self.assertEqual(self.corners(90), [(10, 20), (10, 320), (610, 320), (610, 20)])
        self.assertEqual(self.corners(180), [(610, 20), (10, 20), (10, 320), (610, 320)])
        self.assertEqual(self.corners(270), [(610, 320), (610, 20), (10, 20), (10, 320)])

    def test_displayed_size_swaps_when_turned_sideways(self):
        self.assertEqual([export.displayed_size(self.BOX, r) for r in (0, 90, 180, 270)],
                         [(600, 300), (300, 600), (600, 300), (300, 600)])

    def test_the_box_the_viewer_shows(self):
        self.assertEqual(export.page_box([0, 0, 700, 400], [10, 20, 610, 320]), (10, 20, 610, 320))
        self.assertEqual(export.page_box([0, 0, 700, 400], None), (0, 0, 700, 400))
        self.assertEqual(export.page_box([0, 0, 700, 400], [-50, -50, 900, 900]), (0, 0, 700, 400))  # inside the media
        self.assertEqual(export.page_box([700, 400, 0, 0], None), (0, 0, 700, 400))  # corners the wrong way round
        self.assertEqual(export.page_box([0, 0, 700, 400], [800, 0, 900, 100]), (0, 0, 700, 400))  # crop off the page
        self.assertIsNone(export.page_box([0, 0, 0, 0], None))
        self.assertIsNone(export.page_box([0, 0, float('nan'), 5], None))


class PlacementTests(SimpleTestCase):
    RECT = {'t': 'rect', 'p': 1, 'c': 'red', 'w': 3, 'a': [0.1, 0.2], 'b': [0.5, 0.6]}
    LINE = {'t': 'line', 'p': 1, 'c': 'blue', 'w': 2, 'a': [0.25, 0.5], 'b': [0.75, 0.5]}
    NOTE = {'t': 'text', 'p': 1, 'c': 'black', 's': 18, 'at': [0.5, 0.5], 'text': 'Check this opening'}

    def test_a_non_a4_page_at_rotation_0(self):
        # 600 x 300 points inside a larger media box: display (60, 60) to (300, 180)
        content, drawn = export.overlay([self.RECT, self.LINE, self.NOTE], (10, 20, 610, 320), 0)
        self.assertEqual(drawn, 3)
        got = user_space(content)
        close(self, got['rects'][0], (70, 140, 310, 260))
        close(self, got['lines'][0][0] + got['lines'][0][1], (160, 170, 460, 170))
        # The note's baseline: 0.8 of its size (18 thousandths of 600 = 10.8) below the stored point (300, 150)
        close(self, got['text'][0], (310, 320 - (150 + 0.8 * 10.8)))

    def test_a_rotated_page(self):
        # Turned 90 clockwise: displayed 300 wide and 600 high, so (0.1, 0.2) is (30, 120) and (0.5, 0.6) is (150, 360)
        content, _ = export.overlay([self.RECT, self.LINE], (10, 20, 610, 320), 90)
        got = user_space(content)
        close(self, got['rects'][0], (130, 50, 370, 170))
        # Line from displayed (75, 300) to (225, 300)
        close(self, got['lines'][0][0] + got['lines'][0][1], (310, 95, 310, 245))

    def test_the_other_turns(self):
        content, _ = export.overlay([self.RECT], (10, 20, 610, 320), 180)
        close(self, user_space(content)['rects'][0], (310, 80, 550, 200))  # displayed (60,60)-(300,180) turned half way
        content, _ = export.overlay([self.RECT], (10, 20, 610, 320), 270)
        close(self, user_space(content)['rects'][0], (250, 170, 490, 290))

    def test_line_weight_and_text_size_are_thousandths_of_the_displayed_width(self):
        content, _ = export.overlay([self.RECT, self.NOTE], (0, 0, 1000, 500), 0)
        self.assertIn(' 3.000 w', content.decode())          # 3 thousandths of 1000
        self.assertIn('/SLMarkupHB 18.000 Tf', content.decode())
        content, _ = export.overlay([self.RECT], (0, 0, 1000, 500), 90)  # displayed width is now 500
        self.assertIn(' 1.500 w', content.decode())

    def test_every_kind_draws_and_a_cloud_is_made_of_curves(self):
        shapes = [{'t': 'pen', 'p': 1, 'c': 'green', 'w': 3, 'pts': [[0.1, 0.1], [0.2, 0.25], [0.3, 0.2]]},
                  {'t': 'pen', 'p': 1, 'c': 'green', 'w': 3, 'pts': [[0.4, 0.4]]},
                  {'t': 'arrow', 'p': 1, 'c': 'orange', 'w': 4, 'a': [0.1, 0.1], 'b': [0.4, 0.3]},
                  dict(CLOUD, p=1),
                  {'t': 'measure', 'p': 1, 'c': 'purple', 'w': 3, 'a': [0.1, 0.1], 'b': [0.6, 0.1], 'text': '3.45 m'},
                  {'t': 'measure', 'p': 1, 'c': 'purple', 'w': 3, 'a': [0.1, 0.5], 'b': [0.6, 0.5]}]
        content, drawn = export.overlay(shapes, (0, 0, 841.89, 594.96), 0)
        self.assertEqual(drawn, 6)
        text = content.decode()
        self.assertGreater(len(re.findall(r'\bc\b', text)), 8)   # scallops
        self.assertIn(' h f', text)                              # a filled arrow head
        self.assertEqual({t for t in text.split() if re.fullmatch(r'[A-Za-z]+', t)} - ALLOWED_OPERATORS, set())

    def test_shapes_that_are_not_what_we_store_are_left_out_not_half_drawn(self):
        bad = [{'t': 'rect', 'p': 1, 'c': 'red', 'w': 3, 'a': [0.1, 0.2]},          # no second corner
               {'t': 'blob', 'p': 1}, {'t': 'pen', 'p': 1, 'c': 'red', 'w': 3, 'pts': []}, 'text', None,
               {'t': 'rect', 'p': 1, 'c': 'red', 'w': 3, 'a': ['x', None], 'b': [0.5, 0.6]}]
        content, drawn = export.overlay([s for s in bad if isinstance(s, dict)], (0, 0, 600, 300), 0)
        self.assertLessEqual(drawn, 1)  # the last one is clamped to the page, the rest are dropped
        self.assertEqual(export.check(bad), [])
        self.assertEqual(export.check([dict(self.RECT, a=[float('nan'), 0.1])]), [])
        self.assertEqual(export.check([dict(self.RECT, a=[10 ** 400, 0.1])]), [])


class HostileTextTests(SimpleTestCase):
    NASTY = ') Tj ET Q 1 0 0 rg 0 0 9999 9999 re f (\\) <</S /JavaScript /JS (app.alert(1))>> Ω→日本'

    def test_a_label_is_data_never_drawing_instructions(self):
        shapes = [{'t': 'text', 'p': 1, 'c': 'red', 's': 18, 'at': [0.1, 0.1], 'text': self.NASTY},
                  {'t': 'measure', 'p': 1, 'c': 'blue', 'w': 3, 'a': [0.1, 0.5], 'b': [0.9, 0.5], 'text': self.NASTY}]
        out = io.BytesIO()
        pages, drawn = export.export_pdf(io.BytesIO(drawing()), export.check(shapes), out)
        self.assertEqual((pages, drawn), (1, 2))
        reader = PdfReader(io.BytesIO(out.getvalue()))
        page = reader.pages[0]
        ours = page['/Contents'][-1].get_object().get_data()
        operators = {op.decode() for _args, op in ContentStream(page['/Contents'][-1].get_object(), reader).operations}
        self.assertEqual(operators - ALLOWED_OPERATORS, set())
        self.assertNotIn(b'9999', ours)
        self.assertNotIn(b'JavaScript', ours)
        self.assertNotIn(b'JavaScript', out.getvalue())
        self.assertNotIn(b'app.alert', out.getvalue())
        # Both labels come back as the words typed, outside Latin-1 as "?"
        shown = page.extract_text()
        self.assertIn(') Tj ET Q 1 0 0 rg 0 0 9999 9999 re f (', shown)
        self.assertIn('<</S /JavaScript /JS (app.alert(1))>> ????', shown)
        self.assertIn('Drawing text', shown)

    def test_the_label_is_a_hex_string(self):
        content, _ = export.overlay([{'t': 'text', 'p': 1, 'c': 'red', 's': 18, 'at': [0.1, 0.1], 'text': 'A (b)'}],
                                    (0, 0, 600, 300), 0)
        self.assertIn('<4120286229> Tj', content.decode())


class ReadingTheDrawingTests(SimpleTestCase):
    SHAPE = {'t': 'rect', 'p': 1, 'c': 'red', 'w': 3, 'a': [0.1, 0.2], 'b': [0.5, 0.6]}

    def export(self, data, shapes=None):
        out = io.BytesIO()
        result = export.export_pdf(io.BytesIO(data), shapes or [self.SHAPE], out)
        return result, PdfReader(io.BytesIO(out.getvalue()))

    def test_stamped_onto_the_drawings_own_page_and_the_drawing_is_kept(self):
        (pages, drawn), reader = self.export(drawing(media=(0, 0, 1190.55, 841.89)))
        self.assertEqual((pages, drawn, len(reader.pages)), (1, 1, 1))
        page = reader.pages[0]
        self.assertIn('Drawing text', page.extract_text())
        self.assertEqual([float(v) for v in page.mediabox], [0, 0, 1190.55, 841.89])
        stream = page['/Contents'][-1].get_object().get_data()
        close(self, user_space(stream)['rects'][0], (119.055, 841.89 - 0.6 * 841.89, 595.275, 841.89 - 0.2 * 841.89))

    def test_a_rotated_cropped_drawing_end_to_end(self):
        data = drawing(media=(0, 0, 700, 400), crop=(10, 20, 610, 320), rotate=90)
        _, reader = self.export(data)
        stream = reader.pages[0]['/Contents'][-1].get_object().get_data()
        close(self, user_space(stream)['rects'][0], (130, 50, 370, 170))

    def test_a_turn_set_on_the_page_tree_is_honoured_like_the_viewer_does(self):
        data = drawing(media=(0, 0, 600, 300), parent_rotate=270)
        _, reader = self.export(data)
        stream = reader.pages[0]['/Contents'][-1].get_object().get_data()
        # Displayed 300 wide and 600 high: (30, 120)-(150, 360) becomes x = 600 - dy, y = 300 - dx
        close(self, user_space(stream)['rects'][0], (240, 150, 480, 270))

    def test_pages_without_mark_ups_and_other_pages_are_untouched(self):
        (pages, drawn), reader = self.export(real_pdf(['One', 'Two']), shapes=[dict(self.SHAPE, p=2)])
        self.assertEqual((pages, drawn), (2, 1))
        self.assertNotIn(export.FONT_NAME, reader.pages[0]['/Resources']['/Font'])  # page 1 has no mark-ups
        self.assertIn(export.FONT_NAME, reader.pages[1]['/Resources']['/Font'])
        self.assertEqual(len(reader.pages[1]['/Contents']), 4)  # q, the page, Q, the mark-ups
        self.assertIn('One', reader.pages[0].extract_text())
        self.assertIn('Two', reader.pages[1].extract_text())
        # A mark-up on a page the drawing doesn't have draws nothing
        (_, drawn), _ = self.export(real_pdf(['One']), shapes=[dict(self.SHAPE, p=5)])
        self.assertEqual(drawn, 0)

    def test_encrypted_broken_and_empty_drawings_fail_with_a_plain_reason(self):
        for data, words in ((real_pdf(['Secret'], encrypt='pw'), 'password protected'),
                            (b'not a pdf', 'could not be read'), (b'', 'could not be read'),
                            (real_pdf()[:200], 'could not be read')):
            with self.assertRaises(export.ExportFailed, msg=data[:20]) as ctx:
                self.export(data)
            self.assertIn(words, str(ctx.exception))

    def test_too_many_pages_and_too_slow_are_refused(self):
        with mock.patch.object(export, 'MAX_PAGES', 1), self.assertRaises(export.ExportFailed) as ctx:
            self.export(real_pdf(['a', 'b']))
        self.assertIn('more than 1 pages', str(ctx.exception))
        ticks = iter([0] + [10 ** 9] * 10)
        with self.assertRaises(export.ExportFailed) as ctx:
            export.export_pdf(io.BytesIO(real_pdf(['a'])), [self.SHAPE], io.BytesIO(), now=ticks.__next__)
        self.assertIn('took too long', str(ctx.exception))

    def test_scripts_attachments_links_and_open_actions_are_not_carried_over(self):
        _, reader = self.export(hostile_pdf())
        root = reader.trailer['/Root']
        for key in ('/OpenAction', '/Names', '/AA', '/AcroForm'):
            self.assertNotIn(key, root)
        page = reader.pages[0]
        for annot in page.get('/Annots', []):
            self.assertNotIn('/A', annot.get_object())
        self.assertIn('Plain words only', page.extract_text())

    def test_nothing_is_fetched_or_run(self):
        with mock.patch('socket.socket.connect', side_effect=AssertionError('network used')), \
                mock.patch('subprocess.Popen', side_effect=AssertionError('program started')):
            self.export(hostile_pdf())


class ExportBase(MarkupBase):
    def setUp(self):
        super().setUp()
        # A real drawing revision to export, on its own document so the base revisions stay as they are
        self.dwg, revs = self.doc(number='SLN-ABC-ZZ-02-DR-A-1005', title='Rotated plan', visibility='SHARED', revisions=())
        with org_context(self.org_a):
            self.rev = self.dwg.add_revision(DocumentRevision(
                revision='P01', file=upload('p01.pdf', drawing(media=(0, 0, 700, 400), crop=(10, 20, 610, 320),
                                                               rotate=90)),
                original_name='p01.pdf', size=2000, page_count=1, uploaded_by=self.alice))

    def layer_on(self, owner, share=MarkupLayer.PRIVATE, shapes=(CLOUD,), rev=None):
        rev = rev or self.rev
        with org_context(self.org_a):
            return MarkupLayer.objects.create(project=self.pa, document=rev.document, revision=rev, owner=owner,
                                              share=share, data={'v': 1, 'shapes': list(shapes)},
                                              shape_count=len(shapes))

    def ask(self, user, layers=(), rev=None, doc=None):
        self.login(user)
        rev, doc = rev or self.rev, doc or self.dwg
        return self.client.post(self.purl('markup_export', doc.pk, rev.pk), {'layer': [str(x) for x in layers]})

    def file_url(self, rev=None, doc=None):
        return self.purl('markup_export_file', (doc or self.dwg).pk, (rev or self.rev).pk)

    def run_job(self):
        with org_context(self.org_a):
            for job in Job.objects.filter(name='viewer.markup_export'):
                markup_export(job.payload)

    def row(self, user):
        with org_context(self.org_a):
            return MarkupExport.objects.filter(requested_by=user).first()

    def fetch(self, user, rev=None, doc=None):
        self.login(user)
        r = self.client.get(self.file_url(rev, doc))
        return r, (b''.join(r.streaming_content) if r.status_code == 200 else b'')


class WhoMayExportTests(ExportBase):
    def test_the_same_people_as_viewing_the_drawing(self):
        wip = self.purl('markup_export', self.dwg.pk, self.rev.pk)
        for user, want in ((self.alice, 302), (self.admin, 302), (self.staff, 302), (self.cli, 302), (self.con, 302),
                           (self.sub1, 302), (self.sub2, 302), (self.bidder, 403), (self.outsider, 404),
                           (self.bob, 404)):
            self.layer_on(user) if want == 302 else None
            self.login(user)
            self.assertEqual(self.client.post(wip, {}).status_code, want, user.email)
            # and exactly what drawing_pdf says for the same person
            self.assertEqual(self.client.get(self.purl('drawing_pdf', self.dwg.pk, self.rev.pk)).status_code,
                             200 if want == 302 else want, user.email)

    def test_not_signed_in_and_get_requests_are_refused(self):
        self.client.logout()
        self.assertEqual(self.client.post(self.purl('markup_export', self.dwg.pk, self.rev.pk), {}).status_code, 302)
        self.login(self.staff)
        self.assertEqual(self.client.get(self.purl('markup_export', self.dwg.pk, self.rev.pk)).status_code, 405)

    def test_a_withdrawn_revision_is_refused_to_externals_like_the_viewer(self):
        with org_context(self.org_a):
            rev2 = self.dwg.add_revision(DocumentRevision(revision='P02', file=upload('p02.pdf', drawing()),
                                                          original_name='p02.pdf', size=2000, page_count=1,
                                                          uploaded_by=self.alice))
            self.dwg.withdraw(rev2)
        self.layer_on(self.cli, rev=rev2)
        self.layer_on(self.staff, rev=rev2)
        self.assertEqual(self.ask(self.cli, rev=rev2).status_code, 403)
        self.assertEqual(self.client.get(self.purl('markup_export_file', self.dwg.pk, rev2.pk)).status_code, 403)
        self.assertEqual(self.ask(self.staff, rev=rev2).status_code, 302)

    def test_the_other_company_gets_nothing_and_cannot_use_a_layer_from_here(self):
        self.layer_on(self.staff, share=MarkupLayer.EXTERNAL)
        self.assertEqual(self.ask(self.bob).status_code, 404)
        self.assertEqual(self.fetch(self.bob)[0].status_code, 404)
        with org_context(self.org_b):
            self.assertFalse(MarkupExport.objects.exists())
        # A member of both companies, through Bravo's job by id
        from tenancy.models import Membership
        with org_context(self.org_b):
            Membership.objects.create(organization=self.org_b, user=self.staff, role=Membership.ROLE_MEMBER)
        self.login(self.staff)
        self.assertEqual(self.client.post(self.url('markup_export', self.pb.pk, self.dwg.pk, self.rev.pk, org=self.org_b),
                                          {}).status_code, 404)
        # The job only finds exports inside its own company
        self.ask(self.staff)
        row = self.row(self.staff)
        with org_context(self.org_b):
            markup_export({'id': row.pk})
        self.assertEqual(self.row(self.staff).status, MarkupExport.QUEUED)

    def test_only_the_layers_this_person_may_see_are_drawn(self):
        mine = self.layer_on(self.cli, shapes=[dict(CLOUD, c='red')])
        theirs = self.layer_on(self.staff, share=MarkupLayer.PRIVATE, shapes=[dict(CLOUD, c='green')])
        shared = self.layer_on(self.alice, share=MarkupLayer.EXTERNAL, shapes=[dict(CLOUD, c='blue')])
        team = self.layer_on(self.admin, share=MarkupLayer.TEAM, shapes=[dict(CLOUD, c='purple')])
        self.assertEqual(self.ask(self.cli).status_code, 302)
        row = self.row(self.cli)
        self.assertEqual(sorted(row.layer_ids), sorted([mine.pk, shared.pk]))
        self.assertEqual({s['c'] for s in row.shapes}, {'red', 'blue'})
        # Asking for a layer they can't see is a 404, and makes nothing
        for hidden in (theirs, team):
            self.assertEqual(self.ask(self.cli, layers=[hidden.pk]).status_code, 404)
        self.assertEqual(self.ask(self.cli, layers=['x']).status_code, 404)
        self.assertEqual(sorted(self.row(self.cli).layer_ids), sorted([mine.pk, shared.pk]))
        # Staff see the team layer as well
        self.assertEqual(self.ask(self.staff).status_code, 302)
        self.assertIn(team.pk, self.row(self.staff).layer_ids)

    def test_nothing_to_export_makes_nothing(self):
        r = self.ask(self.staff)
        self.assertEqual(r.status_code, 302)
        self.assertIsNone(self.row(self.staff))
        self.assertContains(self.client.get(r['Location']), 'no mark-ups to export')


class MakingAndDownloadingTests(ExportBase):
    def make(self, user=None, shapes=(CLOUD,)):
        user = user or self.staff
        self.layer_on(user, shapes=shapes)
        self.assertEqual(self.ask(user).status_code, 302)
        self.run_job()
        return self.row(user)

    def test_the_job_makes_a_vector_pdf_and_tells_the_person(self):
        row = self.make()
        self.assertEqual((row.status, row.pages, row.shapes_drawn), (MarkupExport.READY, 1, 1))
        with org_context(self.org_a):
            self.assertTrue(Notification.objects.filter(user=self.staff, title__contains='marked-up PDF').exists())
            markup_export({'id': row.pk})  # a second run changes nothing
        r, data = self.fetch(self.staff)
        self.assertEqual((r.status_code, r['Content-Type']), (200, 'application/pdf'))
        self.assertIn('markup.pdf', r['Content-Disposition'])
        page = PdfReader(io.BytesIO(data)).pages[0]
        self.assertIn('Drawing text', page.extract_text())
        stream = page['/Contents'][-1].get_object().get_data()
        self.assertEqual(len(user_space(stream)['rects']), 0)  # a cloud is curves, not a box
        self.assertIn(b' c\n', stream)

    def test_only_the_person_who_asked_can_download_it(self):
        self.make(self.staff)
        for user in (self.alice, self.admin, self.cli, self.sub1):
            self.assertEqual(self.fetch(user)[0].status_code, 404, user.email)
        self.assertEqual(self.fetch(self.staff)[0].status_code, 200)

    def test_the_download_is_refused_once_they_may_no_longer_open_the_drawing(self):
        self.layer_on(self.cli, shapes=[CLOUD])
        self.ask(self.cli)
        self.run_job()
        self.assertEqual(self.fetch(self.cli)[0].status_code, 200)
        with org_context(self.org_a):
            self.dwg.withdraw(self.rev)
        self.assertEqual(self.fetch(self.cli)[0].status_code, 403)

    def test_not_ready_and_failed_go_back_to_the_viewer_with_the_reason(self):
        self.layer_on(self.staff)
        self.ask(self.staff)
        r, _ = self.fetch(self.staff)
        self.assertEqual(r.status_code, 302)
        self.assertContains(self.client.get(r['Location']), 'still being made')
        with org_context(self.org_a):
            MarkupExport.objects.update(status=MarkupExport.FAILED, error='This drawing is password protected.')
        r, _ = self.fetch(self.staff)
        self.assertContains(self.client.get(r['Location']), 'password protected')

    def test_asking_again_replaces_the_last_one(self):
        self.make()
        first = self.row(self.staff)
        old_name = first.file.name
        self.assertEqual(self.ask(self.staff).status_code, 302)
        with org_context(self.org_a):
            self.assertEqual(MarkupExport.objects.count(), 1)
            self.assertNotEqual(MarkupExport.objects.get().pk, first.pk)
        self.assertFalse(first.file.storage.exists(old_name))

    def test_a_drawing_that_cannot_be_read_says_why_and_stays_failed(self):
        with org_context(self.org_a):
            bad = self.dwg.add_revision(DocumentRevision(revision='P02', file=upload('p02.pdf', b'%PDF-1.4 broken'),
                                                         original_name='p02.pdf', size=15, page_count=1,
                                                         uploaded_by=self.alice))
        self.layer_on(self.staff, rev=bad)
        self.ask(self.staff, rev=bad)
        self.run_job()
        row = self.row(self.staff)
        self.assertEqual(row.status, MarkupExport.FAILED)
        self.assertIn('could not be read', row.error)

    def test_the_virus_check_gates_the_job(self):
        self.layer_on(self.staff)
        self.ask(self.staff)
        with mock.patch('viewer.tasks.scan_status', return_value=None):
            self.run_job()
            with org_context(self.org_a):
                self.assertEqual(Job.objects.filter(name='viewer.markup_export').count(), 2)  # asks again later
        self.assertEqual(self.row(self.staff).status, MarkupExport.QUEUED)
        with mock.patch('viewer.tasks.scan_status', return_value=FileScan.INFECTED), \
                mock.patch('viewer.export.export_pdf') as never:
            self.run_job()
        never.assert_not_called()
        row = self.row(self.staff)
        self.assertEqual(row.status, MarkupExport.FAILED)
        self.assertIn('virus check', row.error)

    def test_a_drawing_over_the_size_limit_is_refused(self):
        self.layer_on(self.staff)
        self.ask(self.staff)
        with mock.patch.object(export, 'MAX_FILE_MB', 0):
            self.run_job()
        self.assertIn('over 0 MB', self.row(self.staff).error)

    def test_tampered_shapes_in_the_database_cannot_break_the_pdf(self):
        self.layer_on(self.staff)
        self.ask(self.staff)
        with org_context(self.org_a):
            MarkupExport.objects.update(shapes=[{'t': 'text', 'p': 1, 'c': 'red', 's': 18, 'at': [0.1, 0.1],
                                                 'text': ') Tj ET'}, {'t': 'rect', 'p': 1, 'a': [9e99, 0], 'b': 0},
                                                'junk'])
        self.run_job()
        row = self.row(self.staff)
        self.assertEqual(row.status, MarkupExport.READY)  # the whole list failed its check: the drawing alone
        self.assertEqual(row.shapes_drawn, 0)

    def test_the_viewer_page_offers_it(self):
        self.login(self.staff)
        self.assertContains(self.client.get(self.purl('drawing_viewer', self.dwg.pk, self.rev.pk)),
                            self.purl('markup_export', self.dwg.pk, self.rev.pk))
