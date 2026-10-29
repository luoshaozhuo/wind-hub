---
name: merge-branch
description: 直接将当前任务分支 Squash Merge 到 main，不创建 PR、不执行合并门禁，确认远程结果后清理任务分支。
---

# merge-branch

用户调用 `//merge-branch` 即授权直接合并，无须再次询问。

在本地仓库运行：

```bash
python <skill-dir>/scripts/merge.py merge
```

## 流程

1. 确认当前位于非 main 的任务分支、工作区干净、本地 HEAD 与远程任务分支一致。
2. 获取远程 main；确保 main 是任务分支祖先，避免意外吞掉独立的新提交。
3. 以远程任务分支的整棵文件树、远程 main 为唯一 parent 创建一个 Squash Commit。
4. 普通、非强制推送 Squash Commit 到 `origin/main`，然后重新 fetch 验证。
5. 仅在远程 main 确认更新后同步本地 main，删除当前任务分支的本地与远程引用。
6. 主分支保护拒绝推送时立即报告阻断，不创建 PR、不绕过保护、不删除源分支。

## 不执行

- 不创建、查询或依赖 PR。
- 不执行 `scripts/ci_gate.py`、Ruff、Mypy、pytest、GitHub Check 或 Review 门禁。
- 不 force push、rebase、reset --hard，不覆写工作区用户修改。
- 不修改 GitHub 分支保护设置，不使用 admin 绕过。

验证和测试在开发、独立验收或 Release 阶段按需求单独执行，不作为合并的自动前置条件。

## 输出

报告源分支 SHA、合并前 main SHA、Squash Commit SHA、远程 main 最终 SHA、分支清理状态。推送失败时报告 `BLOCKED` 与服务端原因；严禁在 main 未更新时声称合并成功。
