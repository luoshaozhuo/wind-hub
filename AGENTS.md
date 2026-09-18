# Codex 执行入口

默认使用中文。仓库公共规则只有一套，Codex 不在本文件复制维护阶段细节。

开始任务时读取：

```text
CLAUDE.md
ai_shared/rules/routing.md
```

随后只按 routing 读取当前阶段需要的规则、真实源码、测试、配置和 schema；
`ai_shared/memory/project_tree.md` 仅用于导航。普通编码不自动进入测试或验证，
`$test`、`$validate`、`$test-all` 只能由用户显式调用。所有任务由当前主会话执行，
不启动或委派独立 agent，不自动执行 Git/GitHub 写操作。
