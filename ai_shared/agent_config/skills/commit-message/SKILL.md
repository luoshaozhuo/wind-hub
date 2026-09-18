---
name: commit-message
description: Use when the user explicitly asks for a commit message based on the current staged diff and you should generate the message without running git commit.
---

# commit-message

## 1. 目的

根据当前 staged diff 生成提交说明；只生成，不执行 commit。

## 2. 触发条件

用户明确要求生成 commit message。

禁止使用：
- 自动执行 git commit。
- 把 unstaged 变更写入提交说明。

## 3. 推荐命令

```bash
git diff --staged --stat
git diff --staged --name-status
git diff --staged
```

## 4. 输出要求

1. 使用中文输出。
2. 默认只读取 staged 变更。
3. 没有 staged 变更时，提示用户先执行 `git add`。
4. 输出可直接复制的 commit message。

## 5. 输出格式

```text
<type>(<scope>): <summary>

<body>

<footer if needed>
```
