"""
Mark-up layers: the shape checks (viewer/markup.py), saving and adding through the viewer's
endpoints, who sees which layer (viewer.access.visible_layers), sharing and deleting, the viewer
page's layer list for each role, revision compare, tenancy and the CSP.
"""
import json
import re

from django.test import SimpleTestCase, TestCase

from documents.tests.base import upload
from projects.models import Document, DocumentRevision
from security.middleware import build_csp
from tenancy.context import TenantContextMissing, org_context
from viewer import markup
from viewer.models import MarkupLayer

from .test_viewer import ViewerBase

PEN = {'t': 'pen', 'p': 1, 'c': 'red', 'w': 3, 'pts': [[0.1, 0.1], [0.2, 0.25]]}
CLOUD = {'t': 'cloud', 'p': 1, 'c': 'blue', 'w': 3, 'a': [0.1, 0.1], 'b': [0.3, 0.4]}
NOTE = {'t': 'text', 'p': 1, 'c': 'black', 's': 18, 'at': [0.5, 0.5], 'text': 'Check this opening'}


def doc_json(*shapes):
    return json.dumps({'v': 1, 'shapes': list(shapes)})


class SchemaTests(SimpleTestCase):
    def test_every_kind_is_accepted_and_rounded(self):
        shapes = [PEN, CLOUD, NOTE,
                  {'t': 'line', 'p': 1, 'c': 'green', 'w': 1, 'a': [0, 0], 'b': [1, 1]},
                  {'t': 'arrow', 'p': 2, 'c': 'orange', 'w': 12, 'a': [0.123456789, 0.5], 'b': [0.9, 0.9]},
                  {'t': 'rect', 'p': 1, 'c': 'purple', 'w': 2, 'a': [0.2, 0.2], 'b': [0.1, 0.1]},
                  {'t': 'measure', 'p': 1, 'c': 'red', 'w': 3, 'a': [0.1, 0.1], 'b': [0.2, 0.1], 'text': '3.45 m'},
                  {'t': 'measure', 'p': 1, 'c': 'red', 'w': 3, 'a': [0.1, 0.1], 'b': [0.2, 0.1]}]
        data = markup.parse(doc_json(*shapes), page_count=2)
        self.assertEqual(len(data['shapes']), 8)
        self.assertEqual(data['shapes'][4]['a'], [0.12346, 0.5])
        self.assertEqual(data['shapes'][7]['text'], '')
        self.assertEqual(markup.summary(data['shapes']),
                         '1 pen stroke, 1 note, 1 line, 1 arrow, 1 box, 1 cloud, 2 measurements')

    def test_refuses_anything_else(self):
        bad = [
            dict(PEN, t='circle'), dict(PEN, c='#ff0000'), dict(PEN, c='red;background:url(x)'),
            dict(PEN, w=0), dict(PEN, w=13), dict(PEN, w=2.5), dict(PEN, w=True), dict(PEN, p=0), dict(PEN, p=3),
            dict(PEN, p='1'), dict(PEN, pts=[]), dict(PEN, pts=[[0.1]]), dict(PEN, pts=[[0.1, 1.2]]),
            dict(PEN, pts=[[-0.01, 0.5]]), dict(PEN, pts=[[True, 0.5]]), dict(PEN, pts=[['0.1', 0.5]]),
            dict(PEN, pts=[[0.1, 0.1]] * 2001), dict(PEN, onclick='alert(1)'), dict(CLOUD, b=None),
            dict(NOTE, text=''), dict(NOTE, text='   '), dict(NOTE, text='x' * 301), dict(NOTE, text=['a']),
            dict(NOTE, s=5), dict(NOTE, s=121), dict(NOTE, w=3), 'pen', None, [],
        ]
        for shape in bad:
            with self.assertRaises(markup.InvalidMarkup, msg=repr(shape)[:80]):
                markup.parse(doc_json(shape), page_count=2)

    def test_refuses_bad_documents(self):
        for text in ['', 'null', '[]', '{}', '{"v": 2, "shapes": []}', '{"v": 1}', '{"v": 1, "shapes": {}}',
                     '{"v": 1, "shapes": [], "html": "<b>"}', '{"v": 1, "shapes": [{"t": "pen", "p": 1, "c": "red", '
                     '"w": 3, "pts": [[NaN, 0.5]]}]}', '{"v": 1, "shapes": [{"t": "pen", "p": 1, "c": "red", "w": 3, '
                     '"pts": [[Infinity, 0.5]]}]}', '{"v": 1, "shapes": [', '[' * 50000 + ']' * 50000, None, 42]:
            with self.assertRaises(markup.InvalidMarkup, msg=repr(text)[:60]):
                markup.parse(text)

    def test_size_and_count_caps(self):
        with self.assertRaisesRegex(markup.InvalidMarkup, 'too big'):
            markup.parse(doc_json(dict(NOTE, text='x' * 200)) + ' ' * markup.MAX_BYTES)
        with self.assertRaisesRegex(markup.InvalidMarkup, 'at most 1000 shapes'):
            markup.parse(doc_json(*[CLOUD] * 1001))
        many = dict(PEN, pts=[[0.1, 0.1]] * 2000)
        with self.assertRaisesRegex(markup.InvalidMarkup, 'too many pen strokes'):
            markup.clean_shapes([many] * 11)
        self.assertEqual(len(markup.parse(doc_json(*[CLOUD] * 1000))['shapes']), 1000)

    def test_text_is_kept_as_plain_words(self):
        data = markup.parse(doc_json(dict(NOTE, text='  <script>alert(1)</script>\n\tnext\u202eline\x00 ')))
        self.assertEqual(data['shapes'][0]['text'], '<script>alert(1)</script> nextline')  # stored as text, drawn by fillText

    def test_add_shapes(self):
        self.assertEqual(markup.parse_shapes(json.dumps([CLOUD]))[0]['t'], 'cloud')
        for text in ['[]', '{}', '"x"']:
            with self.assertRaises(markup.InvalidMarkup):
                markup.parse_shapes(text)


