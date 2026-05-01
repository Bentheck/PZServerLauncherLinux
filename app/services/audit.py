from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import AuditEntry, User


def record_audit(
    db: Session,
    *,
    event_type: str,
    subject_type: str,
    subject_id: str,
    actor: User | None,
    message: str,
) -> None:
    entry = AuditEntry(
        event_type=event_type,
        subject_type=subject_type,
        subject_id=subject_id,
        actor_user_id=actor.id if actor else None,
        actor_label=actor.username if actor else "system",
        message=message,
    )
    db.add(entry)
    db.commit()
