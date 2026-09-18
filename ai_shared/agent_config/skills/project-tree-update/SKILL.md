---
name: project-tree-update
description: Use only when the user explicitly requests an incremental update of ai_shared/memory/project_tree.md from real repository changes.
---

# project-tree-update

## 1. 目的

用户明确要求时，根据文件新增、删除、移动、重命名或职责变化，增量更新 `ai_shared/memory/project_tree.md`。

## 2. 触发条件

必须使用：
- 用户明确要求增量更新 project_tree。

禁止使用：
- 仅因文件新增、删除、移动、重命名、职责变化或目录树不一致而自动检查或触发。
- 单纯读取 project_tree。
- 无变更时重写整棵树。

由当前主会话直接执行，不启动或委派独立 agent。

## 3. 输入

```text
changed-files-gate 结果
真实变更文件
ai_shared/memory/project_tree.md
必要时读取变更文件内容
```

## 4. 操作步骤

1. 读取当前 project_tree。
2. 根据 Git 变更判断新增、删除、移动、重命名、职责变化。
3. 对新增或职责变化文件读取文件头、类/函数、README 或入口代码，确认职责。
4. 增量修改 project_tree。
5. 保持完整文件级目录树，不得折叠到目录层。
6. 每个 item 职责说明通常不超过 40 个中文字符或 40 个英文词。

## 5. 输出格式

```text
skill result:
- skill: project-tree-update
- files added:
- files removed:
- files moved/renamed:
- files responsibility changed:
- project_tree changed: yes/no
- evidence:
- risk:
```