class MarkupBase(ViewerBase):
    def layer(self, owner, rev=None, share=MarkupLayer.PRIVATE, shapes=(CLOUD,)):
        rev = rev or self.p02
        with org_context(self.org_a):
            return MarkupLayer.objects.create(project=self.pa, document=rev.document, revision=rev, owner=owner,
                                              share=share, data={'v': 1, 'shapes': list(shapes)},
                                              shape_count=len(shapes))

    def save(self, data, rev=None, version='', doc=None):
        rev = rev or self.p02
        return self.client.post(self.purl('markup_save', (doc or self.ga).pk, rev.pk),
                                {'data': data, 'version': version})

    def data_url(self, layer):
        return self.purl('markup_data', self.ga.pk, layer.pk)

    def layer_ids(self, response):
        return {la['id'] for la in self.config(response)['layers']}


class SaveTests(MarkupBase):
    def test_save_creates_then_updates_with_a_version(self):
        self.login(self.staff)
        r = self.save(doc_json(CLOUD))
        self.assertEqual(r.status_code, 200, r.content)
        j = r.json()
        self.assertEqual((j['version'], j['count'], j['summary']), (1, 1, '1 cloud'))
        r = self.save(doc_json(CLOUD, NOTE), version='1')
        self.assertEqual(r.json()['version'], 2)
        with org_context(self.org_a):
            layer = MarkupLayer.objects.get()
        self.assertEqual((layer.owner, layer.revision, layer.share, layer.shape_count), (self.staff, self.p02,
                                                                                      MarkupLayer.PRIVATE, 2))
        self.assertEqual(layer.shapes[1]['text'], 'Check this opening')

    def test_a_stale_save_is_refused(self):
        self.login(self.staff)
        self.save(doc_json(CLOUD))
        self.save(doc_json(CLOUD, NOTE), version='1')
        r = self.save(doc_json(), version='1')  # another window still on version 1
        self.assertEqual(r.status_code, 409)
        self.assertIn('another window', r.json()['error'])
        self.assertEqual(self.save(doc_json(), version='5').status_code, 409)
        with org_context(self.org_a):
            self.assertEqual(MarkupLayer.objects.get().shape_count, 2)
            MarkupLayer.objects.all().delete()
        self.assertEqual(self.save(doc_json(CLOUD), version='2').status_code, 409)  # deleted elsewhere

    def test_bad_markup_is_refused_and_nothing_stored(self):
        self.login(self.staff)
        r = self.save(doc_json(dict(PEN, c='javascript:alert(1)')))
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()['error'], 'Choose one of the mark-up colours.')
        self.assertEqual(self.save('not json').status_code, 400)
        self.assertEqual(self.save(doc_json(dict(PEN, p=3))).status_code, 400)  # P02 has 2 pages
        with org_context(self.org_a):
            self.assertFalse(MarkupLayer.objects.exists())

    def test_who_may_draw_on_which_revision(self):
        with org_context(self.org_a):
            p03 = self.ga.add_revision(DocumentRevision(revision='P03', suitability='S0', file=upload('P03.pdf'),
                                                        original_name='P03.pdf', size=10, uploaded_by=self.alice))
        matrix = {self.alice: 200, self.admin: 200, self.staff: 200, self.cli: 200, self.con: 200, self.sub1: 200,
                  self.sub2: 200, self.bidder: 403, self.outsider: 404, self.bob: 404}
        for user, want in matrix.items():
            self.login(user)
            self.assertEqual(self.save(doc_json(CLOUD)).status_code, want, user.email)
        # Work in progress: staff only
        self.login(self.staff)
        self.assertEqual(self.save(doc_json(CLOUD), rev=p03).status_code, 200)
        self.login(self.cli)
        self.assertEqual(self.save(doc_json(CLOUD), rev=p03).status_code, 404)
        # Withdrawn: nobody draws on it
        with org_context(self.org_a):
            self.ga.withdraw(self.p01)
        self.login(self.staff)
        self.assertEqual(self.save(doc_json(CLOUD), rev=self.p01).status_code, 403)
        self.login(self.cli)
        self.assertEqual(self.save(doc_json(CLOUD), rev=self.p01).status_code, 404)

    def test_get_is_not_allowed_and_archived_is_read_only(self):
        self.login(self.staff)
        self.assertEqual(self.client.get(self.purl('markup_save', self.ga.pk, self.p02.pk)).status_code, 405)
        with org_context(self.org_a):
            self.pa.archive(self.alice)
        self.login(self.alice)
        self.assertEqual(self.save(doc_json(CLOUD)).status_code, 403)
        r = self.client.get(self.viewer())
        self.assertEqual(r.status_code, 200)
        self.assertFalse(self.config(r)['can_draw'])
        self.assertNotContains(r, 'data-act="markup"')

    def test_add_appends_to_your_own_layer(self):
        self.login(self.sub1)
        url = self.purl('markup_add', self.ga.pk, self.p02.pk)
        r = self.client.post(url, {'shapes': json.dumps([CLOUD])})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()['version'], 1)
        r = self.client.post(url, {'shapes': json.dumps([CLOUD, CLOUD])})
        self.assertEqual((r.json()['count'], r.json()['version']), (3, 2))
        self.assertEqual(self.client.post(url, {'shapes': json.dumps([dict(CLOUD, a=[2, 2])])}).status_code, 400)
        self.assertEqual(self.client.post(url, {'shapes': '[]'}).status_code, 400)
        self.layer(self.staff)  # someone else's layer on the same revision is left alone
        with org_context(self.org_a):
            self.assertEqual(MarkupLayer.objects.get(owner=self.sub1).shape_count, 3)
            self.assertEqual(MarkupLayer.objects.get(owner=self.staff).shape_count, 1)

    def test_add_respects_the_cap(self):
        self.layer(self.staff, shapes=[CLOUD] * 1000)
        self.login(self.staff)
        r = self.client.post(self.purl('markup_add', self.ga.pk, self.p02.pk), {'shapes': json.dumps([CLOUD])})
        self.assertEqual(r.status_code, 400)


