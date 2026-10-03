# Codex 执行入口

默认使用中文。

开始仓库任务时读取：

```text
CLAUDE.md
ai_shared/rules/routing.md
```

随后只按 routing 读取当前任务需要的规则、源码、测试、配置和 schema。所有任务由当前主会话执行，不创建平行规则体系。
