wind-hub 精确候选配置发布根（2026-11-25）

三个根内 YAML 与 /configs 对应文件 Git Blob 一致。
prod-a/    Deployment prod-a v2.0.2，29 个 YAML。
sim-a/     Deployment sim-a v2.0.1，23 个 YAML。
e2e-final/ Deployment e2e-final v2.0.2，35 个 YAML。

宽表候选配置，禁止在 D4 实现迁移及最终独立验收前激活。
生产仍需 OEM/Manifest/现场核验。
仅在批准后使用 wind-hub config publish --root deploy/config_roots/<deployment-id>。
禁止直接发布混合历史版本的 configs/；选择精确 Deployment ID/version。
仓库历史文件清理不得删除 Configuration 持久化归档。
