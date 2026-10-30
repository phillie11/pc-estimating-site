from django.core.exceptions import PermissionDenied


class RecordConflict(Exception):
    """Someone else saved this record after it was loaded (its version moved on). Reload and try again."""


class InvalidTransition(Exception):
    """The transition doesn't exist for this record type, or not from the record's current status."""


class TransitionForbidden(PermissionDenied):
    """The transition exists but this user may not make it. A PermissionDenied, so views answer 403."""


class PurgeRefused(Exception):
    """A record can't be purged: not soft-deleted first, under legal hold, or still inside its retention period."""
