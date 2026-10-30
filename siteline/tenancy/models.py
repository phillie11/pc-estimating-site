import secrets
import uuid
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import FullResultSet, ImproperlyConfigured
from django.db import models, transaction
from django.db.models.sql.where import AND
from django.utils import timezone
from django.utils.text import slugify

from .context import TenantContextMissing, get_current_org, is_unscoped

RESERVED_SLUGS = {
    'admin', 'login', 'logout', 'signup', 'register', 'billing', 'static', 'media',
    'invite', 'invites', 'accounts', 'account', 'orgs', 'api', 'pricing', 'platform',
    'password', 'files', 'help', 'support', 'about', 'terms', 'privacy', 'www', 'app',
    'settings', 'dashboard', 'stripe', 'webhook', 'webhooks', 'health', 'home',
    'password-reset', 'platform-admin',
}


class Organization(models.Model):
    """A customer company. Everything a customer owns hangs off one of these."""

    STATUS_TRIAL = 'trial'
    STATUS_ACTIVE = 'active'
    STATUS_TRIALING = 'trialing'  # Stripe-managed trial
    STATUS_PAST_DUE = 'past_due'
    STATUS_CANCELED = 'canceled'
    STATUS_UNPAID = 'unpaid'
    STATUS_INCOMPLETE = 'incomplete'
    STATUS_INCOMPLETE_EXPIRED = 'incomplete_expired'
    STATUS_PAUSED = 'paused'
    SUBSCRIPTION_STATUS_CHOICES = [
        (STATUS_TRIAL, 'Free trial'),
        (STATUS_TRIALING, 'Trialing (Stripe)'),
        (STATUS_ACTIVE, 'Active'),
        (STATUS_PAST_DUE, 'Past due'),
        (STATUS_CANCELED, 'Cancelled'),
        (STATUS_UNPAID, 'Unpaid'),
        (STATUS_INCOMPLETE, 'Incomplete'),
        (STATUS_INCOMPLETE_EXPIRED, 'Incomplete (expired)'),
        (STATUS_PAUSED, 'Paused'),
    ]

    name = models.CharField(max_length=160)
    slug = models.SlugField(max_length=60, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # Profile (shown to external collaborators on the portal)
    logo = models.ImageField(upload_to='org-logos/', blank=True)
    brand_colour = models.CharField(max_length=7, default='#E8590C', help_text='Hex colour, e.g. #E8590C')
    address = models.TextField(blank=True)
    phone = models.CharField(max_length=40, blank=True)
    website = models.URLField(blank=True)
    company_number = models.CharField(max_length=20, blank=True)
    vat_number = models.CharField(max_length=20, blank=True)
    currency_symbol = models.CharField(max_length=3, default='£')

    # Billing
    plan = models.CharField(max_length=20, default=settings.TRIAL_PLAN)
    subscription_status = models.CharField(max_length=24, choices=SUBSCRIPTION_STATUS_CHOICES, default=STATUS_TRIAL)
    trial_ends_at = models.DateTimeField(null=True, blank=True)
    stripe_customer_id = models.CharField(max_length=64, blank=True, db_index=True)
    stripe_subscription_id = models.CharField(max_length=64, blank=True, db_index=True)
    current_period_end = models.DateTimeField(null=True, blank=True)
    cancel_at_period_end = models.BooleanField(default=False)
    payment_failed_at = models.DateTimeField(null=True, blank=True)
    is_suspended = models.BooleanField(default=False, help_text='Platform-admin kill switch.')

    project_counter = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    # --- creation -------------------------------------------------------
    @classmethod
    def unique_slug_for(cls, name):
        base = slugify(name)[:50] or 'company'
        if base in RESERVED_SLUGS:
            base = f'{base}-co'
        slug, n = base, 2
        while cls.objects.filter(slug=slug).exists():
            slug = f'{base}-{n}'
            n += 1
        return slug

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = self.unique_slug_for(self.name)
        if self.slug in RESERVED_SLUGS:
            raise ValueError(f'"{self.slug}" is a reserved slug')
        if self._state.adding and self.trial_ends_at is None:
            self.trial_ends_at = timezone.now() + timedelta(days=settings.TRIAL_DAYS)
        super().save(*args, **kwargs)

    # --- billing state --------------------------------------------------
    @property
    def plan_config(self):
        return settings.PLANS.get(self.plan) or settings.PLANS[settings.TRIAL_PLAN]

    @property
    def is_on_internal_trial(self):
        return self.subscription_status == self.STATUS_TRIAL

    @property
    def trial_days_left(self):
        if not self.trial_ends_at:
            return 0
        return max(0, (self.trial_ends_at - timezone.now()).days)

    def access_state(self):
        """'ok' or a reason the workspace is locked."""
        now = timezone.now()
        if self.is_suspended:
            return 'suspended'
        status = self.subscription_status
        if status in (self.STATUS_ACTIVE, self.STATUS_TRIALING):
            return 'ok'
        if status == self.STATUS_TRIAL:
            return 'ok' if self.trial_ends_at and self.trial_ends_at > now else 'trial_expired'
        if status == self.STATUS_PAST_DUE:
            started = self.payment_failed_at or now
            grace_end = started + timedelta(days=settings.PAYMENT_GRACE_DAYS)
            return 'ok' if now < grace_end else 'payment_failed'
        if status == self.STATUS_INCOMPLETE:
            # Checkout started but first payment not confirmed. Keep any
            # remaining trial running so a failed card does not lock them out.
            return 'ok' if self.trial_ends_at and self.trial_ends_at > now else 'inactive'
        return 'inactive'

    @property
    def has_access(self):
        return self.access_state() == 'ok'

    # --- limits ---------------------------------------------------------
    def staff_count(self):
        return self.memberships.filter(is_active=True).exclude(role=Membership.ROLE_EXTERNAL).count()

    def pending_staff_invites(self):
        return self.team_invites.filter(status=TeamInvite.STATUS_PENDING).count()

    def can_add_staff(self):
        limit = self.plan_config['max_staff']
        return limit is None or (self.staff_count() + self.pending_staff_invites()) < limit

    def active_project_count(self):
        from projects.models import Project
        return Project.active_count(self)  # open and not archived

    def can_add_project(self):
        from projects.models import Project
        return Project.plan_has_room(self)

    def next_project_number(self):
        with transaction.atomic():
            org = Organization.objects.select_for_update().get(pk=self.pk)
            org.project_counter += 1
            org.save(update_fields=['project_counter'])
            self.project_counter = org.project_counter
            return org.project_counter


class Membership(models.Model):
    """A user's place in an organisation."""

    ROLE_OWNER = 'OWNER'
    ROLE_ADMIN = 'ADMIN'
    ROLE_MEMBER = 'MEMBER'
    ROLE_EXTERNAL = 'EXTERNAL'
    ROLE_CHOICES = [
        (ROLE_OWNER, 'Owner'),
        (ROLE_ADMIN, 'Admin'),
        (ROLE_MEMBER, 'Staff'),
        (ROLE_EXTERNAL, 'External collaborator'),
    ]
    INTERNAL_ROLES = (ROLE_OWNER, ROLE_ADMIN, ROLE_MEMBER)
    ADMIN_ROLES = (ROLE_OWNER, ROLE_ADMIN)

    # Which side of the business a staff member works in. Owners and admins always see both.
    AREA_BOTH = 'BOTH'
    AREA_ESTIMATING = 'ESTIMATING'
    AREA_DELIVERY = 'DELIVERY'
    AREA_CHOICES = [(AREA_BOTH, 'Estimating & projects'), (AREA_ESTIMATING, 'Estimating'), (AREA_DELIVERY, 'Projects')]

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='memberships')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='memberships')
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default=ROLE_MEMBER)
    area = models.CharField('Works in', max_length=10, choices=AREA_CHOICES, default=AREA_BOTH)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('organization', 'user')]
        ordering = ['organization__name']

    def __str__(self):
        return f'{self.user} @ {self.organization} ({self.role})'

    @property
    def is_internal(self):
        return self.role in self.INTERNAL_ROLES

    @property
    def is_external(self):
        return self.role == self.ROLE_EXTERNAL

    @property
    def is_admin(self):
        return self.role in self.ADMIN_ROLES

    @property
    def is_owner(self):
        return self.role == self.ROLE_OWNER

    @property
    def can_estimate(self):
        return self.is_internal and (self.is_admin or self.area in (self.AREA_BOTH, self.AREA_ESTIMATING))

    @property
    def can_deliver(self):
        return self.is_internal and (self.is_admin or self.area in (self.AREA_BOTH, self.AREA_DELIVERY))

    @property
    def sections(self):
        return [s for s, ok in (('estimating', self.can_estimate), ('delivery', self.can_deliver)) if ok]


