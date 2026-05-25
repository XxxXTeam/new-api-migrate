# SQLite to PostgreSQL Migration Tool

`sqlite_to_postgres.py` 用于把已经运行的 new-api 站点从 SQLite 迁移到 PostgreSQL。

推荐用法是：先让 new-api 使用 PostgreSQL 启动一次，由 GORM 创建官方 PostgreSQL 表结构；然后用本工具只迁移数据。这样最贴近项目当前模型，避免脚本根据 SQLite 老表结构推断出过期 schema。

## 准备工作

1. 停止 new-api。

   迁移前必须先停止服务，避免 SQLite 仍有写入，尤其是额度、日志、订阅预消费、批量更新等数据。

2. 创建 PostgreSQL 数据库和用户。

3. 用 PostgreSQL 启动 new-api 一次，让项目自动建表。

   示例环境变量：

   ```env
   SQL_DSN=postgresql://new-api:password@127.0.0.1:5432/new-api?sslmode=disable
   ```

   启动成功后停止 new-api，再运行迁移脚本。

4. 安装 Python PostgreSQL 驱动。

   ```bash
   python3 -m pip install 'psycopg[binary]'
   ```

   如果使用 `uv`：

   ```bash
   uv pip install 'psycopg[binary]'
   ```

## 推荐命令

```bash
python3 bin/sqlite_to_postgres.py \
  --sqlite /path/to/one-api.db \
  --postgres 'postgresql://new-api:password@127.0.0.1:5432/new-api?sslmode=disable' \
  --truncate-target
```

如果脚本放在 `tools/` 目录并用 `uv run` 执行：

```bash
uv run sqlite_to_postgres.py \
  --sqlite /opt/1panel/apps/new-api/new-api/data/one-api.db \
  --postgres 'postgresql://new-api:password@127.0.0.1:5432/new-api?sslmode=disable' \
  --truncate-target
```

注意：`--postgres` 的 DSN 必须和参数在同一条 shell 命令里。如果换行，上一行末尾要加 `\`。

## 参数说明

- `--sqlite`: SQLite 数据库文件路径，支持直接传 `SQLITE_PATH` 的值。
- `--postgres`: PostgreSQL DSN，支持 `postgres://` 和 `postgresql://`。
- `--backup-dir`: SQLite 一致性备份文件保存目录。
- `--no-backup`: 不创建 SQLite 备份，直接读取源库。不推荐在线站点使用。
- `--truncate-target`: 导入前清空目标 PostgreSQL 中对应表，并重置自增序列。推荐迁移到刚初始化的 PostgreSQL 时使用。
- `--allow-nonempty`: 允许导入到已有数据的 PostgreSQL 表。不推荐，除非你明确知道会发生什么。
- `--create-schema`: 根据 SQLite 元数据创建 PostgreSQL 表。仅作为备用方案；推荐先让 GORM 建表。
- `--drop-target`: 配合 `--create-schema` 使用，先删除目标表再重建。
- `--skip-indexes`: 配合 `--create-schema` 使用，跳过索引创建。
- `--batch-size`: 批量写入行数，默认 `1000`。
- `--lenient-json`: JSON 字段非法时写入 `NULL`，否则默认失败退出。
- `--dry-run`: 只检查源库和目标库，不写入 PostgreSQL。

## 脚本行为

脚本会执行以下步骤：

1. 对 SQLite 执行一致性备份，默认读取备份文件。
2. 执行 `PRAGMA integrity_check`。
3. 读取 SQLite 用户表清单，跳过 `sqlite_%` 内部表。
4. 检查 PostgreSQL 目标 schema。
5. 可选清空目标表。
6. 按表批量复制数据。
7. 转换已知 boolean 字段，例如 `tokens.unlimited_quota`、`subscription_plans.enabled`。
8. 校验 `channels.channel_info` JSON。
9. 修复 PostgreSQL 自增序列。
10. 对比每张表的源/目标行数。

如果中途失败，PostgreSQL 事务会回滚。修复问题后可以继续使用 `--truncate-target` 重跑。

## Redis 启用

Redis 不需要迁移 SQLite 数据。迁移完成后在 new-api 启动环境中增加：

```env
REDIS_CONN_STRING=redis://:password@127.0.0.1:6379/0
SYNC_FREQUENCY=60
```

Docker Compose 示例：

```env
SQL_DSN=postgresql://new-api:password@postgres:5432/new-api?sslmode=disable
REDIS_CONN_STRING=redis://:password@redis:6379/0
```

Redis 主要承载缓存、限流、同步等运行时数据，服务启动后会自动填充。

## 常见问题

### `argument --postgres: expected one argument`

原因是 `--postgres` 后面换行但没有使用 `\` 续行。

错误示例：

```bash
uv run sqlite_to_postgres.py --sqlite /path/one-api.db --postgres
'postgresql://user:pass@host:5432/new-api?sslmode=disable'
```

正确示例：

```bash
uv run sqlite_to_postgres.py --sqlite /path/one-api.db --postgres 'postgresql://user:pass@host:5432/new-api?sslmode=disable'
```

### `Target PostgreSQL already has rows`

目标库已有数据。迁移到刚初始化的 PostgreSQL 时使用：

```bash
--truncate-target
```

如果目标库不是迁移专用库，不要使用该参数，避免清空已有数据。

### `Missing PostgreSQL driver`

安装驱动：

```bash
python3 -m pip install 'psycopg[binary]'
```

### `Invalid JSON in channels.channel_info`

说明 SQLite 里存在非法 JSON。优先检查并修复源数据。若确认可以丢弃非法值，可加：

```bash
--lenient-json
```

### `psycopg.ProgrammingError: only '%s'... got '%'`

使用最新脚本。旧脚本中 PostgreSQL 序列查询里的 `%` 没有转义，会触发该错误。

## 迁移后检查

1. 确认脚本输出 `Migration completed`。
2. 启动 new-api，确认无数据库迁移错误。
3. 登录后台检查：
   - 用户数量
   - 渠道数量
   - Token 数量
   - 充值/订阅记录
   - 日志/用量统计
4. 确认 Redis 日志显示连接成功。

## 回滚方式

迁移脚本默认会在 SQLite 同目录或 `--backup-dir` 指定目录创建备份：

```text
one-api.pg-migration-YYYYMMDD-HHMMSS.db
```

如需回滚，停止 new-api，恢复原 SQLite 配置和数据库文件，再移除 `SQL_DSN` / `REDIS_CONN_STRING` 或改回原配置。
