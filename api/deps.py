"""
api/deps.py — Shared FastAPI dependencies.
"""
from __future__ import annotations

from fastapi import Header, HTTPException

from repositories.subject_repo import SubjectRepository


def get_subject_id(
    x_subject_id: str | None = Header(None, alias="X-Subject-Id"),
    subject_id: str | None = None,
) -> str:
    """
    Resolve the current subject_id for subject-scoped endpoints.

    Resolution order:
    1. X-Subject-Id request header (explicit per-call override)
    2. subject_id query parameter (for direct browser navigations, downloads, previews)
    3. Stored active_subject_id in app_settings

    Raises 400 if neither is available.
    Raises 404 if the provided subject_id does not exist.
    """
    repo = SubjectRepository()

    candidate = x_subject_id or subject_id
    if candidate:
        if not repo.get_subject_by_slug(candidate):
            raise HTTPException(404, f"Subject '{candidate}' not found")
        return candidate

    active = repo.get_active_subject_id()
    if not active:
        raise HTTPException(
            400,
            "Please select a subject to continue.",
        )
    return active
