from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from directory.models import DEFAULT_TRADES, Trade
from projects.models import ProjectInvite, ProjectMember
from tenancy.context import org_context
from tenancy.models import Membership, Organization, TeamInvite, log_activity

from .forms import AcceptInviteNewUserForm, LoginForm, NewOrgForm, ProfileForm, SignupForm
from .models import User


def landing(request):
    if request.user.is_authenticated:
        return redirect('home')
    from django.conf import settings
    return render(request, 'marketing/landing.html', {'plans': settings.PLANS, 'trial_days': settings.TRIAL_DAYS})


def create_organization(name, owner):
    """New workspace with the owner, default trades and a trial."""
    with transaction.atomic():
        org = Organization.objects.create(name=name)
        Membership.objects.create(organization=org, user=owner, role=Membership.ROLE_OWNER)
        with org_context(org):
            Trade.objects.bulk_create([Trade(organization=org, name=t) for t in DEFAULT_TRADES])
            log_activity(owner, 'created the workspace', org.name)
    return org


def signup(request):
    if request.user.is_authenticated:
        return redirect('home')
    form = SignupForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        d = form.cleaned_data
        with transaction.atomic():
            user = User.objects.create_user(email=d['email'], password=d['password'], full_name=d['full_name'])
            org = create_organization(d['company_name'], user)
        login(request, user, backend='django.contrib.auth.backends.ModelBackend')
        messages.success(request, f'Welcome. Your workspace is ready and your free trial has started.')
        return redirect('dashboard', org_slug=org.slug)
    return render(request, 'accounts/signup.html', {'form': form})


class LoginView(auth_views.LoginView):
    form_class = LoginForm
    template_name = 'accounts/login.html'
    redirect_authenticated_user = True


@require_POST
def logout_view(request):
    logout(request)
    return redirect('login')


@login_required
def home(request):
    """Send the user to the right workspace."""
    memberships = list(request.user.memberships.filter(is_active=True).select_related('organization'))
    if not memberships:
        return redirect('org_picker')
    last = next((m for m in memberships if m.organization.slug == request.user.last_org_slug), None)
    if last or len(memberships) == 1:
        return redirect('dashboard', org_slug=(last or memberships[0]).organization.slug)
    return redirect('org_picker')


@login_required
def org_picker(request):
    form = NewOrgForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        org = create_organization(form.cleaned_data['company_name'], request.user)
        return redirect('dashboard', org_slug=org.slug)
    memberships = request.user.memberships.filter(is_active=True).select_related('organization')
    return render(request, 'accounts/org_picker.html', {'memberships': memberships, 'form': form})


@login_required
def profile(request):
    form = ProfileForm(request.POST or None, instance=request.user)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Profile saved.')
        return redirect('profile')
    return render(request, 'accounts/profile.html', {'form': form})


# ---------------------------------------------------------------------------
# Invites
# ---------------------------------------------------------------------------

def _accept_flow(request, invite, email, on_accept, context):
    """Shared logic: existing account → log in & accept; new → create account."""
    existing = User.objects.filter(email__iexact=email).first()
    if request.user.is_authenticated:
        if request.user.email.lower() != email.lower():
            return render(request, 'accounts/invite_wrong_user.html', {'invite_email': email}, status=403)
        if request.method == 'POST':
            return on_accept(request.user)
        return render(request, 'accounts/invite_accept.html', {**context, 'form': None})
    if existing:
        messages.info(request, 'You already have an account. Log in to accept the invitation.')
        return redirect(f"/login/?next={request.path}")
    form = AcceptInviteNewUserForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        user = User.objects.create_user(email=email, password=form.cleaned_data['password'],
                                        full_name=form.cleaned_data['full_name'])
        login(request, user, backend='django.contrib.auth.backends.ModelBackend')
        return on_accept(user)
    return render(request, 'accounts/invite_accept.html', {**context, 'form': form, 'invite_email': email})


def accept_team_invite(request, token):
    invite = get_object_or_404(TeamInvite.objects.select_related('organization'), token=token)
    if not invite.is_valid():
        return render(request, 'accounts/invite_invalid.html', status=410)
    org = invite.organization

    def on_accept(user):
        with transaction.atomic():
            m, created = Membership.objects.get_or_create(organization=org, user=user,
                                                          defaults={'role': invite.role, 'area': invite.area})
            if not created and (m.role == Membership.ROLE_EXTERNAL or not m.is_active):
                m.role, m.is_active, m.area = invite.role, True, invite.area
                m.save()
            invite.status = TeamInvite.STATUS_ACCEPTED
            invite.save(update_fields=['status'])
            with org_context(org):
                log_activity(user, 'joined the team', user.full_name)
        messages.success(request, f'You have joined {org.name}.')
        return redirect('dashboard', org_slug=org.slug)

    return _accept_flow(request, invite, invite.email, on_accept,
                        {'org_name': org.name, 'what': f'join the {org.name} team', 'invite': invite})


def accept_project_invite(request, token):
    invite = get_object_or_404(ProjectInvite.all_objects.select_related('organization', 'project'), token=token)
    if not invite.is_valid():
        return render(request, 'accounts/invite_invalid.html', status=410)
    org, project = invite.organization, invite.project

    def on_accept(user):
        with transaction.atomic(), org_context(org):
            m, created = Membership.objects.get_or_create(organization=org, user=user,
                                                          defaults={'role': Membership.ROLE_EXTERNAL})
            if not m.is_active:
                # A removed staff member returns as an external collaborator, never with their old role
                m.is_active, m.role = True, Membership.ROLE_EXTERNAL
                m.save(update_fields=['is_active', 'role'])
            ProjectMember.objects.get_or_create(
                project=project, user=user,
                defaults={'role': invite.role, 'company_name': invite.company_name,
                          'subcontractor': invite.subcontractor, 'added_by': invite.invited_by},
            )
            invite.status = ProjectInvite.STATUS_ACCEPTED
            invite.save(update_fields=['status'])
            log_activity(user, 'joined the project', project.name, project=project, internal_only=False)
        messages.success(request, f'You now have access to {project.name}.')
        return redirect('project_detail', org_slug=org.slug, pk=project.pk)

    return _accept_flow(request, invite, invite.email, on_accept,
                        {'org_name': org.name, 'what': f'collaborate on {project.name}', 'invite': invite})