def _invite_token():
    return secrets.token_urlsafe(32)


def _default_expiry():
    return timezone.now() + timedelta(days=14)


class TeamInvite(models.Model):
    """Invitation for internal staff to join an organisation."""

    STATUS_PENDING = 'PENDING'
    STATUS_ACCEPTED = 'ACCEPTED'
    STATUS_REVOKED = 'REVOKED'
    STATUS_CHOICES = [(STATUS_PENDING, 'Pending'), (STATUS_ACCEPTED, 'Accepted'), (STATUS_REVOKED, 'Revoked')]
    ROLE_CHOICES = [(Membership.ROLE_ADMIN, 'Admin'), (Membership.ROLE_MEMBER, 'Staff')]

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='team_invites')
    email = models.EmailField()
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default=Membership.ROLE_MEMBER)
    area = models.CharField('Works in', max_length=10, choices=Membership.AREA_CHOICES, default=Membership.AREA_BOTH)
    token = models.CharField(max_length=64, unique=True, default=_invite_token, editable=False)
    invited_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING)
    expires_at = models.DateTimeField(default=_default_expiry)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def is_valid(self):
        return self.status == self.STATUS_PENDING and self.expires_at > timezone.now()


# ---------------------------------------------------------------------------
# Base class for every model that belongs to one organisation
# ---------------------------------------------------------------------------

