"""Let the application record the request id of each audit event (Phase 10, ADR 0017).

audit_events.request_id exists since 0004 and was left unwritable by 0013 until request
correlation existed. The application now names it in every audit insert (NULL outside a
request), so the runtime group gets INSERT on that one column. Nothing else changes: still no
SELECT on audit_events, no UPDATE, no other column.

The group must exist, as for 0013 (run the bootstrap first): GRANT to a missing role fails.

Downgrade revokes the column again, after which every audit insert of this version is refused
(it names the column). A group that no longer exists holds nothing to revoke.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-23 12:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | Sequence[str] | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNTIME_GROUP = "ledger_runtime"


def upgrade() -> None:
    op.execute(f'GRANT INSERT (request_id) ON public.audit_events TO "{RUNTIME_GROUP}"')


def downgrade() -> None:
    op.execute(
        f"""
DO $$
BEGIN
    IF pg_catalog.to_regrole('{RUNTIME_GROUP}') IS NOT NULL THEN
        EXECUTE 'REVOKE INSERT (request_id) ON public.audit_events FROM "{RUNTIME_GROUP}"';
    END IF;
END;
$$
"""
    )
