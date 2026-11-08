"""
Set-up for the AI tests: the commercial test people (changes/tests/base.py), a bid, and a fake AI client.

No test reaches the network. FakeClient stands in for anthropic.Anthropic: it records each request and answers
with whatever the test queued. The API key is a dummy set only for the test.
"""
import json
import os
from contextlib import contextmanager
from decimal import Decimal as D
from types import SimpleNamespace
from unittest import mock

from changes.tests.base import CommercialTestBase, make_user
from projects.models import Project, ProjectMember
from tenancy.context import org_context
from tenancy.models import Membership

from ai.models import AISettings


def message(data, stop='end_turn', model='claude-opus-5-5', tokens=(1000, 500, 0, 0)):
    text = data if isinstance(data, str) else json.dumps(data)
    usage = SimpleNamespace(input_tokens=tokens[0], output_tokens=tokens[1], cache_creation_input_tokens=tokens[2],
                            cache_read_input_tokens=tokens[3])
    return SimpleNamespace(content=[SimpleNamespace(type='thinking', thinking=''), SimpleNamespace(type='text', text=text)],
                           usage=usage, stop_reason=stop, model=model, id='msg_test', _request_id='req_test')


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        outer = self

        class _Stream:
            def __init__(self, params):
                self.params = params

            def __enter__(self):
                outer.requests.append(self.params)
                reply = outer.replies.pop(0)
                if isinstance(reply, Exception):
                    raise reply
                self.reply = reply
                return self

            def __exit__(self, *exc):
                return False

            def get_final_message(self):
                return self.reply

        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=lambda **params: _Stream(params)))


@contextmanager
def fake_ai(*replies, key='sk-test-not-a-real-key'):
    client = FakeClient(replies)
    env = {'ANTHROPIC_API_KEY': key} if key else {}
    with mock.patch.dict(os.environ, env, clear=False), mock.patch('ai.service.get_client', return_value=client):
        if not key:
            os.environ.pop('ANTHROPIC_API_KEY', None)
        yield client


def run_worker():
    from jobs.runner import Worker
    Worker('test-worker').work(burst=True)


class Base(CommercialTestBase):
    def setUp(self):
        super().setUp()
        os.environ.pop('ANTHROPIC_API_KEY', None)
        self.stan = make_user('stan@a.test', 'Stan Site')
        Membership.objects.create(organization=self.org_a, user=self.stan, role=Membership.ROLE_MEMBER,
                                  area=Membership.AREA_DELIVERY)
        with org_context(self.org_a):
            self.bid = Project.objects.create(name='Alpha bid', stage=Project.STAGE_TENDERING, owner=self.eve,
                                              value=D('200000'))
            ProjectMember.objects.create(project=self.bid, user=self.carla, role=ProjectMember.ROLE_CLIENT)
            ProjectMember.objects.create(project=self.bid, user=self.bea, role=ProjectMember.ROLE_BIDDER,
                                         subcontractor=self.subco)
            ProjectMember.objects.create(project=self.bid, user=self.sid, role=ProjectMember.ROLE_SUBCONTRACTOR,
                                         subcontractor=self.subco)

    def switch_on(self, org=None, **kw):
        org = org or self.org_a
        with org_context(org):
            AISettings.objects.update_or_create(organization=org, defaults={'enabled': True, **kw})