class TenantFilter(models.Expression):
    """
    `organization_id = <the current organisation>`, decided when the SQL is
    compiled rather than when the queryset is built. Every TenantModel.objects
    queryset carries one in its WHERE clause, so whatever Django does with the
    query afterwards (`|`, `&`, `^`, union(), Subquery(), Exists()) the filter
    goes with it and is applied wherever and whenever the SQL finally runs.

    Pinned to an organisation (made while one was current) it keeps to that one
    and raises PermissionError inside another. Unpinned it uses the current
    organisation, matches everything inside unscoped(), and raises
    TenantContextMissing with neither.
    """
    conditional = True
    output_field = models.BooleanField()

    def __init__(self, column, org_pk=None):
        super().__init__()
        self.column, self.org_pk = column, org_pk

    def get_source_expressions(self):
        return [self.column]

    def set_source_expressions(self, exprs):
        (self.column,) = exprs

    def as_sql(self, compiler, connection):
        if compiler.query.is_empty():
            raise FullResultSet  # .none(): the query matches nothing anyway and is never sent
        model = self.column.target.model.__name__
        current = get_current_org()
        if self.org_pk is not None:
            if current is not None and current.pk != self.org_pk:
                raise PermissionError(f'{model} query built for one organisation run inside another.')
            org_pk = self.org_pk
        elif current is not None:
            org_pk = current.pk
        elif is_unscoped():
            raise FullResultSet  # no condition: every company
        else:
            raise TenantContextMissing(
                f'{model}.objects queried with no current organisation. '
                'Wrap the code in org_context(org), or use unscoped() / all_objects if it really means every company.'
            )
        sql, params = compiler.compile(self.column)
        return f'{sql} = %s', (*params, org_pk)


