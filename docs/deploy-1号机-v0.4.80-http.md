# 1 号机可直接执行部署说明（v0.4.80 / HTTP）

> 适用范围：把 1 号机恢复或重复部署到已经现场验证过的 ZiZu v0.4.80、Schema 037。
> 本说明遵循现场约束：不部署 Caddy、不申请 TLS，保留 `network_mode: host` 和
> `/dev/mqueue` tmpfs。它是联网测试/维护部署，不是符合生产安全基线的 TLS 发布。
>
> 当前正在开发的 L0 原始点位 → L1 点位转换 → L2 全局实体尚未形成新固定摘要，禁止把
> `ticket/unified-alarm-configuration` 工作树或本机源码直接覆盖到 1 号机。

## 1. 固定发布身份

```text
版本：0.4.80
架构：linux/arm64
Schema：037
镜像：ghcr.io/taidai/zizu@sha256:5abdd2d7b1d65b3cf90ecd3a78176d7b814cfb295d58f9a11e3dac209bfb2d41
Compose project：zizu-release-test
运行 Secret：/opt/zizu-release-test-0.4.80/runtime.env
```

不要使用 `latest`，不要在 1 号机执行 `docker build`，不要挂载 `backend/`、`frontend/`
或 `init-db/` 覆盖镜像内容。

## 2. 从维护电脑登录

PowerShell：

```powershell
ssh -i "$env:USERPROFILE\.ssh\zizu_1_key" `
  -p 13122 `
  -o BatchMode=yes `
  -o IdentitiesOnly=yes `
  -o StrictHostKeyChecking=yes `
  -o UserKnownHostsFile="$env:USERPROFILE\.ssh\known_hosts" `
  root@e606.hlszh.com
```

如果固定主机密钥不匹配，立即停止，不要使用
`StrictHostKeyChecking=no` 绕过。

## 3. 在 1 号机准备发布目录

以下命令全部在 1 号机执行：

```bash
set -euo pipefail

RELEASE_DIR=/opt/zizu-release-test-0.4.80
RUNTIME_ENV="$RELEASE_DIR/runtime.env"
IMAGE='ghcr.io/taidai/zizu@sha256:5abdd2d7b1d65b3cf90ecd3a78176d7b814cfb295d58f9a11e3dac209bfb2d41'

install -d -m 0750 "$RELEASE_DIR"
cd "$RELEASE_DIR"

test -s "$RUNTIME_ENV"
chmod 0600 "$RUNTIME_ENV"
test "$(uname -m)" = 'aarch64'
docker inspect zizu-tsdb >/dev/null
```

`runtime.env` 必须沿用现场已有文件；不要在部署时重新生成或把内容打印到终端。它至少应保持
数据库、Neuron、NanoMQ、JWT 的现有 Secret。部署编排会覆盖网络和 HTTP 模式相关变量。

写入固定发布环境：

```bash
cat > release.env <<'EOF'
ZIZU_PLATFORM_VERSION=0.4.80
ZIZU_SCHEMA_VERSION=037
ZIZU_PLATFORM_IMAGE=ghcr.io/taidai/zizu@sha256:5abdd2d7b1d65b3cf90ecd3a78176d7b814cfb295d58f9a11e3dac209bfb2d41
ZIZU_RUNTIME_ENV=/opt/zizu-release-test-0.4.80/runtime.env
EOF
chmod 0640 release.env
```

写入 1 号机专用、backend-only 编排：

```bash
cat > compose.http.yml <<'EOF'
name: zizu-release-test

services:
  backend:
    image: ${ZIZU_PLATFORM_IMAGE:?release.env is required}
    restart: unless-stopped
    network_mode: host
    tmpfs:
      - /dev/mqueue
    env_file:
      - ${ZIZU_RUNTIME_ENV:?runtime.env is required}
    environment:
      APP_BIND_HOST: 0.0.0.0
      APP_PORT: "9000"
      DB_HOST: 127.0.0.1
      DB_PORT: "5432"
      MQTT_HOST: 127.0.0.1
      MQTT_PORT: "1883"
      NEURON_API_URL: http://127.0.0.1:7000
      NANOMQ_API_URL: http://127.0.0.1:8081
      DB_OWNER_USER: ""
      DB_OWNER_PASSWORD: ""
      DEPLOYMENT_MODE: development
      ALLOW_INSECURE_DEV_SECRETS: "false"
      ALLOW_INSECURE_ANONYMOUS_ACCESS: "false"
      AUTH_REQUIRE_HTTPS: "false"
      AUTH_TRUST_PROXY_HEADERS: "false"
      AUTH_TRUSTED_PROXY_CIDRS: '[]'
      PUBLIC_API_BASE_URL: http://127.0.0.1:9000
      FRONTEND_DIST: /app/frontend/dist
    volumes:
      - zizu-data:/app/data
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL

volumes:
  zizu-data:
EOF
chmod 0640 compose.http.yml

docker compose --env-file release.env -f compose.http.yml config --quiet
```

这里故意没有 PostgreSQL、NanoMQ、Neuron 和 Caddy 服务；部署只替换 ZiZu backend，不重建
现场基础设施。

## 4. 拉取并核验固定镜像

如果 GHCR 尚未登录，只能通过受控令牌的标准输入登录，禁止把令牌写进命令或文件：

```bash
# read -rsp 'GHCR token: ' GHCR_TOKEN; echo
# printf '%s' "$GHCR_TOKEN" | docker login ghcr.io -u <github-user> --password-stdin
# unset GHCR_TOKEN
```

