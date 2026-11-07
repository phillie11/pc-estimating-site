"""The test runner's parallel workers report failures as text (security/testing.py), so a failure never stops the run."""
import pickle
import sys
from unittest import mock

from django.test import SimpleTestCase

from security import testing


def exc_info(raise_it):
    try:
        raise_it()
    except BaseException:
        return sys.exc_info()


class FailuresAsTextTests(SimpleTestCase):
    def _fail(self):
        self.assertEqual('expected', 'got')

    def test_a_failure_pickles_as_a_failure_with_its_traceback(self):
        with mock.patch.object(testing.django_runner, 'tblib', None):
            kind, exc, tb = pickle.loads(pickle.dumps(testing.as_text(exc_info(self._fail))))
        self.assertIs(kind, testing.RemoteFailure)
        self.assertTrue(issubclass(kind, AssertionError))  # unittest files it under failures, not errors
        self.assertIsNone(tb)
        self.assertIn("AssertionError: 'expected' != 'got'", str(exc))
        self.assertIn('in _fail', str(exc))  # where it failed, from the worker's traceback

    def test_an_error_pickles_as_an_error_even_when_the_exception_cannot(self):
        class Unpicklable(Exception):
            def __reduce__(self):
                raise TypeError('cannot pickle this')

        def boom():
            raise Unpicklable('database went away')
        with mock.patch.object(testing.django_runner, 'tblib', None):
            kind, exc, tb = pickle.loads(pickle.dumps(testing.as_text(exc_info(boom))))
        self.assertIs(kind, testing.RemoteError)
        self.assertFalse(issubclass(kind, AssertionError))
        self.assertIn('Unpicklable: database went away', str(exc))

    def test_left_alone_where_tblib_can_pickle_the_real_traceback(self):
        err = exc_info(self._fail)
        with mock.patch.object(testing.django_runner, 'tblib', object()):
            self.assertIs(testing.as_text(err), err)
        self.assertIsNone(testing.as_text(None))

    def test_the_runner_uses_it_for_parallel_runs(self):
        suite = testing.TestRunner.parallel_test_suite
        self.assertIs(suite.runner_class.resultclass, testing.TextResult)
