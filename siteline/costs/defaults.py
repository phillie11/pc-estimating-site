"""
The starting cost code library for a UK fit-out contractor. Each company gets a copy the first
time cost control is opened (costs.access.settings_for), and edits it from Settings > Cost control.
Trade package names match directory.models.DEFAULT_TRADES so an appointed package can find its code.
"""

DEFAULT_CODES = [
    # (code, name, group)
    ('100', 'Site management and supervision', 'PRELIMS'),
    ('110', 'Site set-up, welfare and accommodation', 'PRELIMS'),
    ('120', 'Protection, hoarding and temporary works', 'PRELIMS'),
    ('130', 'Waste and skips', 'PRELIMS'),
    ('140', 'Cleaning and final clean', 'PRELIMS'),
    ('150', 'Access, scaffolding and towers', 'PRELIMS'),
    ('200', 'Strip out & demolition', 'PACKAGES'),
    ('210', 'Drylining & partitions', 'PACKAGES'),
    ('215', 'Suspended ceilings', 'PACKAGES'),
    ('220', 'Joinery', 'PACKAGES'),
    ('225', 'Glazing', 'PACKAGES'),
    ('230', 'Flooring', 'PACKAGES'),
    ('235', 'Decorating', 'PACKAGES'),
    ('240', 'Mechanical', 'PACKAGES'),
    ('245', 'Plumbing', 'PACKAGES'),
    ('250', 'Electrical', 'PACKAGES'),
    ('255', 'Fire protection', 'PACKAGES'),
    ('260', 'Metalwork', 'PACKAGES'),
    ('265', 'Signage', 'PACKAGES'),
    ('290', 'Other trade packages', 'PACKAGES'),
    ('300', 'Direct labour', 'LABOUR'),
    ('310', 'Agency labour', 'LABOUR'),
    ('400', 'Materials bought direct', 'MATERIALS'),
    ('500', 'Plant and tool hire', 'PLANT'),
    ('600', 'Design and consultant fees', 'FEES'),
    ('610', 'Building control and statutory fees', 'FEES'),
    ('620', 'Insurances and bonds', 'FEES'),
    ('900', 'Contingency and risk', 'CONTINGENCY'),
]
