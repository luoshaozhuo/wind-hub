---
name: changed-files-gate
description: Obtain the real staged, unstaged, untracked, mixed-index, and changed-file sets without inferring Git state from another agent's summary.
---

# changed-files-gate

## 用途

获取真实 Git 工作区范围，保护用户已有变更，并为显式检查阶段提供范围证据。

## 必须执行

```bash
git status --short
git diff --name-only
git diff --cached --name-only
git ls-files --others --exclude-standard
```

必要时补充 `git diff --name-status`、`git diff --cached --name-status` 和指定文件的 staged/unstaged diff。

## 阶段语义

1. 编码：展示完整工作区，只用于避免覆盖用户变更。
2. `/test`、`/validate`：`git diff --cached` 是唯一范围起点；排除 unstaged 和 untracked 内容。
3. `/test-all`：仍记录 Git 状态，但测试范围是当前整个工作区。
4. 同一文件同时存在 staged 和 unstaged hunks 时标记 `MIXED_INDEX_FILE`。

## 输出

```text
skill result:
- skill: changed-files-gate
- unstaged files:
- staged files:
- untracked files:
- mixed-index files:
- deleted files:
- renamed files:
- source files:
- test files:
- docs files:
- config/schema files:
- risk:
```

不输出 task tier，不自动触发测试、验证、全量测试或 project_tree 操作。