class VisibilityTests(MarkupBase):
    def setUp(self):
        super().setUp()
        self.private = self.layer(self.staff)
        self.team = self.layer(self.alice, share=MarkupLayer.TEAM)
        self.shared = self.layer(self.admin, share=MarkupLayer.EXTERNAL)
        self.old = self.layer(self.admin, rev=self.p01, share=MarkupLayer.EXTERNAL)
        self.client_team = self.layer(self.cli, share=MarkupLayer.TEAM)
        self.sub_private = self.layer(self.sub1)
        # Work in progress on the same drawing, marked up and "shared" by staff: still internal
        with org_context(self.org_a):
            self.p03 = self.ga.add_revision(DocumentRevision(revision='P03', suitability='S0', file=upload('P03.pdf'),
                                                             original_name='P03.pdf', size=10, uploaded_by=self.alice))
        self.wip = self.layer(self.admin, rev=self.p03, share=MarkupLayer.EXTERNAL)

    def test_each_role_sees_only_what_it_should(self):
        everyone_staff = {self.team.pk, self.shared.pk, self.old.pk, self.client_team.pk, self.wip.pk}
        want = {
            self.staff: everyone_staff | {self.private.pk},
            self.alice: everyone_staff,
            self.admin: everyone_staff,
            self.cli: {self.shared.pk, self.old.pk, self.client_team.pk},
            self.con: {self.shared.pk, self.old.pk},
            self.sub1: {self.shared.pk, self.old.pk, self.sub_private.pk},
            self.sub1b: {self.shared.pk, self.old.pk},  # same company as sub1, but not sub1's private layer
            self.sub2: {self.shared.pk, self.old.pk},
        }
        every = [self.private, self.team, self.shared, self.old, self.client_team, self.sub_private, self.wip]
        for user, ids in want.items():
            self.login(user)
            self.assertEqual(self.layer_ids(self.client.get(self.viewer())), ids, user.email)
            for layer in every:
                r = self.client.get(self.data_url(layer))
                self.assertEqual(r.status_code, 200 if layer.pk in ids else 404, f'{user.email} {layer}')
                if r.status_code == 200:
                    self.assertEqual(r['Content-Type'], 'application/json')
                    self.assertEqual(r.json()['shapes'], [CLOUD])
                    self.assertIn('no-store', r['Cache-Control'])

    def test_withdrawing_a_revision_hides_its_layers_from_externals(self):
        with org_context(self.org_a):
            self.ga.withdraw(self.p01)
        self.login(self.cli)
        self.assertEqual(self.client.get(self.data_url(self.old)).status_code, 404)
        self.login(self.staff)
        self.assertEqual(self.client.get(self.data_url(self.old)).status_code, 200)

    def test_document_made_internal_hides_everything_from_externals(self):
        with org_context(self.org_a):
            Document.objects.filter(pk=self.ga.pk).update(visibility=Document.VIS_INTERNAL)
        self.login(self.cli)
        self.assertEqual(self.client.get(self.data_url(self.shared)).status_code, 404)
        self.assertEqual(self.client.get(self.data_url(self.client_team)).status_code, 404)

    def test_the_list_says_where_each_layer_was_drawn(self):
        self.login(self.staff)
        r = self.client.get(self.viewer())
        layers = {la['id']: la for la in self.config(r)['layers']}
        self.assertTrue(layers[self.shared.pk]['here'])
        self.assertFalse(layers[self.old.pk]['here'])
        self.assertEqual(layers[self.old.pk]['revision'], 'P01')
        self.assertEqual(layers[self.client_team.pk]['owner'], 'Carla Client')
        self.assertEqual(self.config(r)['mine']['id'], self.private.pk)
        self.assertContains(r, '(drawn on another revision)')
        self.assertContains(r, f'data-layer="{self.old.pk}"')
        self.assertNotContains(r, f'data-layer="{self.old.pk}" checked')  # other revisions start hidden
        self.assertContains(r, f'data-layer="{self.shared.pk}" checked')

    def test_markup_text_never_reaches_the_page_as_html(self):
        evil = dict(NOTE, text='</script><img src=x onerror=alert(1)>')
        layer = self.layer(self.alice, rev=self.p01, share=MarkupLayer.EXTERNAL, shapes=[evil])
        self.login(self.cli)
        r = self.client.get(self.viewer(self.p01))
        self.assertNotIn('<img src=x', r.content.decode())
        r = self.client.get(self.data_url(layer))
        self.assertEqual(r['Content-Type'], 'application/json')  # read by script, written with fillText


