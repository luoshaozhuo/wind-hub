# HTTPS 证书

1. 用户可以在此目录提供正式 `dc-ems.jks`。
2. 文件不存在时，`deploy.sh` 使用 keytool 生成 JKS 格式自签名证书，SAN 来自 `.env` 的 JKS_SAN。自签名证书须由现场客户端单独建立信任，不代表正式证书部署完成。

当前 `ConfigInfo.java` 默认读取 `./ssl/dc-ems.jks`，密码固定为源码中的 `1*@3789HZ-dcems`。提供的 JKS 必须与此加载契约相符；脚本用 keytool 检查可读性。修改密码需要应用配置配套，本轮不修改业务源码。

脚本明确使用 `-storetype JKS`，避免新 JDK 默认生成 PKCS12 与运行契约混淆。真实 JKS 和私钥不得提交 Git 或纳入发布包。

`dc-ems.jks` 是 Site-specific Runtime Artifact，不是 Release Artifact。
