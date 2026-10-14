from datetime import UTC, datetime
from unittest.mock import MagicMock

from app.db.session import detach_instance, eager_load_columns
from app.models.core import PolicyRevision


def test_eager_load_columns_reads_revision_scalars() -> None:
    revision = PolicyRevision(
        id="rev-1",
        gap_analysis_id="gap-1",
        policy_id="policy-1",
        org_id="org_example",
        redline_file_url=None,
        status="PENDING",
        created_at=datetime.now(UTC),
    )

    eager_load_columns(revision)

    assert revision.id == "rev-1"
    assert revision.org_id == "org_example"


def test_detach_instance_expunges_after_loading_columns() -> None:
    revision = PolicyRevision(
        id="rev-1",
        gap_analysis_id="gap-1",
        policy_id="policy-1",
        org_id="org_example",
        redline_file_url="storage://policy-redlines/org/rev.docx",
        status="PENDING",
        created_at=datetime.now(UTC),
    )
    db = MagicMock()

    detached = detach_instance(revision, db)

    db.expunge.assert_called_once_with(revision)
    assert detached.redline_file_url.endswith("rev.docx")
