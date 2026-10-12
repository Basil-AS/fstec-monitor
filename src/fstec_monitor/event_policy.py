"""Canonical classification for user-facing and operational event streams."""

CHANGE_EVENT_KINDS = frozenset(
    {
        "document_added",
        "document_removed",
        "document_restored",
        "html_content_changed",
        "attachment_added",
        "attachment_removed",
        "attachment_content_changed",
        "attachment_review_required",
    }
)

OPERATIONAL_ERROR_KINDS = frozenset({"fetch_error", "storage_error"})

# Binary-only changes remain in the audit trail but are not semantic change alerts.
AUDIT_ONLY_EVENT_KINDS = frozenset({"attachment_binary_changed"})
