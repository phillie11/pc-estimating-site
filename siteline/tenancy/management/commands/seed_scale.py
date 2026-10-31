"""
Build a large company for performance work (tenancy/scale.py): 200 jobs, 40 staff, 300 subs and
two years of records. Never run it against production: it is for a throwaway database.

    DEBUG=1 python manage.py seed_scale                 # the full size
    DEBUG=1 python manage.py seed_scale --jobs 20       # a quick small one

Logins: owner@scale.test, staff00@scale.test ..., client000@scale.test, sub000@scale.test, all with
the password in tenancy.scale.PASSWORD. Owners have no two-factor device: sign in with
TWO_FACTOR_ENFORCE=0 on a local server.
"""
from django.core.management.base import BaseCommand, CommandError

from tenancy.models import Organization


class Command(BaseCommand):
    help = 'Build a large demo company for performance testing (throwaway databases only).'

    def add_arguments(self, parser):
        parser.add_argument('--jobs', type=int, default=200)
        parser.add_argument('--name', default='Scale Fitout')

    def handle(self, *args, jobs, name, **opts):
        from tenancy import scale
        if Organization.objects.filter(name=name).exists():
            raise CommandError(f'{name} already exists. Use a fresh database.')
        k = max(jobs / 200, 0.05)
        built = scale.build(name, jobs=jobs, staff=max(5, round(40 * k)) if jobs < 200 else 40,
                            subs=max(30, round(300 * k)) if jobs < 200 else 300, stdout=self.stdout)
        self.stdout.write(self.style.SUCCESS(f'Built /{built.slug}/ with {jobs} jobs.'))
        for role, user in built.users.items():
            self.stdout.write(f'  {role}: {user.email}')
