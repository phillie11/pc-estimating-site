"""
The seam where drafted clarifications could plug in later. Nothing here drafts anything today.

A future drafter (for example one that reads the workbook's review lines, exclusions and zero rates and
suggests clarification wording) would implement `draft(pack)` to return unsaved PackClarification rows with
source=PackClarification.FROM_DRAFT, for a person to accept, edit or discard on the Clarifications page.
Drafts must never be saved or issued without a person accepting them, and must never leave the company.

    from handoverpack import clarifications
    if clarifications.available():
        suggestions = clarifications.draft(pack)
"""


def available():
    """Whether a drafter is switched on. None is built, so always False."""
    return False


def draft(pack):
    """Suggested clarifications for a pack: unsaved PackClarification rows (source FROM_DRAFT). None yet."""
    return []