class ShareAndDeleteTests(MarkupBase):
    def share(self, layer, share, nxt=''):
        return self.client.post(self.purl('markup_share', self.ga.pk, layer.pk), {'share': share, 'next': nxt})

    def test_owner_shares_and_unshares(self):
        layer = self.layer(self.staff)
        self.login(self.staff)
        r = self.share(layer, MarkupLayer.EXTERNAL, nxt=self.viewer() + '?pin=1')
        self.assertRedirects(r, self.viewer() + '?pin=1', fetch_redirect_response=False)
        self.assertEqual(self.fresh(layer).share, MarkupLayer.EXTERNAL)
        self.login(self.cli)
        self.assertEqual(self.client.get(self.data_url(layer)).status_code, 200)
        self.login(self.staff)
        self.share(layer, MarkupLayer.PRIVATE)
        self.login(self.cli)
        self.assertEqual(self.client.get(self.data_url(layer)).status_code, 404)

    def test_externals_share_with_the_site_team_only(self):
        layer = self.layer(self.sub1)
        self.login(self.sub1)
        r = self.client.get(self.viewer())
        self.assertNotContains(r, 'value="EXTERNAL"')
        self.share(layer, MarkupLayer.EXTERNAL)
        self.assertEqual(self.fresh(layer).share, MarkupLayer.PRIVATE)
        self.share(layer, MarkupLayer.TEAM)
        self.assertEqual(self.fresh(layer).share, MarkupLayer.TEAM)
        self.login(self.sub2)
        self.assertEqual(self.client.get(self.data_url(layer)).status_code, 404)
        self.login(self.staff)
        self.assertEqual(self.client.get(self.data_url(layer)).status_code, 200)

    def test_only_the_owner_changes_or_deletes_a_layer(self):
        layer = self.layer(self.staff, share=MarkupLayer.EXTERNAL)
        for user in (self.alice, self.admin, self.cli):
            self.login(user)
            self.assertEqual(self.share(layer, MarkupLayer.PRIVATE).status_code, 403, user.email)
            self.assertEqual(self.client.post(self.purl('markup_delete', self.ga.pk, layer.pk)).status_code, 403)
        self.login(self.staff)
        r = self.client.post(self.purl('markup_delete', self.ga.pk, layer.pk))
        self.assertRedirects(r, self.viewer(), fetch_redirect_response=False)
        with org_context(self.org_a):
            self.assertFalse(MarkupLayer.objects.exists())

    def test_next_must_stay_on_the_project(self):
        layer = self.layer(self.staff)
        self.login(self.staff)
        for nxt in ('https://evil.example/', '//evil.example/', '/other-org/projects/1/', 'javascript:alert(1)'):
            r = self.share(layer, MarkupLayer.TEAM, nxt=nxt)
            self.assertEqual(r.url, self.viewer(), nxt)


