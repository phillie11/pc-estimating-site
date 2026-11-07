"""
The project's test runner (settings.TEST_RUNNER).

Most tests across the apps sign in owners and admins with force_login() and then use the
workspace; with two-factor enforcement on, every one of them would be sent to the set-up page.
So the suite runs with TWO_FACTOR_ENFORCE off, and the tests that are about enforcement
(security/tests/test_two_factor.py) switch it back on with override_settings. Step-up still
applies everywhere: force_login() counts as a fresh sign-in, as a real one does.

Parallel runs (`manage.py test --parallel N`, as CI runs them): each worker process sends its
results to the main one by pickling them, and a traceback can't be pickled without the tblib
package. Django then stops the whole run at the first failure with "cannot pickle 'traceback'
object", without saying which test failed or why. Rather than add a dependency, the workers here
send each failure's formatted traceback as text instead, so a failing test is reported in full
and the rest of the suite carries on, as in a serial run.

Uploaded files: the whole run saves them in a temporary MEDIA_ROOT made for it and deleted at the
end, and in a parallel run each worker has its own folder in it. The workers' databases are
separate copies, so two of them can both make organisation 1's project 1 and save its files to the
same path. (Test modules used to make their own folder at import and delete it when their tests
finished; in a parallel run that folder was shared, and one worker deleted it while another was
still reading from it. That, with the pickling, was why --parallel was "broken".)
"""
import os
import shutil
import tempfile
import traceback

from django.test import runner as django_runner
from django.test.runner import DiscoverRunner, ParallelTestSuite, RemoteTestResult, RemoteTestRunner
from django.test.utils import override_settings


def hold_rate_limit_clock(test):
    """
    For a test that counts requests against a rate limit: hold the limiter's clock one second into an hour,
    which starts a window of every length the limits use (security/ratelimit.py: windows are time //
    window), so the requests the test expects to share a window always do. On a busy CI runner a window
    could otherwise roll over between them. Only the limiter's clock is held.
    """
    import time
    from unittest import mock
    start = (int(time.time()) // 3600) * 3600 + 1
    patcher = mock.patch('security.ratelimit.time', mock.Mock(time=mock.Mock(return_value=start)))
    patcher.start()
    test.addCleanup(patcher.stop)


class RemoteFailure(AssertionError):
    """A failed assertion in a worker process, carrying its formatted traceback (counts as a failure)."""


class RemoteError(Exception):
    """An exception in a worker process, carrying its formatted traceback (counts as an error)."""


def as_text(err):
    """sys.exc_info() as a picklable tuple: the same kind of exception, with the traceback in its message."""
    if err is None or django_runner.tblib is not None:
        return err  # tblib, where installed, pickles the real traceback
    exc_type, exc, tb = err
    text = ''.join(traceback.format_exception(exc_type, exc, tb)).rstrip()
    kind = RemoteFailure if isinstance(exc_type, type) and issubclass(exc_type, AssertionError) else RemoteError
    return kind, kind(f'\n{text}'), None


class TextResult(RemoteTestResult):
    def addError(self, test, err):
        super().addError(test, as_text(err))

    def addFailure(self, test, err):
        super().addFailure(test, as_text(err))

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, as_text(err))

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, as_text(err))


class TextResultRunner(RemoteTestRunner):
    resultclass = TextResult


def init_worker(counter, *args, **kwargs):
    """Django's worker set-up (its own copy of each test database), then its own MEDIA_ROOT."""
    from django.conf import settings
    django_runner._init_worker(counter, *args, **kwargs)
    media = os.path.join(str(settings.MEDIA_ROOT), f'worker-{django_runner._worker_id}')
    os.makedirs(media, exist_ok=True)
    override_settings(MEDIA_ROOT=media).enable()  # for the life of the worker; storages pick it up


class ParallelSuite(ParallelTestSuite):
    init_worker = init_worker
    runner_class = TextResultRunner


class TestRunner(DiscoverRunner):
    parallel_test_suite = ParallelSuite

    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        self._media = tempfile.mkdtemp(prefix='siteline-test-media-')
        self._overrides = override_settings(TWO_FACTOR_ENFORCE=False, MEDIA_ROOT=self._media)
        self._overrides.enable()

    def teardown_test_environment(self, **kwargs):
        self._overrides.disable()
        shutil.rmtree(self._media, ignore_errors=True)
        super().teardown_test_environment(**kwargs)
