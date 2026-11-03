"""
Signals connected in CloseoutConfig.ready():

  Issue pre_save        A snag raised on or after the whole-works practical completion date is
                        saved as a defect (issue_type DEFECT). Other types (health and safety,
                        quality) are left as they are. Rows are pinned to the issue's own
                        organisation and project, so all_objects is safe.
  Issue post_save       When a defect reported by the client side is closed (the PM has checked
                        the fix), the person who reported it is told.
  MainContract save     A practical completion date typed on the contract is copied to the job's
                        whole-works completion section (made if missing), and a new contract
                        takes the date already recorded, so the two never disagree.
"""
from django.urls import reverse
from django.utils import timezone


def defect_after_pc(sender, instance, **kwargs):
    from . import rules
    from .models import whole_works_pc
    if instance.pk is not None or instance.issue_type != 'SNAG' or not instance.project_id:
        return
    if rules.is_defect_phase(whole_works_pc(instance.project), timezone.localdate()):
        instance.issue_type = 'DEFECT'


def remember_status(sender, instance, **kwargs):
    if instance.pk is not None and instance.issue_type == 'DEFECT':
        instance._closeout_old_status = type(instance).all_objects.filter(pk=instance.pk) \
            .values_list('status', flat=True).first()


def tell_reporter(sender, instance, created, **kwargs):
    from projects.models import Issue, ProjectMember
    from tenancy import notify

    from .access import CLIENT_ROLES
    old = getattr(instance, '_closeout_old_status', None)
    if created or instance.issue_type != 'DEFECT' or instance.status != Issue.STATUS_CLOSED or old in (None, Issue.STATUS_CLOSED):
        return
    instance._closeout_old_status = instance.status
    reporter = instance.created_by
    if reporter is None or not ProjectMember.all_objects.filter(
            organization_id=instance.organization_id, project_id=instance.project_id, user=reporter,
            role__in=CLIENT_ROLES).exists():
        return
    url = reverse('closeout_client', args=[instance.project.organization.slug, instance.project_id]) + '#defects'
    notify.alert([reporter], f'Defect fixed and checked: {instance.reference} {instance.title}', url,
                 project=instance.project, org=instance.project.organization)


def contract_pc(sender, instance, created=False, **kwargs):
    """
    Keep the whole-works section and the contract's PC date the same. Writes use update(), so
    neither side's save signal fires again (no loop).

      no whole-works row yet     a PC date on the contract makes one, so the defect phase and the
                                 end-of-defects reminder (which read the row) see it too
      a new contract, PC blank   it takes the date already recorded on the Close-out tab, instead
                                 of wiping it
      otherwise                  the contract's date (blank included) is copied to the row
    """
    from .models import CompletionSection
    row = CompletionSection.all_objects.filter(organization_id=instance.organization_id, project_id=instance.project_id,
                                               is_whole_works=True).first()
    pc = instance.practical_completion_on
    if row is None:
        if pc is not None and not CompletionSection.all_objects.filter(
                project_id=instance.project_id, name=CompletionSection.WHOLE_WORKS).exists():
            CompletionSection(organization_id=instance.organization_id, project_id=instance.project_id,
                              is_whole_works=True, name=CompletionSection.WHOLE_WORKS, pc_date=pc).save()
        return
    if created and pc is None and row.pc_date is not None:
        type(instance).all_objects.filter(pk=instance.pk).update(practical_completion_on=row.pc_date)
        instance.practical_completion_on = row.pc_date
        return
    if row.pc_date != pc:
        CompletionSection.all_objects.filter(pk=row.pk).update(pc_date=pc)
