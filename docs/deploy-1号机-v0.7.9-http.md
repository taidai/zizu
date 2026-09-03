# v0.7.9 HTTP 通知选项修复部署记录

2026-09-03，用户确认后部署。仅修复通知选项读取与提示，不自动启用通知、不改变告警判定或投递语义。

## 发布锁定

- 源码/tag：`66f6a4c5c0086f8fdb47698f2a20b063a4cedb95` / `v0.7.9`。
- [Actions 33709056460](https://github.com/taidai/zizu/actions/runs/33709056460)：成功，双架构清单校验通过。
- ARM64：`ghcr.io/taidai/zizu@sha256:1200bae55184cec4c4e4091148e2f0eff77ad74b8ee14e97e42b2d51fd13c217`。
- 运行镜像 ID：`sha256:6fe38b9afc0446acb464e21c4a99c6562eb525d0a8a9356e0e03982b0c31f8f7`。
- 应用启动于 `2026-09-03T02:57:30.810701864Z`；healthy、restart 0，前后端均为 0.7.9，Schema060。
- 只重建应用容器，保留 host 网络、`/dev/mqueue` tmpfs 和原运行配置；数据库与 NanoMQ 未重启。
  没有启动 Caddy 或申请证书。首页与匿名存活接口均 HTTP200。
- 根分区部署后仍有约3.3GiB可用，使用率79%；没有删除数据、旧镜像或备份。

## 本轮验证

- 后端 unittest 513项，191条件跳过、0失败；脚本56/56；前端mjs88/88；生产构建通过。
- v0.7.9生产包无头选项及加载专项6/6通过，4.6秒。选择与错误重试场景使用本地模拟HTTP，不发送消息。
- 1号机真实无头 Chromium：节点 → L0 → L1 → L2 → 告警逐页操作通过；91个L0正常，
  原始BIT值0保持数值；点位加工生效，2个L2正常，当前及已恢复告警可加载。
- 告警规则调用真实 `GET /api/v1/alarm-http-notification-options`，返回HTTP200及2项停用状态；
  严格验证每项只有id/name/status。页面解释停用原因及系统工具入口，刷新后读取成功且选择保持。
- 既有通知均未启用，因而仍不可选择发送；这是安全门槛，不是加载失败。管理员需明确启用已测试配置，
  再在规则页刷新并选择。此次没有替用户启用、修改绑定或发送测试消息。
- 站点配置、配置修订、告警定义/活动引用、HTTP配置/绑定共6张表在切换前后内容摘要一致。
- 浏览器阻断除登录/实时订阅凭证外的全部非只读API请求，最终无被阻断业务写请求。
  没有发布规则、确认告警或控制设备。截图仅留在本机 `.release-artifacts/v0.7.9/http-options.png`，不入库。
- 内置Browser建立连接后读取DOM超时；未把该过程算作通过。上述现场证据来自真实无头Chromium。

## 备份与软件回滚

- 保留v0.7.8回滚镜像：`ghcr.io/taidai/zizu@sha256:3db7dd6e0f98e426bf69189e8f81febbe6522974c9f22540f04db1a501c22285`。
- `/opt/zizu-release-test-0.5.0/backups/v0.7.9-pre/` 私有目录保存release.env、Compose、runtime.env副本，
  以及上述6张配置表的`alarm-configs.dump`（79,329字节，权限600）。完整解码读取通过，
  SHA256：`b131315db901cde619ae6d063abd81a1550d14fd381d0bb4de0215dae4aad8a6`。
- 既有同日全库备份仍保留：`backups/v0.7.8-pre/omnithings-20260903T012109Z.dump`，520,216,227字节。
  本轮未重复做全库压缩，也未做隔离恢复；本轮没有Schema变更，软件回滚不需要恢复数据库。
- 切换前核对旧镜像和配置副本未变；仅修改release.env的镜像摘要。Compose等待健康最多55秒，失败则
  恢复旧release.env并重建旧应用。本轮健康通过，没有执行回滚。

如需回滚，先确认没有后续发布，再在1号机执行：

```sh
cd /opt/zizu-release-test-0.5.0
cp -p backups/v0.7.9-pre/release.env release.env
docker compose --env-file release.env -f docker-compose.test.e606.yml up -d --no-deps --wait --wait-timeout 55 backend
```

仅回滚软件，不恢复数据库、不删除新数据。保留HTTP通知加密密钥，禁止把私有配置副本上传公共仓库。

本轮发布与专项验收 **PASSED**，不表示已验证真实外部通知发送或全平台所有CRUD。
