"""
The project's test runner (settings.TEST_RUNNER).

Most tests across the apps sign in owners and admins with force_login() and then use the
workspace; with two-factor enforcement on, every one of them would be sent to the set-up page.
So the suite runs with TWO_FACTOR_ENFORCE off, and the tests that are about enforcement
(security/tests/test_two_factor.py) switch it back on with override_settings. Step-up still
applies everywhere: force_login() counts as a fresh sign-in, as a real one does.
"""
from django.test.runner import DiscoverRunner
from django.test.utils import override_settings


class TestRunner(DiscoverRunner):
    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        self._two_factor_off = override_settings(TWO_FACTOR_ENFORCE=False)
        self._two_factor_off.enable()

    def teardown_test_environment(self, **kwargs):
        self._two_factor_off.disable()
        super().teardown_test_environment(**kwargs)
