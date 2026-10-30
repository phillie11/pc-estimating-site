from django.contrib import messages
from django.db import transaction
from django.shortcuts import redirect, render
from django.utils import timezone

from projects.access import get_project
from tenancy.models import log_activity
from tenancy.permissions import internal_required

from .commercial import commercial_context, commercial_position
from .forms import ContractForm
from .models import MainContract
from .presets import ADVISORY, SCHEME


def _contract(project):
    return MainContract.objects.filter(project=project).select_related('administrator__user').first()


@internal_required
def contract_detail(request, org_slug, pk):
    project = get_project(request, pk)
    contract = _contract(project)
    from changes.models import ChangeEvent, EarlyWarning
    ces = ChangeEvent.objects.filter(project=project)
    changes = {
        'early_warnings': EarlyWarning.objects.filter(project=project, status=EarlyWarning.STATUS_OPEN).count(),
        'change_events': ces.filter(status__in=ChangeEvent.LIVE_STATUSES).count(),
        'revenue': sum((ce.revenue_total for ce in ces.filter(status__in=ChangeEvent.LIVE_STATUSES)
                        .prefetch_related('lines')), start=0),
    }
    today = timezone.localdate()
    return render(request, 'contracts/contract.html', commercial_context(
        request, project, 'contract', contract=contract, position=commercial_position(project), changes=changes,
        timetable=contract.next_payment_timetable(today) if contract else None, advisory=ADVISORY,
        scheme=SCHEME, today=today))


@internal_required
def contract_edit(request, org_slug, pk):
    project = get_project(request, pk)
    contract = _contract(project)
    creating = contract is None
    instance = contract or MainContract(project=project)
    if request.method == 'POST' and request.POST.get('action') == 'preset' and not creating:
        changed = contract.apply_preset(only_blank=False)
        contract.updated_by = request.user
        contract.save()
        messages.success(request, (f'Payment, retention and defects terms reset to the standard '
                                   f'{contract.preset.label} terms. {ADVISORY}') if changed else
                         'These already match the standard terms.')
        return redirect('contract_detail', org_slug=org_slug, pk=pk)
    form = ContractForm(request.POST or None, instance=instance, project=project)
    if request.method == 'POST' and form.is_valid():
        contract = form.save(commit=False, actor=request.user)  # also sets the sum and dates on the project
        filled = contract.apply_preset(only_blank=True, creating=creating)
        with transaction.atomic():
            project.save(update_fields=['value', 'start_date', 'completion_date', 'updated_at'])
            contract.save()
        log_activity(request, 'set up the contract' if creating else 'updated the contract', contract.form_label,
                     project=project, url=contract.get_absolute_url(), internal_only=True)
        note = f' Blank terms were filled from the standard {contract.preset.label} terms. {ADVISORY}' if filled else ''
        messages.success(request, ('Contract set up.' if creating else 'Contract updated.') + note)
        return redirect('contract_detail', org_slug=org_slug, pk=pk)
    return render(request, 'contracts/contract_form.html', commercial_context(
        request, project, 'contract', form=form, contract=contract, creating=creating, advisory=ADVISORY))
