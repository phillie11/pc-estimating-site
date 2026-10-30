from django.conf import settings
from django.core import checks


@checks.register()
def email_timeout_under_lock_timeout(app_configs, **kwargs):
    """
    EMAIL_TIMEOUT bounds each SMTP operation (connect, TLS, login, send), so one email can take a
    few times it. If that can reach JOBS_LOCK_TIMEOUT, a hung send is recovered as stale and a
    second worker sends the email again.
    """
    timeout = getattr(settings, 'EMAIL_TIMEOUT', None)
    if not timeout or timeout * 4 > settings.JOBS_LOCK_TIMEOUT:
        return [checks.Warning(
            f'EMAIL_TIMEOUT is {timeout!r}; it should be set and at most a quarter of JOBS_LOCK_TIMEOUT '
            f'({settings.JOBS_LOCK_TIMEOUT}s), or a stuck SMTP server can make the worker send an email twice.',
            id='jobs.W001')]
    return []
