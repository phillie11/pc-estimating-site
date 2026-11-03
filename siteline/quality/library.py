"""
The starting checklist library (inspection and test plans) for a fit-out contractor. Each company
gets its own editable copy the first time anyone opens Quality (ensure_library). Changing or
retiring a checklist never touches inspections already started from it: their checks were copied.

These are typical checks only. Each company should tailor them to its own specifications,
manufacturers' instructions and quality plan.
"""
from django.db import transaction

DEFAULT_TEMPLATES = [
    ('Drylining', 'Partitions: before boarding', 'Check the frame before the first layer of board goes on.', [
        'Head and base track fixed at the centres on the drawing',
        'Studs at the specified centres and the right gauge',
        'Deflection head allowed for where specified',
        'Door openings framed with the right studs or timber',
        'Pattresses and noggins in for fixtures, sanitaryware and wall-hung units',
        'Services first fix complete and tested in the wall',
        'Insulation in place, no gaps',
        'Partition setting out matches the drawing',
    ]),
    ('Drylining', 'Partitions: boarded and ready for finishing', 'Check the boarding before taping and jointing.', [
        'Board type and layers as specified (fire, acoustic, moisture)',
        'Joints staggered between layers',
        'Screw centres as the manufacturer\'s system',
        'Fire-rated junctions sealed at head, base and ends',
        'Penetrations recorded on the fire-stopping register',
        'Beads straight and fixed',
        'No damaged board left in',
    ]),
    ('Ceilings', 'Suspended ceilings', 'Check the grid and tiles before handover.', [
        'Hangers at the specified centres, independent of services',
        'Grid level within tolerance',
        'Perimeter trim fixed and straight',
        'Light fittings, grilles and sprinkler heads supported independently where required',
        'Access panels where the drawings show them',
        'Tiles clean, undamaged and the right type',
        'Fire-rated ceiling (if any) built to the system details',
    ]),
    ('Mechanical and electrical', 'M&E first fix', 'Check services before walls and ceilings are closed up.', [
        'Containment fixed at the specified centres',
        'Cables, pipes and ducts in the right routes on the drawings',
        'Pipework pressure tested and certificate seen',
        'Insulation complete on pipework and ductwork',
        'Back boxes at the right heights and positions',
        'Fire-stopping needs recorded where services pass through rated walls',
        'Labels and identification in place',
        'Photos taken of the services before close-up',
    ]),
    ('Mechanical and electrical', 'M&E second fix', 'Check fittings and terminations before testing.', [
        'Accessories level, fixed and at the right heights',
        'Light fittings as the schedule, working and clean',
        'Grilles and diffusers fitted and clean',
        'Sanitaryware fixed and sealed',
        'Testing and commissioning certificates received',
        'Circuit charts and labels complete',
    ]),
    ('Doors and joinery', 'Fire doors', 'Check every fire door against its certification.', [
        'Door and frame are a certified fire door set (label or plug present)',
        'Gaps at head and sides 2 to 4 mm, or as the certificate',
        'Intumescent and smoke seals fitted and unbroken',
        'Hinges: three, CE or UKCA marked, all screws in',
        'Self-closer shuts the door fully from any angle',
        'Signage fitted (Fire door keep shut)',
        'Glazing is fire-rated with the right beads and seals',
        'No unapproved holes or alterations',
    ]),
    ('Decorations', 'Decorations', 'Check paintwork before the snagging walk round.', [
        'Surfaces prepared and filled before painting',
        'Colours and finishes as the schedule',
        'Even coverage, no runs, misses or grinning through',
        'Cut-in lines straight at ceilings, skirtings and frames',
        'Ironmongery, sockets and glass free of paint',
        'Touch-ups blend in',
    ]),
]


def seed_templates(org, user=None):
    """Create the default checklists for a company (call inside its org_context). Returns how many."""
    from .models import ChecklistTemplate, TemplateItem
    items = []
    for trade, name, description, checks in DEFAULT_TEMPLATES:
        t = ChecklistTemplate.objects.create(organization=org, name=name, trade=trade, description=description,
                                             created_by=user)
        items += [TemplateItem(organization=org, template=t, order=i, text=text) for i, text in enumerate(checks)]
    TemplateItem.objects.bulk_create(items)
    return len(DEFAULT_TEMPLATES)


def ensure_library(org, user=None):
    """Give the company its starting checklists, once. Safe to call on every page that needs them."""
    from .models import QualitySettings
    settings = QualitySettings.for_org(org)
    if settings.library_seeded:
        return False
    with transaction.atomic():
        # Locked so two people opening Quality at once don't both seed it
        QualitySettings.objects.get_or_create(organization=org)  # the unique constraint settles a race
        row = QualitySettings.objects.select_for_update().get(organization=org)
        if row.library_seeded:
            return False
        seed_templates(org, user)
        row.library_seeded = True
        row.save()
    return True
