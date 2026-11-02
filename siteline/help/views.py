from django.contrib import messages
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from projects.access import is_internal
from tenancy.permissions import admin_required

from . import onboarding, search, topics


def search_page(request, org_slug):
    q = search.clean(request.GET.get('q', ''))
    groups = search.run(request, q)
    return render(request, 'help/search.html', {
        'q': q, 'groups': groups, 'too_short': 0 < len(q) < search.MIN_LENGTH,
        'total': sum(len(hits) for _, _, hits in groups), 'internal': is_internal(request),
    })


def help_index(request, org_slug):
    internal = is_internal(request)
    return render(request, 'help/index.html', {'topics': topics.listed(internal)})


def help_topic(request, org_slug, slug):
    topic = topics.get(slug, internal=is_internal(request))
    if topic is None:
        raise Http404
    return render(request, 'help/topic.html', {'topic': topic})


@require_POST
@admin_required
def onboarding_dismiss(request, org_slug):
    onboarding.dismiss(request)
    messages.success(request, 'Getting started checklist hidden. Everything on it is still under Settings and Team.')
    return redirect('dashboard', org_slug=org_slug)
