"""Transactional SQLite adapter for immutable configuration revisions."""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import sqlite3
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from configuration_service.application.directory_manifest import DirectoryManifest
from configuration_service.domain.models import (
    CommitResult,
    ConfigIdentity,
    IdempotencyConflictError,
    PreparedDocument,
    PreparedVersion,
    RepositoryCapacityExceededError,
    RepositoryConflictError,
    RepositoryNotFoundError,
    RepositoryVersionExistsError,
    RevisionConflictError,
)
from configuration_service.infrastructure.adapters.outbound.sqlite.schema import (
    initialize_database,
)

_LOG = logging.getLogger(__name__)

_PROJECTION_TABLES = frozenset(
    {
        "unit_definitions",
        "business_points",
        "point_tables",
        "point_mappings",
        "device_models",
        "devices",
        "app_service_configs",
        "config_entries",
    }
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _document_id(identity: ConfigIdentity) -> str:
    return _digest(identity.config_type + "\x00" + identity.config_id)


def _document_payload(document: PreparedDocument) -> dict[str, Any]:
    return {
        "config_type": document.identity.config_type,
        "config_id": document.identity.config_id,
        "schema_ref": document.schema_ref,
        "file_version": document.file_version,
        "content_sha256": document.content_sha256,
        "payload": document.canonical_payload,
    }


class SQLiteRepository:
    """SQLite is the sole authority for repository versions and projections.

    Callers validate business schema and YAML before creating PreparedDocument;
    the adapter guarantees persistence, database constraints, CAS, and replay.
    """

    def __init__(
        self,
        path: Path,
        *,
        application_version: str,
        busy_timeout_ms: int = 5000,
    ) -> None:
        if busy_timeout_ms < 1:
            raise ValueError("busy_timeout_ms must be positive")
        self._path = path
        self._busy_timeout_ms = busy_timeout_ms
        self._application_version = application_version
        self._owner_fd: int | None = None
        self._database_identity: tuple[int, int] | None = None

    def _acquire_owner(self) -> None:
        if self._owner_fd is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self._path.with_name(self._path.name + ".lock")
        flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW
        fd = os.open(lock_path, flags, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(fd)
            raise RuntimeError("configuration database already owned by another process") from None
        self._owner_fd = fd

    def close(self) -> None:
        """Release process ownership after the service has stopped."""
        if self._owner_fd is None:
            return
        fcntl.flock(self._owner_fd, fcntl.LOCK_UN)
        os.close(self._owner_fd)
        self._owner_fd = None
        self._database_identity = None

    def initialize(self) -> None:
        """Initialize or verify the database before serving requests."""
        self._acquire_owner()
        try:
            if self._path.is_symlink():
                raise RuntimeError("database path must not be a symlink")
            previous_umask = os.umask(0o077)
            try:
                with self._connection() as connection:
                    initialize_database(connection, self._application_version)
            finally:
                os.umask(previous_umask)
            if os.name == "posix":
                self._path.chmod(0o600)
            info = self._path.stat()
            self._database_identity = (info.st_dev, info.st_ino)
        except BaseException:
            self.close()
            raise

    def _verify_database_identity(self) -> None:
        """Reject a removed or externally replaced running database file."""
        identity = self._database_identity
        if identity is None:
            return
        try:
            info = self._path.stat()
        except OSError as exc:
            raise RuntimeError("configuration SQLite database file is missing") from exc
        if (info.st_dev, info.st_ino) != identity:
            raise RuntimeError("configuration SQLite database identity has changed")

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        if self._owner_fd is None:
            raise RuntimeError("repository must be initialized before database access")
        self._verify_database_identity()
        connection = sqlite3.connect(
            self._path,
            timeout=self._busy_timeout_ms / 1000,
            isolation_level=None,
        )
        try:
            self._verify_database_identity()
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.row_factory = sqlite3.Row
            yield connection
        finally:
            connection.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """Keep a consistent WAL snapshot for a complete multi-query read."""
        with self._connection() as connection:
            connection.execute("BEGIN")
            try:
                yield connection
            finally:
                connection.rollback()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """A single writer transaction; changes are never partially visible."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    @staticmethod
    def _current_revision(connection: sqlite3.Connection) -> int:
        row = connection.execute("SELECT MAX(revision_id) FROM revisions").fetchone()
        return int(row[0]) if row[0] is not None else 0

    def current_revision(self) -> int:
        with self.read() as connection:
            return self._current_revision(connection)

    def get_version_source(
        self, version_id: str, *, revision_id: int = 0
    ) -> tuple[str, bytes, str] | None:
        """Read original YAML bytes and path pinned to a revision where possible."""
        if not version_id or revision_id < 0:
            raise ValueError("version and nonnegative revision are required")
        with self.read() as connection:
            # Historical reads may resolve a dependency absent from that
            # revision's directory. Never borrow a later publication's source:
            # it could have a different path or bytes for the same version ID.
            if revision_id:
                row = connection.execute(
                    "SELECT relative_path, yaml_bytes, yaml_sha256 "
                    "FROM revision_files WHERE version_id = ? AND revision_id <= ? "
                    "ORDER BY (revision_id = ?) DESC, revision_id DESC, relative_path "
                    "LIMIT 1",
                    (version_id, revision_id, revision_id),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT relative_path, yaml_bytes, yaml_sha256 "
                    "FROM revision_files WHERE version_id = ? "
                    "ORDER BY revision_id DESC, relative_path LIMIT 1",
                    (version_id,),
                ).fetchone()
            if row is None:
                return None
            payload = bytes(row["yaml_bytes"])
            checksum = str(row["yaml_sha256"])
            if hashlib.sha256(payload).hexdigest() != checksum:
                raise RepositoryConflictError("stored historical YAML hash mismatch")
            return str(row["relative_path"]), payload, checksum

    def get_current_manifest(self) -> tuple[int, str, str | None]:
        """Read the authoritative published revision and root Manifest together."""
        with self.read() as connection:
            revision_id = self._current_revision(connection)
            if revision_id == 0:
                return 0, "", None
            row = connection.execute(
                "SELECT m.manifest_sha256, r.created_at "
                "FROM revisions AS r "
                "JOIN revision_manifests AS m USING (revision_id) "
                "WHERE r.revision_id = ?",
                (revision_id,),
            ).fetchone()
            if row is None:
                raise RepositoryConflictError(
                    "current revision has no complete directory manifest"
                )
            return revision_id, str(row["manifest_sha256"]), str(row["created_at"])

    @staticmethod
    def _check_expected(connection: sqlite3.Connection, expected: int) -> None:
        if expected != SQLiteRepository._current_revision(connection):
            raise RevisionConflictError("repository revision changed")

    @staticmethod
    def _load_replay(
        connection: sqlite3.Connection,
        actor_id: str,
        operation: str,
        request_id: str,
        payload_hash: str,
    ) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT payload_sha256, response_json FROM idempotency_requests "
            "WHERE actor_id = ? AND operation = ? AND request_id = ?",
            (actor_id, operation, request_id),
        ).fetchone()
        if row is None:
            return None
        if row["payload_sha256"] != payload_hash:
            raise IdempotencyConflictError("request_id already used for different input")
        result = json.loads(row["response_json"])
        if not isinstance(result, dict):
            raise RuntimeError("corrupted idempotency response")
        return result

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        *,
        actor_id: str,
        operation: str,
        target_id: str,
        revision_id: int | None,
    ) -> None:
        connection.execute(
            "INSERT INTO audit_events VALUES (?, ?, ?, ?, ?, 'SUCCEEDED', ?, ?)",
            (
                uuid.uuid4().hex,
                actor_id,
                operation,
                target_id,
                revision_id,
                _now(),
                "{}",
            ),
        )

    @staticmethod
    def _save_replay(
        connection: sqlite3.Connection,
        actor_id: str,
        operation: str,
        request_id: str,
        payload_hash: str,
        response: Mapping[str, object],
    ) -> None:
        connection.execute(
            "INSERT INTO idempotency_requests VALUES (?, ?, ?, ?, ?, ?)",
            (actor_id, operation, request_id, payload_hash, _canonical_json(response), _now()),
        )

    def register_schema(
        self,
        *,
        schema_ref: str,
        config_type: str,
        document_schema_version: int,
        schema_definition: Mapping[str, Any],
        header_definition: Mapping[str, Any],
    ) -> None:
        """Register a software-controlled immutable document schema."""
        if not schema_ref or not config_type or document_schema_version < 1:
            raise ValueError("schema identity and version are required")
        schema_json = _canonical_json(schema_definition)
        header_json = _canonical_json(header_definition)
        definition_hash = _digest(schema_json + "\x00" + header_json)
        with self.write() as connection:
            row = connection.execute(
                "SELECT config_type, document_schema_version, definition_sha256 "
                "FROM config_schemas WHERE schema_ref = ?",
                (schema_ref,),
            ).fetchone()
            if row is not None:
                if tuple(row) != (config_type, document_schema_version, definition_hash):
                    raise RepositoryConflictError("schema_ref is already registered differently")
                return
            prior = connection.execute(
                "SELECT schema_ref, definition_sha256 FROM config_schemas "
                "WHERE config_type = ? AND document_schema_version = ?",
                (config_type, document_schema_version),
            ).fetchone()
            if prior is not None:
                raise RepositoryConflictError(
                    "document schema version already registered with a different definition"
                )
            connection.execute(
                "INSERT INTO config_schemas VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    schema_ref,
                    config_type,
                    document_schema_version,
                    schema_json,
                    header_json,
                    definition_hash,
                    _now(),
                ),
            )

    def create_candidate(
        self,
        *,
        actor_id: str,
        request_id: str,
        expected_revision: int,
        documents: Sequence[PreparedDocument],
        manifest: DirectoryManifest,
        ttl_seconds: int,
        max_candidates: int,
    ) -> str:
        """Stage the entire validated directory atomically with its raw YAML bytes."""
        if not actor_id or not request_id or not documents or ttl_seconds < 1:
            raise ValueError("actor, request, documents and positive TTL are required")
        if len(documents) != len(manifest.files):
            raise ValueError("candidate and directory manifest differ in length")
        identities = [(doc.identity, doc.file_version) for doc in documents]
        if len(set(identities)) != len(identities):
            raise ValueError("duplicate document identity and file_version")
        payload_hash = _digest(
            _canonical_json(
                {
                    "expected_revision": expected_revision,
                    "manifest_sha256": manifest.sha256,
                }
            )
        )
        with self.write() as connection:
            replay = self._load_replay(
                connection, actor_id, "CreateCandidate", request_id, payload_hash
            )
            if replay is not None:
                return str(replay["candidate_id"])
            self._check_expected(connection, expected_revision)
            active = connection.execute(
                "SELECT COUNT(*) FROM candidates "
                "WHERE status = 'VALID' AND expires_at > ?",
                (_now(),),
            ).fetchone()[0]
            if active >= max_candidates:
                raise RepositoryCapacityExceededError("candidate capacity exhausted")
            candidate_id = uuid.uuid4().hex
            now = datetime.now(UTC)
            connection.execute(
                "INSERT INTO candidates VALUES (?, ?, ?, 'VALID', ?, ?, ?, ?)",
                (
                    candidate_id,
                    actor_id,
                    expected_revision or None,
                    payload_hash,
                    manifest.sha256,
                    now.isoformat(),
                    (now + timedelta(seconds=ttl_seconds)).isoformat(),
                ),
            )
            for file, document in zip(manifest.files, documents, strict=True):
                schema = connection.execute(
                    "SELECT config_type FROM config_schemas WHERE schema_ref = ?",
                    (document.schema_ref,),
                ).fetchone()
                if schema is None or schema[0] != document.identity.config_type:
                    raise RepositoryConflictError("candidate document schema is not registered")
                yaml_sha = hashlib.sha256(file.yaml_bytes).hexdigest()
                connection.execute(
                    "INSERT INTO candidate_documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        candidate_id,
                        file.relative_path,
                        _document_id(document.identity),
                        document.identity.config_type,
                        document.identity.config_id,
                        document.file_version,
                        document.schema_ref,
                        _canonical_json(document.canonical_payload),
                        document.content_sha256,
                        yaml_sha,
                        file.yaml_bytes,
                    ),
                )
            self._audit(
                connection,
                actor_id=actor_id,
                operation="CreateCandidate",
                target_id=candidate_id,
                revision_id=None,
            )
            self._save_replay(
                connection, actor_id, "CreateCandidate", request_id, payload_hash,
                {"candidate_id": candidate_id},
            )
            return candidate_id

    @staticmethod
    def _insert_projection(connection: sqlite3.Connection, version: PreparedVersion) -> None:
        for projection in version.projections:
            if projection.table not in _PROJECTION_TABLES:
                raise ValueError("unsupported projection table")
            columns = {"config_version_id": version.version_id, **projection.columns}
            if (
                projection.columns.get("config_version_id", version.version_id)
                != version.version_id
            ):
                raise ValueError("projection belongs to a different version")
            allowed = {
                row["name"] for row in connection.execute(f"PRAGMA table_info({projection.table})")
            }
            if not set(columns).issubset(allowed):
                raise ValueError("unknown projection column")
            names = ", ".join(columns)
            markers = ", ".join("?" for _ in columns)
            connection.execute(
                f"INSERT INTO {projection.table} ({names}) VALUES ({markers})",
                tuple(columns.values()),
            )

    @staticmethod
    def _check_reference_cycles(connection: sqlite3.Connection, roots: Sequence[str]) -> None:
        """Reject cycles before committing an immutable configuration graph."""
        visiting: set[str] = set()
        visited: set[str] = set()
        graph: dict[str, tuple[str, ...]] = {}
        stack = [(root, False) for root in roots]
        while stack:
            node, complete = stack.pop()
            if complete:
                visiting.remove(node)
                visited.add(node)
                continue
            if node in visited:
                continue
            if node in visiting:
                raise RepositoryConflictError("cyclic configuration version references")
            if len(visited) + len(visiting) >= 100_000:
                raise RepositoryConflictError("reference graph exceeds safe traversal limit")
            visiting.add(node)
            stack.append((node, True))
            if node not in graph:
                rows = connection.execute(
                    "SELECT target_version_id FROM config_references "
                    "WHERE source_version_id = ? ORDER BY target_version_id",
                    (node,),
                )
                graph[node] = tuple(str(row[0]) for row in rows)
            stack.extend((target, False) for target in reversed(graph[node]))

    def publish_candidate(
        self,
        *,
        actor_id: str,
        request_id: str,
        candidate_id: str,
        expected_revision: int,
        versions: Sequence[PreparedVersion],
    ) -> CommitResult:
        """Commit the entire candidate directory as one immutable SQLite revision."""
        if not actor_id or not request_id or not candidate_id or not versions:
            raise ValueError("publication requires actor, request, candidate and versions")
        payload_hash = _digest(
            _canonical_json({"candidate_id": candidate_id, "expected_revision": expected_revision})
        )
        with self.write() as connection:
            connection.execute("PRAGMA defer_foreign_keys = ON")
            replay = self._load_replay(
                connection, actor_id, "PublishCandidate", request_id, payload_hash
            )
            if replay is not None:
                return CommitResult(
                    revision_id=int(replay["revision_id"]),
                    version_ids=tuple(replay["version_ids"]),
                    replayed=True,
                )
            self._check_expected(connection, expected_revision)
            candidate = connection.execute(
                "SELECT owner_id, base_revision_id, status, expires_at, "
                "manifest_sha256, payload_sha256 "
                "FROM candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            if candidate is None:
                raise RepositoryNotFoundError("candidate does not exist")
            if candidate["owner_id"] != actor_id or candidate["status"] != "VALID":
                raise RepositoryConflictError("candidate is unavailable to this actor")
            if datetime.fromisoformat(candidate["expires_at"]) <= datetime.now(UTC):
                raise RepositoryConflictError("candidate expired")
            if (candidate["base_revision_id"] or 0) != expected_revision:
                raise RevisionConflictError("candidate validated against another revision")
            staged = tuple(
                connection.execute(
                    "SELECT * FROM candidate_documents WHERE candidate_id = ? "
                    "ORDER BY relative_path",
                    (candidate_id,),
                )
            )
            prepared = {
                (version.document.identity, version.document.file_version): version
                for version in versions
            }
            if len(staged) != len(versions) or len(prepared) != len(versions):
                raise RepositoryConflictError("prepared versions differ from staged directory")
            ordered: list[tuple[sqlite3.Row, PreparedVersion]] = []
            for item in staged:
                identity = ConfigIdentity(str(item["config_type"]), str(item["config_id"]))
                version = prepared.get((identity, str(item["file_version"])))
                if version is None:
                    raise RepositoryConflictError("staged directory identity differs")
                doc = version.document
                if (
                    item["document_id"] != _document_id(doc.identity)
                    or item["schema_ref"] != doc.schema_ref
                    or item["content_sha256"] != doc.content_sha256
                    or item["canonical_payload_json"] != _canonical_json(doc.canonical_payload)
                    or hashlib.sha256(item["yaml_bytes"]).hexdigest() != item["yaml_sha256"]
                ):
                    raise RepositoryConflictError("prepared content differs from frozen candidate")
                ordered.append((item, version))
            newly_inserted: list[PreparedVersion] = []
            for _item, version in ordered:
                doc = version.document
                document_id = _document_id(doc.identity)
                connection.execute(
                    "INSERT INTO config_documents VALUES (?, ?, ?) "
                    "ON CONFLICT(document_id) DO NOTHING",
                    (document_id, doc.identity.config_type, doc.identity.config_id),
                )
                prior = connection.execute(
                    "SELECT version_id, schema_ref, content_sha256 "
                    "FROM config_versions WHERE document_id = ? AND file_version = ?",
                    (document_id, doc.file_version),
                ).fetchone()
                if prior is None:
                    connection.execute(
                        "INSERT INTO config_versions VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            version.version_id, document_id, doc.schema_ref,
                            doc.file_version, doc.content_sha256, _now(),
                        ),
                    )
                    newly_inserted.append(version)
                elif (
                    str(prior["version_id"]) != version.version_id
                    or str(prior["schema_ref"]) != doc.schema_ref
                    or str(prior["content_sha256"]) != doc.content_sha256
                ):
                    raise RepositoryConflictError(
                        "immutable document file_version redefined with different semantics"
                    )
            for version in newly_inserted:
                self._insert_projection(connection, version)
            fresh_ids = {version.version_id for version in newly_inserted}
            for _item, version in ordered:
                dependencies = set(version.references)
                if version.version_id in fresh_ids:
                    for target_version_id, relation in sorted(dependencies):
                        if connection.execute(
                            "SELECT 1 FROM version_events WHERE version_id = ? "
                            "AND event_type = 'DEPRECATED'",
                            (target_version_id,),
                        ).fetchone():
                            raise RepositoryConflictError("new dependency refers to deprecated version")
                        connection.execute(
                            "INSERT INTO config_references VALUES (?, ?, ?)",
                            (version.version_id, target_version_id, relation),
                        )
                else:
                    original = {
                        (str(row[0]), str(row[1]))
                        for row in connection.execute(
                            "SELECT target_version_id, relation FROM config_references "
                            "WHERE source_version_id = ?",
                            (version.version_id,),
                        )
                    }
                    if original != dependencies:
                        raise RepositoryConflictError(
                            "immutable document dependency closure has changed"
                        )
            self._check_reference_cycles(
                connection, [version.version_id for version in newly_inserted]
            )
            # Full snapshots cannot silently drop a file still needed by a current
            # live configuration. Historical versions remain queryable separately.
            included = {version.version_id for _item, version in ordered}
            for _item, version in ordered:
                if any(target not in included for target, _kind in version.references):
                    raise RepositoryConflictError(
                        "full directory is missing a pinned current dependency"
                    )
            manifest_entries = [
                {
                    "relative_path": str(item["relative_path"]),
                    "yaml_sha256": str(item["yaml_sha256"]),
                }
                for item, _version in ordered
            ]
            digest = _digest(_canonical_json(manifest_entries))
            if digest != candidate["manifest_sha256"]:
                raise RepositoryConflictError(
                    "published directory differs from the validated candidate manifest"
                )
            if _digest(
                _canonical_json(
                    {
                        "expected_revision": expected_revision,
                        "manifest_sha256": digest,
                    }
                )
            ) != candidate["payload_sha256"]:
                raise RepositoryConflictError("candidate payload integrity check failed")
            total_bytes = sum(len(item["yaml_bytes"]) for item, _version in ordered)
            selected: dict[str, PreparedVersion] = {}
            for _item, version in ordered:
                key = _document_id(version.document.identity)
                previous = selected.get(key)
                current_version = tuple(int(i) for i in version.document.file_version.split("."))
                if (
                    previous is None
                    or current_version
                    > tuple(int(i) for i in previous.document.file_version.split("."))
                ):
                    selected[key] = version
            old_manifest = None
            old_selection: dict[str, str] = {}
            if expected_revision:
                old_manifest = connection.execute(
                    "SELECT manifest_sha256 FROM revision_manifests WHERE revision_id = ?",
                    (expected_revision,),
                ).fetchone()
                old_selection = {
                    str(row["document_id"]): str(row["version_id"])
                    for row in connection.execute(
                        "SELECT document_id, version_id FROM revision_members "
                        "WHERE revision_id = ?",
                        (expected_revision,),
                    )
                }
            selected_ids = {key: version.version_id for key, version in selected.items()}
            if (
                old_manifest is not None
                and old_manifest["manifest_sha256"] == digest
                and old_selection == selected_ids
            ):
                revision_id = expected_revision
                unchanged = True
            else:
                connection.execute(
                    "INSERT INTO revisions (parent_revision_id, publisher, request_id, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (expected_revision or None, actor_id, request_id, _now()),
                )
                revision_id = int(
                    connection.execute("SELECT last_insert_rowid()").fetchone()[0]
                )
                for item, version in ordered:
                    connection.execute(
                        "INSERT INTO revision_files VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            revision_id, item["relative_path"], item["document_id"],
                            version.version_id, item["yaml_sha256"], item["yaml_bytes"],
                        ),
                    )
                connection.execute(
                    "INSERT INTO revision_manifests VALUES (?, ?, ?, ?)",
                    (revision_id, digest, total_bytes, len(ordered)),
                )
                for document_id, version in selected.items():
                    connection.execute(
                        "INSERT INTO revision_members VALUES (?, ?, ?)",
                        (revision_id, document_id, version.version_id),
                    )
                unchanged = False
            connection.execute(
                "UPDATE candidates SET status = 'PUBLISHED' WHERE candidate_id = ?",
                (candidate_id,),
            )
            version_ids = tuple(version.version_id for _item, version in ordered)
            self._audit(
                connection, actor_id=actor_id, operation="PublishCandidate",
                target_id=candidate_id, revision_id=revision_id,
            )
            self._save_replay(
                connection, actor_id, "PublishCandidate", request_id, payload_hash,
                {"revision_id": revision_id, "version_ids": version_ids},
            )
            return CommitResult(
                revision_id=revision_id,
                version_ids=version_ids,
                replayed=unchanged,
            )

    def get_revision_timestamp(self, revision_id: int) -> str:
        """Return the immutable publication timestamp, including idempotent replays."""
        with self.read() as connection:
            row = connection.execute(
                "SELECT created_at FROM revisions WHERE revision_id = ?",
                (revision_id,),
            ).fetchone()
            if row is None:
                raise RepositoryNotFoundError("revision does not exist")
            return str(row[0])

    def get_revision_members(self, revision_id: int) -> dict[ConfigIdentity, str]:
        """Read exactly the recommended versions at one historic revision."""
        if revision_id < 1:
            raise ValueError("revision_id must reference a committed revision")
        with self.read() as connection:
            exists = connection.execute(
                "SELECT 1 FROM revisions WHERE revision_id = ?", (revision_id,)
            ).fetchone()
            if exists is None:
                raise RepositoryNotFoundError("revision does not exist")
            rows = connection.execute(
                "SELECT d.config_type, d.config_id, m.version_id "
                "FROM revision_members AS m "
                "JOIN config_documents AS d ON d.document_id = m.document_id "
                "WHERE m.revision_id = ? ORDER BY d.config_type, d.config_id",
                (revision_id,),
            )
            return {
                ConfigIdentity(row["config_type"], row["config_id"]): row["version_id"]
                for row in rows
            }

    def get_version(
        self,
        identity: ConfigIdentity,
        *,
        version_id: str | None = None,
        revision_id: int | None = None,
    ) -> dict[str, Any]:
        """Resolve a pinned version or the member of a particular revision."""
        if (version_id is None) == (revision_id is None):
            raise ValueError("specify exactly one version selector")
        with self.read() as connection:
            if version_id is not None:
                row = connection.execute(
                    "SELECT v.*, d.config_type, d.config_id FROM config_versions AS v "
                    "JOIN config_documents AS d ON d.document_id = v.document_id "
                    "WHERE d.config_type = ? AND d.config_id = ? AND v.version_id = ?",
                    (identity.config_type, identity.config_id, version_id),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT v.*, d.config_type, d.config_id FROM revision_members AS m "
                    "JOIN config_versions AS v ON v.version_id = m.version_id "
                    "JOIN config_documents AS d ON d.document_id = m.document_id "
                    "WHERE m.revision_id = ? AND d.config_type = ? AND d.config_id = ?",
                    (revision_id, identity.config_type, identity.config_id),
                ).fetchone()
            if row is None:
                raise RepositoryNotFoundError("configuration version not found")
            return dict(row)

    def resolve_version(self, identity: ConfigIdentity, file_version: str) -> str:
        """Resolve an explicit immutable file_version, never the moving latest."""
        with self.read() as connection:
            row = connection.execute(
                "SELECT v.version_id FROM config_versions AS v "
                "JOIN config_documents AS d ON d.document_id = v.document_id "
                "WHERE d.config_type = ? AND d.config_id = ? "
                "AND v.file_version = ?",
                (identity.config_type, identity.config_id, file_version),
            ).fetchone()
            if row is None:
                raise RepositoryNotFoundError("referenced configuration version not found")
            return str(row["version_id"])

    def get_version_identity(self, version_id: str) -> ConfigIdentity:
        """Resolve the stable document identity for a historical version."""
        with self.read() as connection:
            row = connection.execute(
                "SELECT d.config_type, d.config_id FROM config_versions AS v "
                "JOIN config_documents AS d ON d.document_id = v.document_id "
                "WHERE v.version_id = ?",
                (version_id,),
            ).fetchone()
            if row is None:
                raise RepositoryNotFoundError("configuration version not found")
            return ConfigIdentity(row["config_type"], row["config_id"])

    def get_projection_rows(self, version_id: str, table: str) -> tuple[dict[str, Any], ...]:
        """Read one typed projection using a fixed allowlist of tables."""
        if table not in _PROJECTION_TABLES:
            raise ValueError("unsupported projection table")
        with self.read() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM config_versions WHERE version_id = ?", (version_id,)
                ).fetchone()
                is None
            ):
                raise RepositoryNotFoundError("version does not exist")
            rows = connection.execute(
                f"SELECT * FROM {table} WHERE config_version_id = ? ORDER BY rowid",
                (version_id,),
            )
            return tuple(dict(row) for row in rows)

    def get_reference_closure(
        self,
        identity: ConfigIdentity,
        revision_id: int,
        *,
        max_versions: int = 1000,
    ) -> tuple[dict[str, Any], ...]:
        """Resolve a single root and its pinned historical dependencies."""
        return self.get_bundle_versions((identity,), revision_id, max_versions=max_versions)

    @staticmethod
    def _load_version_closure(
        connection: sqlite3.Connection,
        pending: list[str],
        *,
        max_versions: int,
    ) -> tuple[dict[str, Any], ...]:
        """Resolve pinned dependency versions in deterministic topological order."""
        import heapq

        records: dict[str, dict[str, Any]] = {}
        dependencies: dict[str, set[str]] = {}
        while pending:
            version_id = pending.pop()
            if version_id in records:
                continue
            if len(records) >= max_versions:
                raise RepositoryCapacityExceededError(
                    "configuration dependency closure exceeds limit"
                )
            row = connection.execute(
                "SELECT v.*, d.config_type, d.config_id "
                "FROM config_versions AS v "
                "JOIN config_documents AS d ON d.document_id = v.document_id "
                "WHERE v.version_id = ?",
                (version_id,),
            ).fetchone()
            if row is None:
                raise RepositoryNotFoundError("historical configuration reference is broken")
            records[version_id] = dict(row)
            targets = {
                str(item[0])
                for item in connection.execute(
                    "SELECT target_version_id FROM config_references "
                    "WHERE source_version_id = ?",
                    (version_id,),
                )
            }
            dependencies[version_id] = targets
            pending.extend(sorted(targets))
        counts = {version: len(targets) for version, targets in dependencies.items()}
        dependents: dict[str, set[str]] = {version: set() for version in records}
        for source, targets in dependencies.items():
            for target in targets:
                dependents[target].add(source)
        ready = [version for version, count in counts.items() if count == 0]
        heapq.heapify(ready)
        ordered: list[dict[str, Any]] = []
        while ready:
            version = heapq.heappop(ready)
            ordered.append(records[version])
            for dependent in sorted(dependents[version]):
                counts[dependent] -= 1
                if counts[dependent] == 0:
                    heapq.heappush(ready, dependent)
        if len(ordered) != len(records):
            raise RepositoryConflictError("cyclic configuration version references")
        return tuple(ordered)

    def get_point_table_versions(
        self,
        point_table_id: str,
        *,
        max_versions: int,
    ) -> tuple[dict[str, Any], ...]:
        """Read historical PointTable closure by immutable ID, ignoring current recommendations."""
        import re

        match = re.fullmatch(
            r"point_table_(?P<template>[A-Za-z][A-Za-z0-9_.-]*)_v"
            r"(?P<version>[0-9]+\.[0-9]+\.[0-9]+)",
            point_table_id,
        )
        if match is None or max_versions < 1:
            raise ValueError("invalid point_table_id or closure limit")
        with self.read() as connection:
            root = connection.execute(
                "SELECT v.version_id FROM config_versions AS v "
                "JOIN config_documents AS d ON d.document_id = v.document_id "
                "WHERE d.config_type = 'point_table' AND d.config_id = ? "
                "AND v.file_version = ?",
                (match["template"], match["version"]),
            ).fetchone()
            if root is None:
                raise RepositoryNotFoundError("immutable PointTable not found")
            return self._load_version_closure(
                connection, [str(root["version_id"])], max_versions=max_versions
            )

    def get_bundle_versions(
        self,
        roots: Sequence[ConfigIdentity],
        revision_id: int,
        *,
        max_versions: int,
    ) -> tuple[dict[str, Any], ...]:
        """Resolve pinned dependencies and topologically order one SQLite snapshot."""
        if revision_id < 1 or max_versions < 1 or not roots:
            raise ValueError("revision, roots and max_versions must be positive")
        with self.read() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM revisions WHERE revision_id = ?", (revision_id,)
                ).fetchone()
                is None
            ):
                raise RepositoryNotFoundError("revision does not exist")
            pending: list[str] = []
            for identity in sorted(set(roots), key=lambda item: (item.config_type, item.config_id)):
                row = connection.execute(
                    "SELECT m.version_id FROM revision_members AS m "
                    "JOIN config_documents AS d ON d.document_id = m.document_id "
                    "WHERE m.revision_id = ? AND d.config_type = ? AND d.config_id = ?",
                    (revision_id, identity.config_type, identity.config_id),
                ).fetchone()
                if row is None:
                    raise RepositoryNotFoundError("root configuration not found at revision")
                pending.append(str(row["version_id"]))
            return self._load_version_closure(
                connection, pending, max_versions=max_versions
            )

    def get_schema_by_type(self, config_type: str, document_schema_version: int) -> dict[str, Any]:
        """Look up the exact registered schema version without exposing SQL."""
        with self.read() as connection:
            row = connection.execute(
                "SELECT * FROM config_schemas WHERE config_type = ? "
                "AND document_schema_version = ?",
                (config_type, document_schema_version),
            ).fetchone()
            if row is None:
                raise RepositoryConflictError("unregistered document schema")
            return dict(row)

    def get_document_schema(self, schema_ref: str) -> dict[str, Any]:
        """Load an immutable parser and Header definition by exact schema identity."""
        with self.read() as connection:
            row = connection.execute(
                "SELECT * FROM config_schemas WHERE schema_ref = ?", (schema_ref,)
            ).fetchone()
            if row is None:
                raise RepositoryNotFoundError("document schema does not exist")
            return dict(row)

    def get_candidate(self, candidate_id: str, actor_id: str) -> dict[str, Any]:
        """Return staged typed documents only to their owning authenticated actor."""
        if not candidate_id or not actor_id:
            raise ValueError("candidate identity and actor are required")
        with self.read() as connection:
            row = connection.execute(
                "SELECT * FROM candidates WHERE candidate_id = ? AND owner_id = ?",
                (candidate_id, actor_id),
            ).fetchone()
            if row is None:
                raise RepositoryNotFoundError("candidate does not exist for this actor")
            documents = connection.execute(
                "SELECT * FROM candidate_documents WHERE candidate_id = ? ORDER BY relative_path",
                (candidate_id,),
            ).fetchall()
            return {
                **dict(row),
                "documents": tuple(dict(item) for item in documents),
            }

    def expire_candidates(self) -> int:
        """Mark elapsed, uncommitted candidates without changing frozen contents."""
        with self.write() as connection:
            cursor = connection.execute(
                "UPDATE candidates SET status = 'EXPIRED' "
                "WHERE status = 'VALID' AND expires_at <= ?",
                (_now(),),
            )
            return cursor.rowcount

    @staticmethod
    def _new_revision(
        connection: sqlite3.Connection,
        *,
        expected_revision: int,
        actor_id: str,
        request_id: str,
        document_id: str | None = None,
        version_id: str | None = None,
    ) -> int:
        """Append a complete recommended-version snapshot without updating history."""
        connection.execute(
            "INSERT INTO revisions (parent_revision_id, publisher, request_id, created_at) "
            "VALUES (?, ?, ?, ?)",
            (expected_revision or None, actor_id, request_id, _now()),
        )
        revision_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        if document_id is not None:
            if version_id is None:
                raise ValueError("replacement version is required")
            connection.execute(
                "INSERT INTO revision_members VALUES (?, ?, ?)",
                (revision_id, document_id, version_id),
            )
        if expected_revision:
            connection.execute(
                "INSERT INTO revision_members (revision_id, document_id, version_id) "
                "SELECT ?, old.document_id, old.version_id FROM revision_members AS old "
                "WHERE old.revision_id = ? AND (? IS NULL OR old.document_id != ?)",
                (revision_id, expected_revision, document_id, document_id),
            )
            connection.execute(
                "INSERT INTO revision_files "
                "(revision_id, relative_path, document_id, version_id, yaml_sha256, yaml_bytes) "
                "SELECT ?, relative_path, document_id, version_id, yaml_sha256, yaml_bytes "
                "FROM revision_files WHERE revision_id = ?",
                (revision_id, expected_revision),
            )
            connection.execute(
                "INSERT INTO revision_manifests "
                "(revision_id, manifest_sha256, total_bytes, file_count) "
                "SELECT ?, manifest_sha256, total_bytes, file_count "
                "FROM revision_manifests WHERE revision_id = ?",
                (revision_id, expected_revision),
            )
        return revision_id

    def set_recommended_version(
        self,
        *,
        actor_id: str,
        request_id: str,
        identity: ConfigIdentity,
        version_id: str,
        expected_revision: int,
    ) -> tuple[int, str | None]:
        """Move a document pointer by creating an immutable full revision."""
        if not actor_id or not request_id or not version_id or expected_revision < 0:
            raise ValueError("actor, request, version and valid revision are required")
        payload_hash = _digest(
            _canonical_json(
                {
                    "identity": [identity.config_type, identity.config_id],
                    "version_id": version_id,
                    "expected_revision": expected_revision,
                }
            )
        )
        with self.write() as connection:
            replay = self._load_replay(
                connection, actor_id, "SetRecommendedVersion", request_id, payload_hash
            )
            if replay is not None:
                previous = replay.get("previous_version_id")
                return int(replay["revision_id"]), str(previous) if previous is not None else None
            self._check_expected(connection, expected_revision)
            record = connection.execute(
                "SELECT v.document_id FROM config_versions AS v "
                "JOIN config_documents AS d ON d.document_id = v.document_id "
                "WHERE d.config_type = ? AND d.config_id = ? AND v.version_id = ?",
                (identity.config_type, identity.config_id, version_id),
            ).fetchone()
            if record is None:
                raise RepositoryNotFoundError("requested document version does not exist")
            if connection.execute(
                "SELECT 1 FROM version_events WHERE version_id = ? AND event_type = 'DEPRECATED'",
                (version_id,),
            ).fetchone():
                raise RepositoryConflictError("cannot recommend a deprecated version")
            if not connection.execute(
                "SELECT 1 FROM revision_files "
                "WHERE revision_id = ? AND document_id = ? AND version_id = ?",
                (expected_revision, str(record["document_id"]), version_id),
            ).fetchone():
                raise RepositoryConflictError(
                    "recommended version must be present in the current complete directory"
                )
            document_id = str(record["document_id"])
            if connection.execute(
                "SELECT 1 FROM config_references AS r "
                "JOIN version_events AS e ON e.version_id = r.target_version_id "
                "AND e.event_type = 'DEPRECATED' WHERE r.source_version_id = ?",
                (version_id,),
            ).fetchone():
                raise RepositoryConflictError("version references a deprecated dependency")
            old = connection.execute(
                "SELECT version_id FROM revision_members "
                "WHERE revision_id = ? AND document_id = ?",
                (expected_revision, document_id),
            ).fetchone()
            previous_id = str(old["version_id"]) if old is not None else None
            if previous_id == version_id:
                raise RepositoryConflictError("version is already recommended")
            revision_id = self._new_revision(
                connection,
                expected_revision=expected_revision,
                actor_id=actor_id,
                request_id=request_id,
                document_id=document_id,
                version_id=version_id,
            )
            response = {"revision_id": revision_id, "previous_version_id": previous_id}
            self._audit(
                connection,
                actor_id=actor_id,
                operation="SetRecommendedVersion",
                target_id=version_id,
                revision_id=revision_id,
            )
            self._save_replay(
                connection,
                actor_id,
                "SetRecommendedVersion",
                request_id,
                payload_hash,
                response,
            )
            return revision_id, previous_id

    def deprecate_version(
        self,
        *,
        actor_id: str,
        request_id: str,
        version_id: str,
        reason: str,
        expected_revision: int,
    ) -> tuple[int, int]:
        """Append a version deprecation event while preserving historical reads."""
        if not actor_id or not request_id or not version_id or not reason.strip():
            raise ValueError("actor, request, version and reason are required")
        if expected_revision < 0:
            raise ValueError("expected_revision must not be negative")
        payload_hash = _digest(
            _canonical_json(
                {
                    "version_id": version_id,
                    "reason": reason,
                    "expected_revision": expected_revision,
                }
            )
        )
        with self.write() as connection:
            replay = self._load_replay(
                connection, actor_id, "DeprecateConfigVersion", request_id, payload_hash
            )
            if replay is not None:
                return int(replay["revision_id"]), int(replay["referencing_versions_count"])
            self._check_expected(connection, expected_revision)
            if (
                connection.execute(
                    "SELECT 1 FROM config_versions WHERE version_id = ?", (version_id,)
                ).fetchone()
                is None
            ):
                raise RepositoryNotFoundError("version does not exist")
            if connection.execute(
                "SELECT 1 FROM version_events "
                "WHERE version_id = ? AND event_type = 'DEPRECATED'",
                (version_id,),
            ).fetchone():
                raise RepositoryConflictError("version is already deprecated")
            if connection.execute(
                "SELECT 1 FROM revision_members " "WHERE revision_id = ? AND version_id = ?",
                (expected_revision, version_id),
            ).fetchone():
                raise RepositoryConflictError("cannot deprecate the current recommended version")
            # The current snapshot may publish more than one immutable version.
            # Non-recommended versions still form part of the active root and
            # must not be deprecated while physically present in that root.
            if connection.execute(
                "SELECT 1 FROM revision_files WHERE revision_id = ? AND version_id = ?",
                (expected_revision, version_id),
            ).fetchone():
                raise RepositoryConflictError(
                    "cannot deprecate a version present in the current directory"
                )
            refs = connection.execute(
                "SELECT COUNT(*) FROM config_references WHERE target_version_id = ?",
                (version_id,),
            ).fetchone()
            reference_count = int(refs[0])
            revision_id = self._new_revision(
                connection,
                expected_revision=expected_revision,
                actor_id=actor_id,
                request_id=request_id,
            )
            connection.execute(
                "INSERT INTO version_events VALUES (?, ?, ?, 'DEPRECATED', ?, ?, ?)",
                (uuid.uuid4().hex, version_id, revision_id, reason, actor_id, _now()),
            )
            self._audit(
                connection,
                actor_id=actor_id,
                operation="DeprecateConfigVersion",
                target_id=version_id,
                revision_id=revision_id,
            )
            self._save_replay(
                connection,
                actor_id,
                "DeprecateConfigVersion",
                request_id,
                payload_hash,
                {
                    "revision_id": revision_id,
                    "referencing_versions_count": reference_count,
                },
            )
            return revision_id, reference_count

    def list_configs(
        self,
        *,
        revision_id: int,
        page_size: int,
        after: ConfigIdentity | None = None,
        config_type: str = "",
        config_id_prefix: str = "",
        module_type: str = "",
    ) -> tuple[dict[str, Any], ...]:
        """Page through one immutable revision, in stable identity order."""
        if revision_id < 1 or not 1 <= page_size <= 1001:
            raise ValueError("invalid revision or page size")
        with self.read() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM revisions WHERE revision_id = ?", (revision_id,)
                ).fetchone()
                is None
            ):
                raise RepositoryNotFoundError("revision does not exist")
            rows = connection.execute(
                "SELECT d.config_type, d.config_id, v.version_id, v.file_version, "
                "v.schema_ref, v.content_sha256, "
                "EXISTS(SELECT 1 FROM version_events AS e "
                "WHERE e.version_id = v.version_id AND e.event_type = 'DEPRECATED' "
                "AND e.revision_id <= ?) AS deprecated "
                "FROM revision_members AS m "
                "JOIN config_documents AS d ON d.document_id = m.document_id "
                "JOIN config_versions AS v ON v.version_id = m.version_id "
                "WHERE m.revision_id = ? "
                "AND (? = '' OR d.config_type = ?) "
                "AND substr(d.config_id, 1, length(?)) = ? "
                "AND (? = '' OR EXISTS ("
                "SELECT 1 FROM app_service_configs AS svc "
                "WHERE svc.config_version_id = v.version_id "
                "AND svc.module_type = ?)) "
                "AND (d.config_type > ? OR "
                "(d.config_type = ? AND d.config_id > ?)) "
                "ORDER BY d.config_type, d.config_id LIMIT ?",
                (
                    revision_id,
                    revision_id,
                    config_type,
                    config_type,
                    config_id_prefix,
                    config_id_prefix,
                    module_type,
                    module_type,
                    after.config_type if after else "",
                    after.config_type if after else "",
                    after.config_id if after else "",
                    page_size,
                ),
            )
            return tuple(dict(row) for row in rows)

    def version_snapshot(self, identity: ConfigIdentity) -> tuple[int, int]:
        """Capture the version cursor and deprecation revision in one read snapshot."""
        with self.read() as connection:
            row = connection.execute(
                "SELECT MAX(v.rowid) FROM config_versions AS v "
                "JOIN config_documents AS d ON d.document_id = v.document_id "
                "WHERE d.config_type = ? AND d.config_id = ?",
                (identity.config_type, identity.config_id),
            ).fetchone()
            if row[0] is None:
                raise RepositoryNotFoundError("document has no versions")
            return int(row[0]), self._current_revision(connection)

    def list_config_versions(
        self,
        identity: ConfigIdentity,
        *,
        watermark: int,
        snapshot_revision: int,
        page_size: int,
        after_rowid: int = 0,
        include_deprecated: bool = False,
    ) -> tuple[dict[str, Any], ...]:
        """Page through versions pinned to insertion and deprecation watermarks."""
        if watermark < 1 or snapshot_revision < 0 or after_rowid < 0 or not 1 <= page_size <= 1000:
            raise ValueError("invalid version page parameters")
        with self.read() as connection:
            rows = connection.execute(
                "SELECT v.rowid AS cursor, v.version_id, v.file_version, "
                "v.schema_ref, v.content_sha256, v.created_at, "
                "EXISTS(SELECT 1 FROM version_events AS e "
                "WHERE e.version_id = v.version_id AND e.event_type = 'DEPRECATED' "
                "AND e.revision_id <= ?) AS deprecated, "
                "(SELECT COUNT(*) FROM config_references AS r "
                "WHERE r.target_version_id = v.version_id) AS referenced_by_count "
                "FROM config_versions AS v "
                "JOIN config_documents AS d ON d.document_id = v.document_id "
                "WHERE d.config_type = ? AND d.config_id = ? AND v.rowid > ? "
                "AND v.rowid <= ? AND (? OR NOT EXISTS("
                "SELECT 1 FROM version_events AS e WHERE e.version_id = v.version_id "
                "AND e.event_type = 'DEPRECATED' AND e.revision_id <= ?)) "
                "ORDER BY v.rowid LIMIT ?",
                (
                    snapshot_revision,
                    identity.config_type,
                    identity.config_id,
                    after_rowid,
                    watermark,
                    int(include_deprecated),
                    snapshot_revision,
                    page_size,
                ),
            )
            return tuple(dict(row) for row in rows)

    def list_references(
        self,
        *,
        version_id: str,
        inbound: bool,
        page_size: int,
        after: tuple[str, str, str] | None = None,
    ) -> tuple[dict[str, Any], ...]:
        """Return fixed version-reference edges with deterministic ordering."""
        if not version_id or not 1 <= page_size <= 1001:
            raise ValueError("version_id and bounded page_size are required")
        with self.read() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM config_versions WHERE version_id = ?", (version_id,)
                ).fetchone()
                is None
            ):
                raise RepositoryNotFoundError("reference version does not exist")
            side = "target_version_id" if inbound else "source_version_id"
            rows = connection.execute(
                "SELECT source_version_id, target_version_id, relation "
                f"FROM config_references WHERE {side} = ? "
                "AND (source_version_id, target_version_id, relation) > (?, ?, ?) "
                "ORDER BY source_version_id, target_version_id, relation LIMIT ?",
                (version_id, *(after or ("", "", "")), page_size),
            )
            return tuple(dict(row) for row in rows)

    def get_stats(self, *, config_type: str = "") -> dict[str, int]:
        """Return counts from one committed snapshot, optionally by configuration type."""
        with self.read() as connection:
            counts: dict[str, int] = {}
            if config_type:
                counts["document_count"] = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM config_documents WHERE config_type = ?",
                        (config_type,),
                    ).fetchone()[0]
                )
                counts["version_count"] = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM config_versions AS v "
                        "JOIN config_documents AS d ON d.document_id = v.document_id "
                        "WHERE d.config_type = ?",
                        (config_type,),
                    ).fetchone()[0]
                )
                counts["reference_count"] = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM config_references AS r "
                        "JOIN config_versions AS v ON v.version_id = r.source_version_id "
                        "JOIN config_documents AS d ON d.document_id = v.document_id "
                        "WHERE d.config_type = ?",
                        (config_type,),
                    ).fetchone()[0]
                )
            else:
                for key, table in (
                    ("document_count", "config_documents"),
                    ("version_count", "config_versions"),
                    ("reference_count", "config_references"),
                ):
                    counts[key] = int(
                        connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    )
            counts["revision_count"] = int(
                connection.execute("SELECT COUNT(*) FROM revisions").fetchone()[0]
            )
            row = connection.execute("SELECT schema_version FROM db_meta WHERE id = 1")
            meta = row.fetchone()
            if meta is None:
                raise RuntimeError("database metadata is missing")
            counts["db_schema_version"] = int(meta[0])
            return counts

    def recover_interrupted_backups(self) -> int:
        """Never report in-flight jobs as successful after process restart."""
        with self.write() as connection:
            row = connection.execute(
                "UPDATE backup_tasks SET state = 'INTERRUPTED', finished_at = ?, "
                "error = 'service restarted before backup completion' "
                "WHERE state IN ('QUEUED', 'RUNNING')",
                (_now(),),
            )
            return row.rowcount

    def create_backup_task(
        self,
        *,
        actor_id: str,
        request_id: str,
        label: str,
        max_active_jobs: int,
    ) -> str:
        """Persist a backup intent with a durable caller-scoped idempotency key."""
        if not actor_id or not request_id:
            raise ValueError("actor_id and request_id are required")
        payload_hash = _digest(_canonical_json({"label": label}))
        with self.write() as connection:
            replay = self._load_replay(
                connection, actor_id, "CreateBackup", request_id, payload_hash
            )
            if replay is not None:
                return str(replay["task_id"])
            active = connection.execute(
                "SELECT COUNT(*) FROM backup_tasks " "WHERE state IN ('QUEUED', 'RUNNING')"
            ).fetchone()[0]
            if active >= max_active_jobs:
                raise RepositoryCapacityExceededError("backup queue capacity exhausted")
            task_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO backup_tasks "
                "(task_id, actor_id, request_id, state, created_at) "
                "VALUES (?, ?, ?, 'QUEUED', ?)",
                (task_id, actor_id, request_id, _now()),
            )
            self._audit(
                connection,
                actor_id=actor_id,
                operation="CreateBackup",
                target_id=task_id,
                revision_id=None,
            )
            self._save_replay(
                connection,
                actor_id,
                "CreateBackup",
                request_id,
                payload_hash,
                {"task_id": task_id},
            )
            return task_id

    def start_backup_task(self, task_id: str) -> None:
        """Transition a queued task once, before filesystem side effects."""
        with self.write() as connection:
            updated = connection.execute(
                "UPDATE backup_tasks SET state = 'RUNNING' "
                "WHERE task_id = ? AND state = 'QUEUED'",
                (task_id,),
            ).rowcount
            if updated != 1:
                raise RepositoryConflictError("backup task is not queued")

    def finish_backup_task(
        self,
        task_id: str,
        *,
        path: str | None,
        size_bytes: int | None,
        sha256: str | None,
        error: str | None,
        database_schema_version: int | None = None,
        integrity_verified: bool = False,
    ) -> None:
        """Persist a terminal state, without claiming success on errors."""
        success = error is None and path is not None and sha256 is not None
        if success and (database_schema_version is None or not integrity_verified):
            raise ValueError("successful backup requires verified schema metadata")
        if success and (size_bytes is None or size_bytes < 1):
            raise ValueError("successful backup requires positive file size")
        state = "SUCCEEDED" if success else "FAILED"
        with self.write() as connection:
            updated = connection.execute(
                "UPDATE backup_tasks SET state = ?, path = ?, size_bytes = ?, "
                "sha256 = ?, error = ?, finished_at = ?, "
                "database_schema_version = ?, integrity_verified = ? "
                "WHERE task_id = ? AND state = 'RUNNING'",
                (
                    state,
                    path,
                    size_bytes,
                    sha256,
                    error,
                    _now(),
                    database_schema_version,
                    int(integrity_verified),
                    task_id,
                ),
            ).rowcount
            if updated != 1:
                raise RepositoryConflictError("backup task is not running")

    def get_backup_task(self, task_id: str) -> dict[str, Any]:
        """Read durable backup status by task identity."""
        with self.read() as connection:
            row = connection.execute(
                "SELECT * FROM backup_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise RepositoryNotFoundError("backup task does not exist")
            return dict(row)

    def backup_to(self, path: Path) -> None:
        """Copy a consistent WAL snapshot into an exclusively created local file."""
        if not path.is_absolute() or path.exists() or path.is_symlink():
            raise ValueError("backup target must be a new absolute path")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(path, flags, 0o600)
        os.close(fd)
        try:
            with self._connection() as source:
                destination = sqlite3.connect(path)
                try:
                    source.backup(destination, pages=256, sleep=0.05)
                finally:
                    destination.close()
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    def list_audit_events(
        self,
        *,
        after: tuple[str, str] | None = None,
        from_time: str | None = None,
        to_time: str | None = None,
        operation_filter: str = "",
        page_size: int = 100,
    ) -> tuple[dict[str, Any], ...]:
        """Read durable repository events in stable timestamp/identity order."""
        if not 1 <= page_size <= 1000:
            raise ValueError("page_size out of range")
        with self.read() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_events WHERE "
                "(? IS NULL OR created_at >= ?) AND (? IS NULL OR created_at <= ?) "
                "AND (? = '' OR operation = ?) "
                "AND (created_at, event_id) > (?, ?) "
                "ORDER BY created_at, event_id LIMIT ?",
                (
                    from_time,
                    from_time,
                    to_time,
                    to_time,
                    operation_filter,
                    operation_filter,
                    *(after or ("", "")),
                    page_size,
                ),
            )
            return tuple(dict(row) for row in rows)

    def storage_stats(self) -> dict[str, int | None]:
        """Return actual filesystem counters or explicit unavailable metrics."""
        import shutil

        result: dict[str, int | None] = {
            "db_file_bytes": None,
            "wal_file_bytes": None,
            "free_disk_bytes": None,
        }
        try:
            result["db_file_bytes"] = self._path.stat().st_size
        except OSError:
            _LOG.debug("configuration storage usage metric unavailable", exc_info=True)
        try:
            result["wal_file_bytes"] = self._path.with_name(self._path.name + "-wal").stat().st_size
        except FileNotFoundError:
            result["wal_file_bytes"] = 0
        except OSError:
            _LOG.debug("configuration storage usage metric unavailable", exc_info=True)
        try:
            result["free_disk_bytes"] = shutil.disk_usage(self._path.parent).free
        except OSError:
            _LOG.debug("configuration storage usage metric unavailable", exc_info=True)
        return result

    def record_rejected_operation(self, *, actor_id: str, operation: str, error_code: str) -> None:
        """Audit authenticated rejects without storing request payloads or secrets."""
        with self.write() as connection:
            connection.execute(
                "INSERT INTO audit_events VALUES (?, ?, ?, ?, NULL, ?, ?, ?)",
                (
                    uuid.uuid4().hex,
                    actor_id,
                    operation,
                    "configuration-repository",
                    error_code,
                    _now(),
                    "{}",
                ),
            )

    def operational_metrics(self) -> dict[str, int]:
        """Read durable counters from one consistent SQLite snapshot."""
        with self.read() as connection:
            metrics: dict[str, int] = {}
            for name, query in (
                (
                    "candidate_active",
                    "SELECT COUNT(*) FROM candidates " "WHERE status = 'VALID' AND expires_at > ?",
                ),
                ("backup_queued", "SELECT COUNT(*) FROM backup_tasks WHERE state = 'QUEUED'"),
                ("backup_running", "SELECT COUNT(*) FROM backup_tasks WHERE state = 'RUNNING'"),
                ("backup_succeeded", "SELECT COUNT(*) FROM backup_tasks WHERE state = 'SUCCEEDED'"),
                ("backup_failed", "SELECT COUNT(*) FROM backup_tasks WHERE state = 'FAILED'"),
                (
                    "backup_interrupted",
                    "SELECT COUNT(*) FROM backup_tasks " "WHERE state = 'INTERRUPTED'",
                ),
                (
                    "requests_succeeded",
                    "SELECT COUNT(*) FROM audit_events " "WHERE outcome = 'SUCCEEDED'",
                ),
                (
                    "requests_failed",
                    "SELECT COUNT(*) FROM audit_events " "WHERE outcome != 'SUCCEEDED'",
                ),
            ):
                params = (_now(),) if name == "candidate_active" else ()
                metrics[name] = int(connection.execute(query, params).fetchone()[0])
            return metrics

    def diagnose_storage(self) -> dict[str, tuple[bool, str]]:
        """Check SQLite consistency, FK relationships, WAL and storage capacity."""
        findings: dict[str, tuple[bool, str]] = {}
        with self.read() as connection:
            consistent = connection.execute("PRAGMA quick_check").fetchone()
            findings["sqlite"] = (
                consistent is not None and consistent[0] == "ok",
                "quick_check",
            )
            foreign_key = connection.execute("PRAGMA foreign_key_check").fetchone()
            findings["references"] = (foreign_key is None, "foreign_key_check")
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
            findings["wal"] = (
                journal_mode is not None and journal_mode[0].lower() == "wal",
                "journal_mode",
            )
        stats = self.storage_stats()
        disk_space = stats["free_disk_bytes"]
        findings["storage"] = (
            disk_space is not None and disk_space > 0,
            "free_disk_bytes",
        )
        return findings
