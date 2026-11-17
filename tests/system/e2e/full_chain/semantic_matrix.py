"""E2E-08/I-09 的完整语义分母与真实 delta 执行清单。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SemanticEntry:
    """一个 CLI 叶子或 RPC 在一次明确场景下的可审计语义。"""

    key: str
    request: str
    scenario: str
    normal: str
    rejection: str
    auth: str
    object_id: str
    readback: str
    side_effect: str
    idempotency: str
    evidence: str
    action: str
    expected_exit: int | None = None
    expected_status: str | None = None
    expected_error: str | None = None
    expected_detail: str = "documented response"
    deployment: str = ""


SURFACE = "tests/system/e2e/full_chain/test_e2e_surface_matrix.py"
LIFECYCLE = (
    "tests/system/e2e/full_chain/test_e2e_lifecycle.py::"
    "test_e2e_09_cli_lifecycle_and_worker_cleanup"
)
COLLECTOR = (
    "tests/collector/system/test_collector_grpc_system.py::"
    "test_uc07_s01_all_20_rpcs_real_invocation"
)
COLLECTOR_REJECT = (
    "tests/collector/system/test_collector_grpc_system.py::"
    "test_uc07_s02_authorization_pagination_and_invalid_arguments"
)
COLLECTOR_GI = "tests/collector/system/test_collector_acquisition_system.py"
COMMANDER = (
    "tests/commander/integration/test_grpc.py::test_all_seventeen_rpcs_registered_and_callable"
)
CONTROL = "tests/system/e2e/full_chain/test_e2e_control.py::test_e2e_04_ppc_control_applied"
CONTROL_AUTH = (
    "tests/system/e2e/full_chain/test_e2e_control_security.py::"
    "test_e2e_04_request_replay_and_unauthorized_control"
)
RECOVERY = (
    "tests/commander/integration/test_recovery_rpc.py::"
    "test_recover_scope_excludes_viewer_and_allows_only_ops_deployer_to_boundary"
)
TRUSTED_RECOVERY = (
    "tests/commander/integration/test_trusted_recovery.py::"
    "test_external_fencing_success_unblocks_without_rewriting_unknown"
)
SINK = (
    "tests/sink_service/system/test_concurrent_delivery_e2e.py::"
    "TestConcurrentReliableDelivery::test_r7_04_stop_isolation_and_idempotent_start_stop"
)
SINK_AUTH = "tests/sink_service/system/test_grpc_api.py::TestMtlsAuthorizationMatrix"
GATEWAY = (
    "tests/data_generator/integration/test_gateway_integration.py::test_update_then_read_roundtrip"
)
GATEWAY_REJECT = (
    "tests/data_generator/integration/test_gateway_integration.py::test_unclaimed_update_rejected"
)
GATEWAY_LIFECYCLE = (
    "tests/data_generator/integration/test_gateway_integration.py::"
    "test_gateway_lifecycle_rpc_and_revision_conflict"
)
GENERATOR = "tests/data_generator/integration/test_grpc_server.py::test_all_fifteen_rpcs_callable"
CLI_AUTH = "tests/cli/test_r1f3_strict_cli_e2e.py (wrong/missing admin token)"
COMMON_CONFIG = (
    "tests/system/rpc/test_server_collector_rpc.py; "
    "tests/commander/integration/test_hot_reload.py::"
    "test_apply_replaces_files_without_semantic_reload_for_comment; "
    "tests/industrial_gateway/component/test_config_snapshots.py::"
    "test_push_validates_cas_file_set_integrity_and_config/"
    "test_apply_hot_reconfigures_desired_state; "
    "tests/sink_service/component/test_config_snapshot.py::"
    "test_push_validates_and_stages_without_mutating_disk/"
    "test_apply_commits_and_advances_runtime_when_converged; "
    "tests/data_generator/system/test_config_system.py::test_get_push_apply"
)
DELTA = "tests/system/e2e/full_chain/test_e2e_semantic_matrix.py"


def _cli(
    command: str,
    args: tuple[str, ...],
    *,
    expected_exit: int = 0,
    expected_error: str | None = None,
    action: str = "delta",
    evidence: str = DELTA,
    object_id: str = "local",
    normal: str = "exit 0 with the documented JSON result",
    rejection: str = "documented request rejection; no mutation",
    auth: str = "authenticated CLI identity; denied identity cannot mutate",
    readback: str = "N-A: read-only/local operation observed at its external interface",
    side_effect: str = "none",
    idempotency: str = "N-A: read-only or local operation",
    detail: str = "documented JSON envelope",
) -> SemanticEntry:
    return SemanticEntry(
        key=command,
        request=" ".join((command, *args)).strip(),
        scenario="normal" if expected_exit == 0 else "reject",
        normal=normal,
        rejection=rejection,
        auth=auth,
        object_id=object_id,
        readback=readback,
        side_effect=side_effect,
        idempotency=idempotency,
        evidence=evidence if action == "delta" else f"{evidence}; {SURFACE}",
        action=action,
        expected_exit=expected_exit,
        expected_error=expected_error,
        expected_detail=detail,
    )


def _mutation_cli(
    command: str,
    args: tuple[str, ...],
    *,
    object_id: str,
    readback: str,
    evidence: str,
    side_effect: str,
    idempotency: str,
) -> SemanticEntry:
    return _cli(
        command,
        args,
        action="reuse",
        evidence=evidence,
        object_id=object_id,
        normal="exit 0 only after the documented operation is accepted and observable",
        rejection="exact revision/identity rejection is non-zero and does not change the object",
        auth="unauthorized mutation is rejected before its side effect",
        readback=readback,
        side_effect=side_effect,
        idempotency=idempotency,
    )


CLI_ROWS = (
    _cli("help", (), object_id="CLI registry", normal="exit 0 and enumerate the registered leaves"),
    _cli("exit", (), object_id="one-shot shell", normal="exit 0 without starting a shell"),
    _cli("service list", (), object_id="six managed services plus collector alias"),
    _cli(
        "service status",
        (),
        object_id="all configured services",
        readback="Linux process state and CommonService status are separate JSON fields",
    ),
    _cli(
        "service info",
        ("gateway",),
        object_id="gateway",
        normal="exit 0 with service_id=industrial_gateway",
    ),
    _cli(
        "service capabilities",
        ("gateway",),
        object_id="gateway",
        normal="exit 0 with 9 Common + 10 Gateway RPC names",
    ),
    _mutation_cli(
        "service start",
        ("gateway", "--yes"),
        object_id="gateway process",
        readback="independent /proc identity and CommonService HEALTHY",
        evidence=LIFECYCLE,
        side_effect="one native process start; repeated start creates none",
        idempotency="repeat start returns already_running with the same PID",
    ),
    _mutation_cli(
        "service restart",
        ("collector_reliable", "--yes"),
        object_id="collector_reliable process",
        readback="new /proc identity and later CommonService HEALTHY",
        evidence=LIFECYCLE,
        side_effect="old process tree reaped and one replacement started",
        idempotency="N-A: restart intentionally changes PID; serialized by the process manager",
    ),
    _mutation_cli(
        "service stop",
        ("collector_reliable", "--yes"),
        object_id="collector_reliable process",
        readback="process STOPPED and old /proc identities reaped",
        evidence=LIFECYCLE,
        side_effect="managed process tree stopped",
        idempotency="repeat stop returns already_stopped",
    ),
    _cli(
        "config diff",
        ("{left}", "{right}"),
        object_id="two empty directories",
        normal="exit 0 with complete=true and no differences",
    ),
    _cli(
        "config get",
        ("gateway", "--scope", "disk"),
        expected_exit=2,
        expected_error="PERMISSION_DENIED",
        object_id="gateway DISK snapshot",
        normal="exit 0 only for a certificate granted config_read",
        rejection="N-A for this row: the selected case is an authorization rejection",
        auth=(
            "fixture CLI has read/lifecycle/inject/diagnose, not config_read: "
            "exact PERMISSION_DENIED"
        ),
        readback="independent configuration-tree hash remains unchanged",
        detail="FAILURE/PERMISSION_DENIED",
    ),
    _cli(
        "config push",
        ("gateway", "{empty}", "--expected-revision", "{zero_revision}"),
        expected_exit=2,
        expected_error="PERMISSION_DENIED",
        object_id="gateway candidate",
        normal="exit 0 for a valid candidate with config_write permission",
        rejection=(
            "malformed content returns valid=false or INVALID_ARGUMENT by service contract, "
            "never a commit"
        ),
        auth="fixture CLI lacks config_write: exact PERMISSION_DENIED",
        readback="DISK revision/files and independent tree hash remain unchanged",
        detail="FAILURE/PERMISSION_DENIED",
    ),
    _cli(
        "config apply",
        (
            "gateway",
            "missing-semantic-candidate",
            "--expected-revision",
            "{zero_revision}",
            "--yes",
        ),
        expected_exit=2,
        expected_error="PERMISSION_DENIED",
        object_id="missing gateway candidate",
        normal="authorized valid apply has an explicit committed/partial result",
        rejection="missing/stale candidate cannot commit",
        auth="fixture CLI lacks config_write: exact PERMISSION_DENIED",
        readback="DISK revision/files and independent tree hash remain unchanged",
        detail="FAILURE/PERMISSION_DENIED",
    ),
    _cli(
        "net ping",
        ("not-an-ip", "--timeout", "0.1"),
        expected_exit=2,
        expected_error="ValueError",
        object_id="invalid IP literal",
        normal="exit 0 only when loopback ICMP reports UP",
        rejection="invalid IP literal is a local ValueError with no probe",
        readback="N-A: rejected before network I/O",
    ),
    _cli(
        "net tcp",
        ("127.0.0.1", "{gateway_port}", "--timeout", "1"),
        object_id="gateway TCP listener",
        normal="exit 0 with tcp_state=OPEN",
    ),
    _cli(
        "net industrial",
        ("not-an-ip", "--timeout", "0.1"),
        expected_exit=2,
        expected_error="ValueError",
        object_id="invalid industrial-probe target",
        normal="exit 0 returns bounded per-port TCP states",
        rejection="invalid IP literal is a local ValueError with no probe",
    ),
    _cli(
        "net scan",
        ("not-a-cidr", "--timeout", "0.1"),
        expected_exit=2,
        expected_error="ValueError",
        object_id="invalid CIDR",
        normal="exit 0 returns a bounded host scan",
        rejection="invalid CIDR is a local ValueError with no scan",
    ),
    _cli("gateway list", ("--page-size", "5"), object_id="six Gateway instances"),
    _cli("gateway status", ("gw-wtg_sim01",), object_id="gw-wtg_sim01"),
    _cli("gateway variables", ("gw-wtg_sim01",), object_id="gw-wtg_sim01 variables"),
    _cli("gateway read", ("gw-wtg_sim01", "wind_speed"), object_id="gw-wtg_sim01/wind_speed"),
    _cli("gateway metrics", ("--window-ms", "1000"), object_id="Gateway service metrics"),
    _cli("gateway events", ("--page-size", "1"), object_id="Gateway events"),
    _mutation_cli(
        "gateway update",
        ("gw-wtg_sim01", "e2e_sink_target=1", "--owner-id", "cli", "--yes"),
        object_id="gw-wtg_sim01/e2e_sink_target",
        readback="independent ReadVariables returns the accepted value",
        evidence=GATEWAY,
        side_effect=(
            "Gateway-owned value updated under ACQUIRE/RELEASE lease; no industrial command"
        ),
        idempotency=(
            "N-A: value update is latest-value semantics; lease tokens are single-owner "
            "and released"
        ),
    ),
    _mutation_cli(
        "gateway stop",
        ("gw-wtg_sim01", "--yes"),
        object_id="gw-wtg_sim01",
        readback="GetGateway actual_state=STOPPED",
        evidence=GATEWAY_LIFECYCLE,
        side_effect="one Gateway server stopped",
        idempotency="repeated desired STOP converges without duplicate listener",
    ),
    _mutation_cli(
        "gateway start",
        ("gw-wtg_sim01", "--yes"),
        object_id="gw-wtg_sim01",
        readback="GetGateway actual_state=RUNNING",
        evidence=GATEWAY_LIFECYCLE,
        side_effect="one Gateway server started",
        idempotency="repeated desired START converges without duplicate listener",
    ),
    _cli("collector tasks", (), object_id="realtime Collector tasks"),
    _cli("collector devices", (), object_id="realtime Collector devices"),
    _cli("collector device", ("WTG_SIM01",), object_id="WTG_SIM01"),
    _cli("collector points", ("WTG_SIM01",), object_id="WTG_SIM01 point table"),
    _cli("collector point", ("WTG_SIM01", "wind_speed"), object_id="WTG_SIM01/wind_speed metadata"),
    _cli("collector task", ("{task_id}",), object_id="realtime WTG task"),
    _mutation_cli(
        "collector start",
        ("{task_id}", "--yes"),
        object_id="realtime WTG task",
        readback="GetTask RUNNING plus independent NATS samples",
        evidence=COLLECTOR,
        side_effect="durable desired START and one acquisition worker",
        idempotency="desired START converges; stale revision ABORTED",
    ),
    _cli(
        "collector latest",
        ("WTG_SIM01", "wind_speed"),
        object_id="WTG_SIM01/wind_speed active read",
    ),
    _cli(
        "collector watch",
        ("WTG_SIM01", "wind_speed", "--interval", "0.1", "--count", "1"),
        object_id="one active-read sample",
    ),
    _cli("collector publishing", (), object_id="realtime publishing channel"),
    _cli("collector metrics", ("--window-ms", "1000"), object_id="Collector service metrics"),
    _cli("collector events", ("--page-size", "1"), object_id="Collector events"),
    _mutation_cli(
        "collector stop",
        ("{task_id}", "--yes"),
        object_id="realtime WTG task",
        readback="GetTask STOPPED and acquisition worker stopped",
        evidence=COLLECTOR,
        side_effect="durable desired STOP",
        idempotency="desired STOP converges; stale revision ABORTED",
    ),
    _cli("sink deliveries", (), object_id="configured Sink deliveries"),
    _cli("sink delivery", ("ems_power",), object_id="ems_power"),
    _cli("sink metrics", ("--window-ms", "60000"), object_id="Sink 60000 ms metrics window"),
    _cli("sink events", ("--page-size", "1"), object_id="Sink events"),
    _mutation_cli(
        "sink stop",
        ("ems_power", "--yes"),
        object_id="ems_power",
        readback="GetDelivery STOPPED; stopped durable does not drain",
        evidence=SINK,
        side_effect="one Delivery stopped without affecting others",
        idempotency="repeat STOP remains STOPPED",
    ),
    _mutation_cli(
        "sink start",
        ("ems_power", "--yes"),
        object_id="ems_power",
        readback="GetDelivery RUNNING and original backlog drains",
        evidence=SINK,
        side_effect="one Delivery started without duplicate writer",
        idempotency="repeat START remains one RUNNING Delivery",
    ),
    _cli("data-generator devices", (), object_id="Generator device catalog"),
    _cli("data-generator device", ("WTG_SIM01",), object_id="WTG_SIM01"),
    _cli("data-generator scenarios", (), object_id="Generator scenario catalog"),
    _cli("data-generator metrics", ("--window-ms", "1000"), object_id="Generator metrics"),
    _cli("data-generator events", ("--page-size", "1"), object_id="Generator events"),
    _mutation_cli(
        "data-generator start",
        ("wind-plant-physical", "--scenario-version", "1.0.0", "--yes"),
        object_id="new Generation",
        readback="GetGeneration RUNNING and independent recording JSONL",
        evidence=GENERATOR,
        side_effect="one generation and recording stream",
        idempotency="N-A: each start deliberately creates a new generation_id",
    ),
    _cli("data-generator status", ("{generation_id}",), object_id="active Generation"),
    _cli(
        "data-generator variables list",
        ("--generation-id", "{generation_id}", "--device-id", "WTG_SIM01"),
        object_id="WTG_SIM01 model inputs",
    ),
    _cli(
        "data-generator variables update",
        (
            "--generation-id",
            "{generation_id}",
            "--device-id",
            "WTG_SIM01",
            "--request-id",
            "{request_id}",
            "--point-id",
            "wind_speed_mean",
            "--value",
            "12",
            "--yes",
        ),
        object_id="WTG_SIM01/wind_speed_mean",
        normal="exit 0 only after accepted=true and a positive effective_step",
        rejection="same request_id with different content is ALREADY_EXISTS/exit 2",
        auth="CLI certificate has variables_update; other identities cannot write",
        readback="GetInputExecution APPLIED at the same step plus independent recording value 12",
        side_effect="one physical model input applied at one step",
        idempotency="same request/content returns the original effective_step",
        detail="accepted=true, request_id and effective_step",
    ),
    _mutation_cli(
        "data-generator stop",
        ("{generation_id}", "--yes"),
        object_id="active Generation",
        readback="GetGeneration STOPPED and recording finalized",
        evidence=GENERATOR,
        side_effect="generation loop stopped",
        idempotency="N-A: duplicate StopGeneration replay is not claimed; single stop is verified",
    ),
    _cli("commander devices", (), object_id="Commander device catalog"),
    _cli("commander device", ("PPC_SIM01",), object_id="PPC_SIM01"),
    _cli("commander points", ("PPC_SIM01",), object_id="PPC_SIM01 control points"),
    _cli(
        "commander result",
        ("missing-semantic-command",),
        expected_exit=1,
        expected_error="NOT_FOUND",
        object_id="missing command",
        normal="exit 0 returns a durable command result",
        rejection="unknown command_id is exact NOT_FOUND/exit 1",
        readback="N-A: negative lookup proves caller-scoped absence",
    ),
    _cli(
        "commander request-result",
        ("missing-semantic-request",),
        expected_exit=1,
        expected_error="NOT_FOUND",
        object_id="missing request",
        normal="exit 0 returns the durable request and children",
        rejection="unknown request_id is exact NOT_FOUND/exit 1",
        readback="N-A: negative lookup proves caller-scoped absence",
    ),
    _cli(
        "commander recover-device",
        (
            "--device-id",
            "PPC_SIM01",
            "--expected-revision",
            "{zero_revision}",
            "--request-id",
            "{request_id}",
            "--attestation-file",
            "{attestation}",
        ),
        expected_exit=1,
        expected_error="FAILED_PRECONDITION",
        action="reuse",
        evidence=RECOVERY,
        object_id="PPC_SIM01 recovery",
        normal=(
            "UNBLOCKED only with an independent trusted authority; not claimable in this fixture"
        ),
        rejection=(
            "without trusted verification exact FAILED_PRECONDITION/exit 1 and device remains "
            "BLOCKED"
        ),
        auth="recover scope is restricted; viewer/unregistered certificates are PERMISSION_DENIED",
        readback="durable UNKNOWN/BLOCKED and zero device writes; successful path evidence is "
        + TRUSTED_RECOVERY,
        side_effect="none in full-chain fail-closed path",
        idempotency="same trusted request replays its decision; conflicting content ALREADY_EXISTS",
    ),
    _mutation_cli(
        "commander write",
        ("PPC_SIM01", "agc_enable", "1", "--yes"),
        object_id="PPC_SIM01/agc_enable",
        readback="GetCommandResult CONFIRMED_SUCCESS and Generator APPLIED",
        evidence=CONTROL + "; " + CONTROL_AUTH,
        side_effect="one durable command traverses Commander/Gateway/Generator",
        idempotency="same request_id returns command_id; conflicting content ALREADY_EXISTS",
    ),
    _mutation_cli(
        "commander batch",
        ("{batch}", "--yes"),
        object_id="one ordered PPC command batch",
        readback="GetRequestResult settles every child and Generator reports APPLIED",
        evidence=COMMANDER + "; " + CONTROL_AUTH,
        side_effect="atomic acceptance with independent per-child execution",
        idempotency="request_id is caller-scoped idempotency key; conflict is rejected",
    ),
    _cli(
        "commander metrics",
        ("--scope", "commander", "--window-ms", "1000"),
        object_id="Commander metrics",
    ),
    _cli("commander events", ("--page-size", "1"), object_id="Commander events"),
    _cli("diagnose collector device", ("WTG_SIM01",), object_id="WTG_SIM01"),
    _cli(
        "diagnose collector pipeline", ("WTG_SIM01",), object_id="WTG_SIM01 acquisition/publishing"
    ),
    _cli(
        "diagnose collector gi",
        ("WTG_SIM01", "--timeout-ms", "1000"),
        expected_exit=2,
        expected_error="INVALID_ARGUMENT",
        object_id="Modbus WTG_SIM01",
        normal="SUCCESS only on the dedicated real IEC104 station; evidence=" + COLLECTOR_GI,
        rejection="Modbus InterrogateDevice is exact INVALID_ARGUMENT/exit 2",
        auth="missing/wrong admin token is PERMISSION_DENIED before GI",
        side_effect="none: full-chain intentionally has no IEC104 GI",
    ),
    _cli("diagnose commander", ("PPC_SIM01",), object_id="PPC_SIM01"),
    _cli("diagnose sink", ("ems_power",), object_id="ems_power"),
    _cli("diagnose gateway", ("gw-wtg_sim01",), object_id="gw-wtg_sim01"),
    _cli("diagnose data-generator", ("{generation_id}",), object_id="active Generation"),
    _cli(
        "diagnose pipeline",
        ("WTG_SIM01", "--sink-id", "missing-semantic-delivery"),
        expected_exit=1,
        expected_error="NOT_FOUND",
        object_id="missing Sink delivery",
        normal="PASS requires shared verified sample IDs from Collector and Sink",
        rejection="missing Delivery is exact NOT_FOUND/exit 1; absence is never reported PASS",
        readback="N-A: rejected diagnostic target",
    ),
)


def _rpc(
    service: str,
    method: str,
    *,
    request: str = "{}",
    expected_status: str = "OK",
    expected_detail: str = "documented response",
    action: str = "reuse",
    evidence: str,
    object_id: str,
    normal: str = "OK with a schema-valid response",
    rejection: str = "documented request-level rejection, never transport success by assumption",
    auth: str = "authenticated caller only; unauthorized caller is PERMISSION_DENIED",
    readback: str = "N-A: read-only RPC response is the external observation",
    side_effect: str = "none",
    idempotency: str = "N-A: read-only operation",
    deployment: str = "",
) -> SemanticEntry:
    return SemanticEntry(
        key=f"{service}.{method}",
        request=request,
        scenario="normal" if expected_status == "OK" else "reject",
        normal=normal,
        rejection=rejection,
        auth=auth,
        object_id=object_id,
        readback=readback,
        side_effect=side_effect,
        idempotency=idempotency,
        evidence=evidence if action == "delta" else f"{evidence}; {SURFACE}",
        action=action,
        expected_status=expected_status,
        expected_detail=expected_detail,
        deployment=deployment,
    )


COMMON_METHODS = (
    "GetServiceInfo",
    "GetServiceStatus",
    "GetCapabilities",
    "GetConfigSnapshot",
    "PushConfigSnapshot",
    "ApplyConfigSnapshot",
    "GetMetrics",
    "GetEvents",
    "Diagnose",
)


RPC_ROWS = (
    *[
        _rpc(
            "CommonService",
            method,
            action="delta",
            evidence=DELTA
            + "::test_common_service_semantics_on_all_six_deployments; "
            + COMMON_CONFIG,
            object_id="expanded separately on all six deployments",
            normal="OK on the authorized deployment; per-deployment rows carry exact results",
            rejection="per-deployment exact status/response; rejected config cannot change DISK",
            auth="mTLS/admin token scopes enforced independently on each deployment",
            readback="service identity/capabilities or unchanged DISK snapshot and file hash",
            side_effect="none for reads; config rows deliberately exercise rejection only",
            idempotency="N-A for reads; rejected Push/Apply creates no committed revision",
        )
        for method in COMMON_METHODS
    ],
    *[
        _rpc(
            "CollectorService",
            method,
            evidence=COLLECTOR + "; " + COLLECTOR_REJECT,
            object_id="realtime/reliable Collector object appropriate to " + method,
            normal="OK with asserted device/point/task/publishing data"
            if method not in {"StartTask", "StopTask"}
            else "OK accepted=true and desired state recorded",
            rejection=(
                "NOT_FOUND/INVALID_ARGUMENT/ABORTED as asserted by the dedicated 20-RPC suite"
            ),
            auth="missing/wrong admin token is PERMISSION_DENIED on admin/read/control surfaces",
            readback="GetTask plus independent NATS samples"
            if method in {"StartTask", "StopTask"}
            else "N-A: external read response; active reads also compare real slave values",
            side_effect="durable task desired state and one worker"
            if method in {"StartTask", "StopTask"}
            else "none",
            idempotency="desired-state convergence; stale revision ABORTED"
            if method in {"StartTask", "StopTask"}
            else "N-A: read-only operation",
        )
        for method in (
            "ListDevices",
            "GetDevice",
            "ListPoints",
            "GetPoint",
            "ReadPoints",
            "ListTasks",
            "GetTask",
            "StartTask",
            "StopTask",
            "GetPublishingStatus",
            "InterrogateDevice",
        )
    ],
    _rpc(
        "CommanderService",
        "RecoverDeviceControl",
        expected_status="FAILED_PRECONDITION",
        expected_detail="trusted recovery verification or commit failed",
        evidence=RECOVERY + "; " + TRUSTED_RECOVERY,
        object_id="PPC_SIM01 recovery",
        normal=(
            "N-A in full-chain without independent trusted authority; dedicated authority "
            "test proves UNBLOCKED"
        ),
        rejection="exact FAILED_PRECONDITION and BLOCKED/UNKNOWN unchanged",
        auth="only recover-scoped certificate reaches verification",
        readback="durable BLOCKED/UNKNOWN and zero writes",
        side_effect="none on full-chain rejection",
        idempotency=(
            "trusted same-request replay returns original decision; conflict ALREADY_EXISTS"
        ),
    ),
    *[
        _rpc(
            "CommanderService",
            method,
            evidence=COMMANDER + "; " + CONTROL + "; " + CONTROL_AUTH,
            object_id="PPC_SIM01 or caller-scoped command identity",
            normal="OK; accepted control is independently settled, not equated with success"
            if method in {"SubmitCommand", "SubmitCommands"}
            else "OK with caller-scoped durable query data",
            rejection="NOT_FOUND/ALREADY_EXISTS/ABORTED according to identity/idempotency/revision",
            auth="query/control/reload scopes distinct; viewer cannot submit",
            readback="GetCommandResult/GetRequestResult plus Generator APPLIED"
            if method in {"SubmitCommand", "SubmitCommands"}
            else "N-A: durable external query result",
            side_effect="durable request and physical simulation input"
            if method in {"SubmitCommand", "SubmitCommands"}
            else "none",
            idempotency="request_id replays original acceptance; changed content ALREADY_EXISTS"
            if method in {"SubmitCommand", "SubmitCommands"}
            else "N-A: read-only operation",
        )
        for method in (
            "ListDevices",
            "GetDevice",
            "ListPoints",
            "SubmitCommand",
            "SubmitCommands",
            "GetCommandResult",
            "GetRequestResult",
        )
    ],
    *[
        _rpc(
            "SinkService",
            method,
            evidence=SINK + "; " + SINK_AUTH,
            object_id="ems_power or the selected Delivery",
            normal="OK accepted=true and desired state recorded"
            if method in {"StartDelivery", "StopDelivery"}
            else "OK with Delivery identity/state",
            rejection=(
                "unknown Delivery NOT_FOUND; stale revision ABORTED; query-only caller denied"
            ),
            readback="GetDelivery and independent backlog/output observation"
            if method in {"StartDelivery", "StopDelivery"}
            else "N-A: external Delivery query",
            side_effect="one Delivery transition; other deliveries isolated"
            if method in {"StartDelivery", "StopDelivery"}
            else "none",
            idempotency="repeat START/STOP converges without duplicate writer"
            if method in {"StartDelivery", "StopDelivery"}
            else "N-A: read-only operation",
        )
        for method in ("ListDeliveries", "GetDelivery", "StartDelivery", "StopDelivery")
    ],
    *[
        _rpc(
            "IndustrialGatewayService",
            method,
            evidence=(DELTA + "::test_commander_certificate_simulation_applied")
            if method in {"ExecuteSimulationCommand", "GetSimulationExecutionResult"}
            else GATEWAY + "; " + GATEWAY_REJECT + "; " + GATEWAY_LIFECYCLE,
            action="delta"
            if method in {"ExecuteSimulationCommand", "GetSimulationExecutionResult"}
            else "reuse",
            object_id="gw-ppc_sim01 simulation"
            if method in {"ExecuteSimulationCommand", "GetSimulationExecutionResult"}
            else "gw-wtg_sim01 and its variables",
            normal="OK; Commander request reaches APPLIED with effective_step"
            if method in {"ExecuteSimulationCommand", "GetSimulationExecutionResult"}
            else "OK with asserted Gateway/value/lease result",
            rejection=(
                "stale revision ABORTED, unmapped point PERMISSION_DENIED, stale lease "
                "FAILED_PRECONDITION"
            ),
            auth=(
                "Execute/query require Commander simulation_execute certificate; Update requires "
                "inject and lease"
            ),
            readback="GetSimulationExecutionResult plus Generator APPLIED and recording"
            if method in {"ExecuteSimulationCommand", "GetSimulationExecutionResult"}
            else (
                "independent ReadVariables after ACQUIRE/UPDATE/RELEASE"
                if method in {"UpdateVariables", "ClaimTargets"}
                else "GetGateway lifecycle state"
                if method in {"StartGateway", "StopGateway"}
                else "N-A: external catalog/read result"
            ),
            side_effect="one Commander-authorized model input"
            if method == "ExecuteSimulationCommand"
            else (
                "Gateway-owned value only; no industrial forwarding"
                if method in {"UpdateVariables", "ClaimTargets"}
                else "one Gateway lifecycle transition"
                if method in {"StartGateway", "StopGateway"}
                else "none"
            ),
            idempotency="same request_id returns original execution evidence"
            if method in {"ExecuteSimulationCommand", "GetSimulationExecutionResult"}
            else (
                "single-owner token; stale tokens never authorize writes"
                if method in {"UpdateVariables", "ClaimTargets"}
                else "desired-state convergence"
                if method in {"StartGateway", "StopGateway"}
                else "N-A: read-only operation"
            ),
        )
        for method in (
            "ListGateways",
            "GetGateway",
            "StartGateway",
            "StopGateway",
            "ListVariables",
            "ReadVariables",
            "UpdateVariables",
            "ClaimTargets",
            "ExecuteSimulationCommand",
            "GetSimulationExecutionResult",
        )
    ],
    *[
        _rpc(
            "DataGeneratorService",
            method,
            evidence=(DELTA + "::test_generator_update_is_applied_and_idempotent")
            if method in {"UpdateVariables", "GetInputExecution"}
            else GENERATOR,
            action="delta" if method in {"UpdateVariables", "GetInputExecution"} else "reuse",
            object_id="WTG_SIM01/wind_speed_mean request"
            if method in {"UpdateVariables", "GetInputExecution"}
            else "Generator catalog/Generation",
            normal="OK accepted=true; independent query reaches APPLIED at effective_step"
            if method in {"UpdateVariables", "GetInputExecution"}
            else "OK with asserted Generator identity/state",
            rejection=(
                "same request_id with different fingerprint ALREADY_EXISTS; invalid input "
                "accepted=false without a step"
            ),
            auth="CLI/Gateway variables_update identity required; caller scopes execution lookup",
            readback="GetInputExecution APPLIED and independent recording value 12"
            if method in {"UpdateVariables", "GetInputExecution"}
            else "GetGeneration plus recording JSONL"
            if method in {"StartGeneration", "StopGeneration"}
            else "N-A: external catalog/read result",
            side_effect="one model input at one physical step"
            if method == "UpdateVariables"
            else "one Generation lifecycle transition"
            if method in {"StartGeneration", "StopGeneration"}
            else "none",
            idempotency="identical request returns original effective_step"
            if method in {"UpdateVariables", "GetInputExecution"}
            else "new start gets new identity; duplicate stop replay is not claimed"
            if method in {"StartGeneration", "StopGeneration"}
            else "N-A: read-only operation",
        )
        for method in (
            "ListDevices",
            "GetDevice",
            "ListScenarios",
            "GetGeneration",
            "StartGeneration",
            "StopGeneration",
            "ListVariables",
            "UpdateVariables",
            "GetInputExecution",
        )
    ],
)


COMMON_DEPLOYMENTS = (
    "gateway",
    "collector_realtime",
    "collector_reliable",
    "commander",
    "sink",
    "data-generator",
)
_PUSH_EXPECTATIONS = {
    "gateway": ("PERMISSION_DENIED", "config_write denied before validation"),
    "collector_realtime": ("OK", "valid=false with validation issue"),
    "collector_reliable": ("OK", "valid=false with validation issue"),
    "commander": ("INVALID_ARGUMENT", "snapshot size mismatch"),
    "sink": ("OK", "valid=false with validation issue"),
    "data-generator": ("OK", "valid=false with validation issue"),
}
_APPLY_EXPECTATIONS = {
    "gateway": ("PERMISSION_DENIED", "config_write denied before candidate lookup"),
    "collector_realtime": ("FAILED_PRECONDITION", "missing candidate"),
    "collector_reliable": ("FAILED_PRECONDITION", "missing candidate"),
    "commander": ("NOT_FOUND", "missing candidate"),
    "sink": ("NOT_FOUND", "missing candidate"),
    "data-generator": ("OK", "file_commit=FAILED and effective revision unchanged"),
}


def _common_deployment_rows() -> tuple[SemanticEntry, ...]:
    rows: list[SemanticEntry] = []
    for deployment in COMMON_DEPLOYMENTS:
        for method in COMMON_METHODS:
            status = "OK"
            detail = "deployment-specific valid response"
            if deployment == "gateway" and method == "GetConfigSnapshot":
                status, detail = "PERMISSION_DENIED", "CLI lacks config_read"
            elif method == "PushConfigSnapshot":
                status, detail = _PUSH_EXPECTATIONS[deployment]
            elif method == "ApplyConfigSnapshot":
                status, detail = _APPLY_EXPECTATIONS[deployment]
            rows.append(
                _rpc(
                    "CommonService",
                    method,
                    deployment=deployment,
                    action="delta",
                    evidence=DELTA
                    + "::test_common_service_semantics_on_all_six_deployments; "
                    + COMMON_CONFIG,
                    expected_status=status,
                    expected_detail=detail,
                    object_id=f"{deployment} CommonService",
                    normal=(
                        "authorized normal response is deployment-specific and never inferred "
                        "from another service"
                    ),
                    rejection=(
                        f"exact {status}: {detail}; DISK revision/files remain unchanged for "
                        "config rejection"
                    ),
                    auth=(
                        "deployment's real mTLS/admin-token policy; Gateway CLI has no "
                        "config rights"
                    ),
                    readback=(
                        "service identity/capability or independent unchanged DISK snapshot/"
                        "file hash"
                    ),
                    side_effect="none; only invalid/missing config mutations are sent",
                    idempotency="rejected mutations commit nothing; reads are naturally repeatable",
                )
            )
    return tuple(rows)


COMMON_DEPLOYMENT_ROWS = _common_deployment_rows()
