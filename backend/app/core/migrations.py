"""
ZiZu DB migration runner — 自动应用 init-db/migration_*.sql。

设计原则：
  - 幂等：每条迁移只执行一次，通过 schema_migrations 表记录。
  - 顺序：按 migration_###_*.sql 中的数字序号排序。
  - 路径：优先读取环境变量 MIGRATIONS_DIR，其次容器默认 /app/init-db，
          最后回退到开发仓库根目录下的 init-db。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from loguru import logger

_MIGRATION_PATTERN = re.compile(r"migration_(\d+).*\.sql$")


def _find_migrations_dir() -> Path | None:
    """定位迁移文件目录。"""
    candidates = []
    env_dir = os.environ.get("MIGRATIONS_DIR")
    if env_dir:
        candidates.append(Path(env_dir))
    candidates.append(Path("/app/init-db"))
    # 开发环境：从 backend/app/core/migrations.py 向上找 repo/init-db
    repo_init = Path(__file__).resolve().parents[3] / "init-db"
    candidates.append(repo_init)
    candidates.append(Path.cwd() / "init-db")

    for cand in candidates:
        if cand.exists() and cand.is_dir():
            return cand
    return None


def _list_migration_files(migrations_dir: Path) -> list[tuple[str, str, Path]]:
    """返回 (version, filename, path) 列表，按 version 排序。"""
    files: list[tuple[str, str, Path]] = []
    for path in sorted(migrations_dir.glob("migration_*.sql")):
        match = _MIGRATION_PATTERN.match(path.name)
        version = match.group(1) if match else path.stem
        files.append((version, path.name, path))
    files.sort(key=lambda x: x[0])
    return files


def _ensure_migrations_table(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ DEFAULT now()
        )
        """
    )


def _applied_versions(cur) -> set[str]:
    try:
        cur.execute("SELECT version FROM schema_migrations")
        return {row[0] for row in cur.fetchall()}
    except Exception as e:
        logger.warning("[Migrations] failed to read schema_migrations: {}", e)
        return set()


def run_migrations() -> dict:
    """
    执行所有未应用的迁移文件。

    Returns:
        {"applied": [versions], "skipped": [versions], "errors": int}
    """
    migrations_dir = _find_migrations_dir()
    if migrations_dir is None:
        logger.warning("[Migrations] init-db directory not found, skip auto migration")
        return {"applied": [], "skipped": [], "errors": 1}

    files = _list_migration_files(migrations_dir)
    if not files:
        logger.info("[Migrations] no migration_*.sql files found in {}", migrations_dir)
        return {"applied": [], "skipped": [], "errors": 0}

    from app.services.telemetry_store import get_connection

    result = {"applied": [], "skipped": [], "errors": 0}

    production = os.environ.get("DEPLOYMENT_MODE", "production") == "production"
    with get_connection() as conn:
        with conn.cursor() as cur:
            if production:
                cur.execute("SELECT to_regclass('public.schema_migrations')")
                if cur.fetchone()[0] is None:
                    logger.error(
                        "[Migrations] production requires schema_migrations; run the owner migration job first"
                    )
                    return {"applied": [], "skipped": [], "errors": 1}
            else:
                _ensure_migrations_table(cur)
            applied = _applied_versions(cur)

            if production:
                pending = [version for version, _filename, _path in files if version not in applied]
                if pending:
                    logger.error(
                        "[Migrations] production application role cannot apply pending migrations: {}",
                        pending,
                    )
                    return {"applied": [], "skipped": sorted(applied), "errors": len(pending)}
                result["skipped"] = [version for version, _filename, _path in files]
                return result

            for version, filename, path in files:
                if version in applied:
                    result["skipped"].append(version)
                    continue
                sql = path.read_text(encoding="utf-8")
                try:
                    cur.execute(sql)
                    cur.execute(
                        "INSERT INTO schema_migrations (version) VALUES (%s)",
                        (version,),
                    )
                    conn.commit()
                    result["applied"].append(version)
                    logger.info("[Migrations] applied {} ({})", version, filename)
                except Exception as e:
                    conn.rollback()
                    result["errors"] += 1
                    logger.error(
                        "[Migrations] failed to apply {} ({}): {}",
                        version,
                        filename,
                        e,
                    )

    return result