class CompareTests(MarkupBase):
    def compare(self, **params):
        return self.client.get(self.purl('drawing_compare', self.ga.pk), params)

    def compare_config(self, r):
        m = re.search(r'<script id="dvc-config" type="application/json">(.*?)</script>', r.content.decode(), re.S)
        return json.loads(m.group(1))

    def test_defaults_to_the_current_revision_against_the_one_before(self):
        self.login(self.cli)
        r = self.compare()
        self.assertEqual(r.status_code, 200)
        cfg = self.compare_config(r)
        self.assertEqual((cfg['a']['label'], cfg['b']['label']), ('P01', 'P02'))
        self.assertEqual(cfg['a']['pdf'], self.pdf(self.p01))
        self.assertEqual(cfg['add_url'], self.purl('markup_add', self.ga.pk, self.p02.pk))
        r = self.compare(a=self.p02.pk, b=self.p01.pk)
        cfg = self.compare_config(r)
        self.assertEqual((cfg['a']['label'], cfg['b']['label']), ('P02', 'P01'))

    def test_externals_cannot_pick_a_revision_they_cannot_open(self):
        with org_context(self.org_a):
            p03 = self.ga.add_revision(DocumentRevision(revision='P03', suitability='S0', file=upload('P03.pdf'),
                                                        original_name='P03.pdf', size=10, uploaded_by=self.alice))
        self.login(self.cli)
        cfg = self.compare_config(self.compare(b=p03.pk))
        self.assertEqual(cfg['b']['label'], 'P02')
        self.assertNotContains(self.compare(), 'P03')
        self.login(self.staff)
        cfg = self.compare_config(self.compare(b=p03.pk))
        self.assertEqual((cfg['a']['label'], cfg['b']['label']), ('P02', 'P03'))

    def test_one_revision_is_an_empty_state(self):
        d, revs = self.doc(number='SLN-ABC-ZZ-02-DR-A-1005', visibility=Document.VIS_SHARED)
        self.login(self.staff)
        r = self.client.get(self.purl('drawing_compare', d.pk))
        self.assertContains(r, 'Nothing to compare yet')
        self.assertContains(r, f'href="{self.purl("document_detail", d.pk)}"')
        self.assertNotContains(r, 'compare.js')
        # And the viewer only offers Compare when there are two
        self.assertNotContains(self.client.get(self.viewer(revs['P01'], d)), self.purl('drawing_compare', d.pk))
        self.assertContains(self.client.get(self.viewer()), self.purl('drawing_compare', self.ga.pk))

    def test_access(self):
        matrix = {self.alice: 200, self.staff: 200, self.cli: 200, self.con: 200, self.sub1: 200, self.bidder: 403,
                  self.outsider: 404, self.bob: 404}
        for user, want in matrix.items():
            self.login(user)
            self.assertEqual(self.compare().status_code, want, user.email)

    def test_archived_job_compares_but_does_not_cloud(self):
        with org_context(self.org_a):
            self.pa.archive(self.alice)
        self.login(self.alice)
        r = self.compare()
        self.assertEqual(self.compare_config(r)['add_url'], '')
        self.assertNotContains(r, 'data-act="cloud"')

    def test_your_clouds_on_the_newer_revision_are_shown(self):
        layer = self.layer(self.sub1)
        self.login(self.sub1)
        self.assertEqual(self.compare_config(self.compare())['mine_url'], self.data_url(layer))
        self.login(self.sub2)
        self.assertEqual(self.compare_config(self.compare())['mine_url'], '')

    def test_keeps_the_strict_csp(self):
        self.login(self.staff)
        r = self.compare()
        self.assertEqual(r['Content-Security-Policy'], build_csp())
        for tag in re.findall(r'<script\b[^>]*>', r.content.decode()):
            self.assertTrue('src="/static/' in tag or 'application/json' in tag, tag)
        self.assertContains(r, '/static/viewer/compare.js')


