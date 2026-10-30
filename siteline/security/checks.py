from django.conf import settings
from django.core import checks


@checks.register(checks.Tags.security, deploy=True)
def two_factor_enforced(app_configs, **kwargs):
    if not getattr(settings, 'TWO_FACTOR_ENFORCE', True):
        return [checks.Warning(
            'TWO_FACTOR_ENFORCE is off, so owners and admins can work without two-factor sign-in.',
            hint='Remove TWO_FACTOR_ENFORCE=0 from the environment.', id='security.W001')]
    return []


@checks.register(checks.Tags.security, deploy=True)
def shared_rate_limit_cache(app_configs, **kwargs):
    backend = settings.CACHES.get('default', {}).get('BACKEND', '')
    if backend.endswith('LocMemCache') or backend.endswith('DummyCache'):
        return [checks.Warning(
            f'The default cache is {backend.rsplit(".", 1)[-1]}, so rate limits are counted per web '
            'process (or not at all) instead of across the site.',
            hint="Use the database cache (CACHE_BACKEND unset) and run migrate.", id='security.W002')]
    return []
