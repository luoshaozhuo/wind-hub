wind-hub 精确候选配置发布根（2026-11-23）

来源：/configs 内的版本化 YAML；三个目录的 YAML 字节与相应源文件 Git Blob 相同。

prod-a/    Deployment prod-a v1.6.0，29 个 YAML。待 OEM/Manifest/现场核验，不得生产激活。
sim-a/     Deployment sim-a v1.3.0，23 个 YAML。模拟服务与管理面候选。
e2e-final/ Deployment e2e-final v1.0.1，35 个 YAML。仅供独立 E2E 验证。

Claude 本次仅做只读 YAML、精确引用闭包和 Loader 静态校验，不实际发布。
生产批准后方可使用：wind-hub config publish --root deploy/config_roots/<deployment-id>
不要使用仓库 configs/ 作为直接发布根：该目录包含不可变历史版本和未固定旧点表。
不允许自动选择版本最大者；必须显式选择 Deployment ID 和版本。
