"""Collector CLI 独立配置任务归属与代次栅栏的解析测试。"""

import pytest

from cli.main import _collector_kwargs, build_parser


def test_placement_command_passes_complete_snapshot() -> None:
    args = build_parser().parse_args([
        "collector", "apply-placement", "worker-1", "12", "task-1", "task-2"
    ])
    assert _collector_kwargs(args) == {
        "worker_id": "worker-1",
        "generation": 12,
        "task_ids": ["task-1", "task-2"],
    }


def test_fenced_start_requires_generation() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["collector", "start-task", "task-1"])
    args = parser.parse_args([
        "collector", "start-task", "task-1", "--placement-generation", "12"
    ])
    assert _collector_kwargs(args) == {
        "task_id": "task-1",
        "placement_generation": 12,
    }


def test_empty_task_assignment_is_explicitly_supported() -> None:
    args = build_parser().parse_args([
        "collector", "apply-placement", "worker-1", "13"
    ])
    assert _collector_kwargs(args)["task_ids"] == []