然后执行：

```bash
docker pull "$IMAGE"

test "$(docker image inspect "$IMAGE" --format '{{.Architecture}}')" = 'arm64'
test "$(docker image inspect "$IMAGE" --format '{{index .Config.Labels "org.opencontainers.image.version"}}')" = '0.4.80'
docker image inspect "$IMAGE" --format 'image_id={{.Id}} architecture={{.Architecture}}'
```

任一核验失败都不得继续。

## 5. 备份数据库并核对 Schema

```bash
BACKUP_DIR=/opt/zizu-backups
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP_FILE="$BACKUP_DIR/zizu_iot-before-0.4.80-$STAMP.dump"
PG_IMAGE=$(docker inspect zizu-tsdb --format '{{.Config.Image}}')

install -d -m 0700 "$BACKUP_DIR"
docker exec zizu-tsdb sh -ec \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
  > "$BACKUP_FILE"
test -s "$BACKUP_FILE"

docker run --rm \
  -v "$BACKUP_DIR:/backup:ro" \
  --entrypoint pg_restore \
  "$PG_IMAGE" -l "/backup/$(basename "$BACKUP_FILE")" >/dev/null

sha256sum "$BACKUP_FILE" | tee "$BACKUP_FILE.sha256"

SCHEMA_VERSION=$(docker exec zizu-tsdb sh -ec \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc \
   "SELECT version FROM schema_migrations ORDER BY version::integer DESC LIMIT 1"')
test "$SCHEMA_VERSION" = '037'
```

本版本的 034—037 迁移已经在现场成功执行过。若 Schema 不是 037，立即停止，不要通过手工改
`schema_migrations` 伪造版本。

## 6. 切换 backend

```bash
cd /opt/zizu-release-test-0.4.80

docker compose -p zizu-release-test \
  --env-file release.env \
  -f compose.http.yml \
  up -d --no-build --force-recreate --no-deps backend

CID=$(docker compose -p zizu-release-test \
  --env-file release.env -f compose.http.yml ps -q backend)
test -n "$CID"
```

严格核验实际容器：

```bash
EXPECTED_ID=$(docker image inspect "$IMAGE" --format '{{.Id}}')
ACTUAL_ID=$(docker inspect "$CID" --format '{{.Image}}')
test "$ACTUAL_ID" = "$EXPECTED_ID"
test "$(docker inspect "$CID" --format '{{.HostConfig.NetworkMode}}')" = 'host'
docker inspect "$CID" --format '{{json .HostConfig.Tmpfs}}' | grep -q '/dev/mqueue'
```

## 7. 自动健康门禁

```bash
healthy=0
for attempt in $(seq 1 30); do
  if body=$(curl -fsS --max-time 3 http://127.0.0.1:9000/api/v1/health/live); then
    if printf '%s' "$body" | python3 -c \
      'import json,sys; d=json.load(sys.stdin); assert d == {"status":"alive","version":"0.4.80"}'; then
      healthy=1
      break
    fi
  fi
  sleep 2
done
test "$healthy" = '1'

curl -fsS --max-time 5 http://e606.hlszh.com:9000/api/v1/health/live
curl -fsS --max-time 5 http://e606.hlszh.com:9000/ >/dev/null

if docker logs --since 5m "$CID" 2>&1 | \
  grep -Eiq 'traceback|critical|migration.+error|startup failed'; then
  docker logs --since 5m "$CID"
  exit 1
fi

docker compose -p zizu-release-test \
  --env-file release.env -f compose.http.yml ps
```

浏览器最后验证：

1. 打开 `http://e606.hlszh.com:9000/`；
2. 使用已供应的管理员账号登录；
3. `/auth/me` 能恢复身份；
4. 节点、实时数据、告警中心能够读取；
5. 不执行设备控制，不创建临时告警规则，除非进入单独的现场验收窗口。

## 8. 失败处理与回滚边界

先保留现场证据：

```bash
docker logs --since 15m "$CID" > "/opt/zizu-backups/zizu-0.4.80-failed-$STAMP.log" 2>&1
docker compose -p zizu-release-test --env-file release.env -f compose.http.yml ps
```

- 如果只是容器启动失败且 Schema 仍为 037，可重新执行第 6 节；镜像摘要不变，属于同制品恢复。
- 如果数据库已经升级到高于 037，禁止单独切回 v0.4.80 镜像。必须停止 backend，评估迁移兼容性，
  并在明确批准后用本轮已校验的 custom-format 备份恢复。
- 不要运行 `docker compose down -v`；它会删除 `zizu-data` 卷。
- 不要删除 `zizu-tsdb`、`zizu-nanomq` 或 Neuron，不要执行旧 `deploy.sh`、
  `scripts/deploy_1号机.py`、`docs/deploy-e606.md` 中的旧部署流程。

## 9. 完成标准

只有以下全部满足才算部署成功：

- 容器实际 image ID 等于固定摘要解析出的 image ID；
- 架构为 arm64，版本 label 为 0.4.80；
- backend 使用 host network，且 `/dev/mqueue` tmpfs 存在；
- Schema 精确为 037；
- 本机与公网 liveness 均返回 `alive / 0.4.80`；
- 首页可读、管理员可登录、受认证业务读取正常；
- 最近日志无迁移错误、Traceback 或 Critical；
- 备份文件、`pg_restore -l` 校验和 SHA-256 均存在。
