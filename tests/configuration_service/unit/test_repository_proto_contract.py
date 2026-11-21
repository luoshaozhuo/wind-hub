"""Verify generated Configuration RPC descriptors match the source contract."""

from __future__ import annotations

from contracts import configuration_repository_pb2 as pb


def test_full_directory_document_path_is_in_wire_contract() -> None:
    fields = pb.ConfigDocumentInput.DESCRIPTOR.fields_by_name
    assert fields["relative_path"].number == 4
    assert fields["yaml_bytes"].number == 3
    response = pb.CreateCandidateResponse.DESCRIPTOR.fields_by_name
    assert response["manifest_sha256"].number == 6
    output = pb.ConfigYamlDocument.DESCRIPTOR.fields_by_name
    assert output["relative_path"].number == 11


def test_revision_reconciliation_and_point_table_query_descriptors() -> None:
    service = pb.DESCRIPTOR.services_by_name["ConfigurationRepositoryService"]
    assert "GetCurrentRevision" in service.methods_by_name
    assert "GetPointTableBundle" in service.methods_by_name
    assert (
        pb.GetCurrentRevisionResponse.DESCRIPTOR.fields_by_name[
            "committed_at"
        ].message_type.full_name
        == "google.protobuf.Timestamp"
    )
    assert (
        pb.GetPointTableBundleResponse.DESCRIPTOR.fields_by_name["documents"].message_type.full_name
        == "windhub.configuration.v1.ConfigYamlDocument"
    )