class IsolationTests(MarkupBase):
    def test_another_company_gets_404(self):
        layer = self.layer(self.alice, share=MarkupLayer.EXTERNAL)
        self.login(self.bob)
        get_urls = [self.data_url(layer), self.purl('drawing_compare', self.ga.pk),
                    self.url('markup_data', self.pa.pk, self.ga.pk, layer.pk, org=self.org_b),
                    self.url('drawing_compare', self.pa.pk, self.ga.pk, org=self.org_b)]
        for url in get_urls:
            self.assertEqual(self.client.get(url).status_code, 404, url)
        post_urls = [self.purl('markup_save', self.ga.pk, self.p02.pk), self.purl('markup_add', self.ga.pk, self.p02.pk),
                     self.purl('markup_share', self.ga.pk, layer.pk), self.purl('markup_delete', self.ga.pk, layer.pk),
                     self.url('markup_save', self.pa.pk, self.ga.pk, self.p02.pk, org=self.org_b),
                     self.url('markup_delete', self.pa.pk, self.ga.pk, layer.pk, org=self.org_b)]
        for url in post_urls:
            self.assertEqual(self.client.post(url, {'data': doc_json(), 'shapes': json.dumps([CLOUD]),
                                                    'share': 'PRIVATE'}).status_code, 404, url)
        with org_context(self.org_a):
            self.assertEqual(MarkupLayer.objects.get().share, MarkupLayer.EXTERNAL)
        with org_context(self.org_b):
            self.assertFalse(MarkupLayer.objects.exists())

    def test_a_bravo_layer_cannot_be_read_through_alpha(self):
        bdoc, brevs = self.doc(number='SLN-ABC-ZZ-02-DR-A-1001', visibility=Document.VIS_SHARED, project=self.pb,
                               org=self.org_b)
        with org_context(self.org_b):
            blayer = MarkupLayer.objects.create(project=self.pb, document=bdoc, revision=brevs['P01'], owner=self.bob,
                                                share=MarkupLayer.EXTERNAL, data={'v': 1, 'shapes': []})
        self.login(self.alice)
        self.assertEqual(self.client.get(self.purl('markup_data', bdoc.pk, blayer.pk)).status_code, 404)
        self.assertEqual(self.client.get(self.purl('markup_data', self.ga.pk, blayer.pk)).status_code, 404)
        self.assertEqual(self.save(doc_json(CLOUD), rev=brevs['P01'], doc=bdoc).status_code, 404)


class FailClosedTests(TestCase):
    def test_no_org_context_fails_closed(self):
        with self.assertRaises(TenantContextMissing):
            list(MarkupLayer.objects.all())