class TenantQuerySet(models.QuerySet):
    """
    Fails closed. A queryset made while an organisation is current is filtered
    to it. One made with none (a form field's class-level queryset, a command
    that forgot org_context) is filtered to whichever organisation is current
    when it runs, and raises TenantContextMissing if there is none, unless it
    is inside unscoped(). The filter is a TenantFilter in the WHERE clause, so
    it survives combining querysets and Subquery()/Exists(); _guarded() runs
    first on every way Django runs a query (iteration, count, exists,
    aggregate, update, delete, pk__in=qs) to fail early with a clear message.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._tenant_org = None        # the organisation this queryset is pinned to
        self._tenant_unscoped = False  # made inside unscoped()

    def _clone(self):
        c = super()._clone()
        c._tenant_org, c._tenant_unscoped = self._tenant_org, self._tenant_unscoped
        return c

    def _scoped(self):
        """Called by the manager: pin to the current organisation if there is one, else filter when run."""
        org = get_current_org()
        if org is None and is_unscoped():
            self._tenant_unscoped = True
            return self
        column = models.F('organization_id').resolve_expression(self.query, allow_joins=False)
        self.query.where.add(TenantFilter(column, org.pk if org else None), AND)
        self._tenant_org = org
        return self

    def _guarded(self):
        """Raise now if this queryset may not run here; the TenantFilter does the filtering."""
        current = get_current_org()
        if self._tenant_org is not None:
            if current is not None and current.pk != self._tenant_org.pk:
                raise PermissionError(f'{self.model.__name__} query built for one organisation run inside another.')
            return self
        if self._tenant_unscoped or is_unscoped() or self.query.is_empty():
            return self
        if current is None:
            raise TenantContextMissing(
                f'{self.model.__name__}.objects queried with no current organisation. '
                'Wrap the code in org_context(org), or use unscoped() / all_objects if it really means every company.'
            )
        return self

    # --- combining ----------------------------------------------------------
    # Each operand's own TenantFilter travels into the combined SQL. An
    # all_objects queryset has none, so it would read every company.
    def _check_operands(self, *others):
        for other in others:
            if isinstance(other, models.QuerySet) and not isinstance(other, TenantQuerySet):
                raise PermissionError(f'Combine {self.model.__name__}.objects querysets only with each other, '
                                      'not with all_objects.')

    def __or__(self, other):
        self._check_operands(other)
        return super().__or__(other)

    def __and__(self, other):
        self._check_operands(other)
        return super().__and__(other)

    def __xor__(self, other):
        self._check_operands(other)
        return super().__xor__(other)

    def union(self, *other_qs, all=False):
        self._check_operands(*other_qs)
        return super().union(*other_qs, all=all)

    def intersection(self, *other_qs):
        self._check_operands(*other_qs)
        return super().intersection(*other_qs)

    def difference(self, *other_qs):
        self._check_operands(*other_qs)
        return super().difference(*other_qs)

    # --- reads ------------------------------------------------------------
    def _fetch_all(self):
        if self._result_cache is None:
            self._guarded()
        super()._fetch_all()

    def iterator(self, chunk_size=None):
        return super(TenantQuerySet, self._guarded()).iterator(chunk_size)

    def aiterator(self, chunk_size=2000):
        return super(TenantQuerySet, self._guarded()).aiterator(chunk_size)

    def count(self):
        if self._result_cache is not None:
            return len(self._result_cache)
        return super(TenantQuerySet, self._guarded()).count()

    def exists(self):
        if self._result_cache is not None:
            return bool(self._result_cache)
        return super(TenantQuerySet, self._guarded()).exists()

    def aggregate(self, *args, **kwargs):
        return super(TenantQuerySet, self._guarded()).aggregate(*args, **kwargs)

    def explain(self, **kwargs):
        return super(TenantQuerySet, self._guarded()).explain(**kwargs)

    def resolve_expression(self, *args, **kwargs):  # used as a subquery, e.g. pk__in=qs
        return super(TenantQuerySet, self._guarded()).resolve_expression(*args, **kwargs)

    # --- writes -----------------------------------------------------------
    def update(self, **kwargs):
        qs = self._guarded()
        org = qs._tenant_org or get_current_org()
        for key in ('organization', 'organization_id'):
            if key in kwargs:
                target = getattr(kwargs[key], 'pk', kwargs[key])
                if org is not None and target != org.pk:
                    raise PermissionError('Refusing to move rows into a different organisation.')
        return super(TenantQuerySet, qs).update(**kwargs)

    update.alters_data = True

    def _update(self, values):
        return super(TenantQuerySet, self._guarded())._update(values)

    _update.alters_data = True

    def delete(self):
        return super(TenantQuerySet, self._guarded()).delete()

    delete.alters_data = True
    delete.queryset_only = True

    def _raw_delete(self, using):
        return super(TenantQuerySet, self._guarded())._raw_delete(using)

    _raw_delete.alters_data = True

    def bulk_create(self, objs, *args, **kwargs):
        """Same rules as TenantModel.save(), checked for every object before anything is written."""
        objs = list(objs)
        current = get_current_org() or self._tenant_org
        if current is None and not (self._tenant_unscoped or is_unscoped()):
            raise TenantContextMissing(f'{self.model.__name__}.objects.bulk_create() with no current organisation.')
        for obj in objs:
            if not obj.organization_id:
                if current is None:
                    raise ImproperlyConfigured(
                        f'{type(obj).__name__} bulk-created with no organisation and no current org context.'
                    )
                obj.organization = current
            elif current is not None and obj.organization_id != current.pk:
                raise PermissionError('Refusing to write a row into a different organisation.')
        return super().bulk_create(objs, *args, **kwargs)

    bulk_create.alters_data = True


class TenantManager(models.Manager.from_queryset(TenantQuerySet)):
    """
    Scopes to the current organisation and fails closed without one (see
    TenantQuerySet). It is the default manager, so reverse and many-to-many
    related managers (project.rfis, sub.trades), ModelChoiceFields and model
    form unique checks are scoped the same way.
    """

    def get_queryset(self):
        return super().get_queryset()._scoped()


class TenantModel(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, editable=False, related_name='+')

    objects = TenantManager()
    # Unscoped escape hatch: admin, webhooks, token lookups, cross-company commands only.
    all_objects = models.Manager()

    class Meta:
        abstract = True
        # Django uses the base manager to follow a foreign key (rfi.project), refresh_from_db,
        # save()'s UPDATE and delete cascades. Each of those is pinned to a pk or FK value from
        # a row the caller already holds, so it cannot list another company's data, and keeping
        # it unscoped means cascades and FK access still work outside a request.
        base_manager_name = 'all_objects'

    def save(self, *args, **kwargs):
        if not self.organization_id:
            org = get_current_org()
            if org is None:
                raise ImproperlyConfigured(
                    f'{type(self).__name__} saved with no organisation and no current org context.'
                )
            self.organization = org
        current = get_current_org()
        if current is not None and self.organization_id != current.pk:
            raise PermissionError('Refusing to write a row into a different organisation.')
        super().save(*args, **kwargs)


class ActivityEvent(TenantModel):
    """Audit trail / activity feed."""

    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    project = models.ForeignKey('projects.Project', null=True, blank=True, on_delete=models.CASCADE, related_name='activity')
    verb = models.CharField(max_length=80)
    target = models.CharField(max_length=255, blank=True)
    url = models.CharField(max_length=300, blank=True)
    internal_only = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.actor} {self.verb} {self.target}'


def log_activity(request_or_user, verb, target='', project=None, url='', internal_only=True):
    user = getattr(request_or_user, 'user', request_or_user)
    ActivityEvent.objects.create(
        actor=user if getattr(user, 'is_authenticated', False) else None,
        verb=verb, target=str(target)[:255], project=project, url=url, internal_only=internal_only,
    )


class Notification(TenantModel):
    """Something that needs a person's attention, shown under the bell and emailed as a link."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notifications')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    project = models.ForeignKey('projects.Project', null=True, blank=True, on_delete=models.CASCADE, related_name='+')
    title = models.CharField(max_length=255)
    url = models.CharField(max_length=300)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['user', 'read_at'])]

    def __str__(self):
        return self.title
