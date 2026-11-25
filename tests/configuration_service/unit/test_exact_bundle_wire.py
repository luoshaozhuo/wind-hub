"""Generated Configuration wire exposes one immutable exact-root selector."""

from contracts import configuration_repository_pb2 as pb


def test_exact_roots_survive_protobuf_roundtrip() -> None:
    request = pb.GetConfigBundleRequest(
        exact_roots=[
            pb.ConfigVersionRef(
                config_id=pb.ConfigId(config_type="device", config_id="farm-a"),
                version_id="sha-of-immutable-repository-version",
            )
        ],
        include_dependencies=True,
    )
    restored = pb.GetConfigBundleRequest.FromString(request.SerializeToString())
    assert not restored.roots
    assert not restored.HasField("revision_id")
    assert restored.exact_roots[0].version_id == (
        "sha-of-immutable-repository-version"
    )
    assert restored.exact_roots[0].config_id.config_type == "device"
    assert restored.include_dependencies
