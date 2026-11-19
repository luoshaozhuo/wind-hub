"""Historical source selection never borrows content from a future revision."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from configuration_service.infrastructure.adapters.outbound.sqlite.repository import (
    SQLiteRepository,
)


def test_historical_yaml_source_is_bounded_by_requested_revision(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "configuration.db", application_version="test")
    repository.initialize()
    now = datetime.now(UTC).isoformat()
    original = b"schema_version: 1\nfile_version: 1.0.0\n"
    renamed = b"schema_version: 1\nfile_version: 1.0.0\n# renamed\n"
    future = b"schema_version: 1\nfile_version: 2.0.0\n"
    try:
        with repository.write() as connection:
            connection.execute(
                "INSERT INTO config_schemas VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("site:1", "site", 1, "{}", "{}", "a" * 64, now),
            )
            connection.execute(
                "INSERT INTO config_documents VALUES (?, ?, ?)",
                ("site-document", "site", "site"),
            )
            for version, identity in (("1.0.0", "site-v1"), ("2.0.0", "site-v2")):
                connection.execute(
                    "INSERT INTO config_versions VALUES (?, ?, ?, ?, ?, ?)",
                    (identity, "site-document", "site:1", version, "b" * 64, now),
                )
            for revision, parent in ((1, None), (2, 1), (3, 2)):
                connection.execute(
                    "INSERT INTO revisions VALUES (?, ?, ?, ?, ?)",
                    (revision, parent, "tester", f"r-{revision}", now),
                )
            for revision, path, identity, payload in (
                (1, "instances/site.yaml", "site-v1", original),
                (3, "archive/site.yaml", "site-v1", renamed),
                (3, "archive/site_v2.yaml", "site-v2", future),
            ):
                connection.execute(
                    "INSERT INTO revision_files VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        revision,
                        path,
                        "site-document",
                        identity,
                        hashlib.sha256(payload).hexdigest(),
                        payload,
                    ),
                )

        assert repository.get_version_source("site-v1", revision_id=1) == (
            "instances/site.yaml",
            original,
            hashlib.sha256(original).hexdigest(),
        )
        assert repository.get_version_source("site-v1", revision_id=2) == (
            "instances/site.yaml",
            original,
            hashlib.sha256(original).hexdigest(),
        )
        assert repository.get_version_source("site-v1", revision_id=3) == (
            "archive/site.yaml",
            renamed,
            hashlib.sha256(renamed).hexdigest(),
        )
        assert repository.get_version_source("site-v1") == (
            "archive/site.yaml",
            renamed,
            hashlib.sha256(renamed).hexdigest(),
        )
        assert repository.get_version_source("site-v2", revision_id=2) is None
        assert repository.get_version_source("site-v2", revision_id=3) is not None
    finally:
        repository.close()
