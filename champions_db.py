"""冠军赛季数据：SQLite 存储与只读查询。

数据由独立脚本 ``champions_fetch.py`` 抓取写入，插件运行时只读。

存储位置（按优先级）：
  1. 显式参数 / 插件配置 champions_db_path
  2. 环境变量 POKEDEX_CHAMPIONS_DB
  3. AstrBot 数据目录：data/plugin_data/astrbot_plugin_pokedex/champions.db
     （优先 astrbot.core.utils.astrbot_path.get_astrbot_data_path，
       其次根据插件所在的 .../data/plugins/<name> 反推，最后回退插件目录 data/）

表结构只保留排名与对战资料（使用率、招式、道具、特性、性格、EV、队友），
不存属性/种族值/类型等基础信息；pokemon_id 用于与图鉴数据关联。
"""

from __future__ import annotations

import difflib
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

PLUGIN_NAME = "astrbot_plugin_pokedex"
DB_FILENAME = "champions.db"

SOURCE_LABELS = {
    "pokechamdb": "PokéChamp DB",
    "itlibra": "poke.itlibra.com",
    "pokedb": "champs.pokedb.tokyo",
    "opgg": "OP.GG",
}
FORMAT_LABELS = {"single": "单打", "double": "双打"}

OPTION_LABELS = {
    "move": "招式",
    "item": "道具",
    "ability": "特性",
    "nature": "性格",
    "tera": "太晶",
    "ev_spread": "能力点",
    "teammate": "队友",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS champion_pokemon (
    pokemon_id  TEXT PRIMARY KEY,
    name_zh     TEXT,
    name_en     TEXT,
    national_no INTEGER
);

CREATE TABLE IF NOT EXISTS champion_usage (
    usage_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    pokemon_id   TEXT NOT NULL REFERENCES champion_pokemon(pokemon_id),
    format       TEXT NOT NULL,
    season       TEXT NOT NULL,
    source       TEXT NOT NULL,
    usage_pct    REAL,
    rank         INTEGER,
    battle_count INTEGER,
    updated_at   TEXT,
    UNIQUE (pokemon_id, format, season, source)
);

CREATE INDEX IF NOT EXISTS idx_champion_usage_rank
    ON champion_usage (format, season, source, rank);
CREATE INDEX IF NOT EXISTS idx_champion_usage_pokemon
    ON champion_usage (pokemon_id, format, source);

CREATE TABLE IF NOT EXISTS champion_option (
    option_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    usage_id     INTEGER NOT NULL REFERENCES champion_usage(usage_id) ON DELETE CASCADE,
    option_type  TEXT NOT NULL,
    option_value TEXT NOT NULL,
    usage_pct    REAL,
    rank         INTEGER,
    extra        TEXT,
    UNIQUE (usage_id, option_type, option_value)
);

CREATE INDEX IF NOT EXISTS idx_champion_option_reverse
    ON champion_option (option_type, option_value);

CREATE TABLE IF NOT EXISTS champion_team (
    team_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    format        TEXT NOT NULL,
    season        TEXT NOT NULL,
    rank          INTEGER,
    rating_value  REAL,
    source        TEXT NOT NULL,
    season_number INTEGER,
    UNIQUE (format, season, source, rank)
);

CREATE TABLE IF NOT EXISTS champion_team_member (
    member_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id    INTEGER NOT NULL REFERENCES champion_team(team_id) ON DELETE CASCADE,
    slot       INTEGER NOT NULL,
    pokemon_id TEXT,
    name_raw   TEXT,
    source_id  TEXT,
    terastal   TEXT,
    item       TEXT,
    UNIQUE (team_id, slot)
);

CREATE TABLE IF NOT EXISTS champion_sync_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    source            TEXT NOT NULL,
    format            TEXT,
    season            TEXT,
    scope             TEXT NOT NULL,
    source_updated_at TEXT,
    status            TEXT NOT NULL,
    message           TEXT,
    items_written     INTEGER DEFAULT 0,
    run_at            TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_champion_sync_lookup
    ON champion_sync_log (source, format, season, scope, id DESC);
"""


# ---------------------------------------------------------------------------
# 路径解析
# ---------------------------------------------------------------------------
def resolve_db_path(explicit: str | Path | None = None) -> Path:
    """解析 champions.db 路径（插件与独立脚本共用，保证读写同一文件）。"""
    if explicit:
        return Path(explicit).expanduser()

    env_path = os.environ.get("POKEDEX_CHAMPIONS_DB")
    if env_path:
        return Path(env_path).expanduser()

    data_root = _astrbot_data_root()
    if data_root is not None:
        return data_root / "plugin_data" / PLUGIN_NAME / DB_FILENAME

    # 最后回退：插件自身 data/（README 会提示该位置不符合 AstrBot 规范）
    return Path(__file__).resolve().parent / "data" / DB_FILENAME


def _astrbot_data_root() -> Path | None:
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        return Path(get_astrbot_data_path())
    except Exception:  # astrbot 未安装 / 旧版本
        pass

    env_root = os.environ.get("ASTRBOT_DATA_PATH")
    if env_root:
        return Path(env_root).expanduser()

    # 插件目录形如 <data>/plugins/astrbot_plugin_pokedex
    here = Path(__file__).resolve().parent
    if here.parent.name == "plugins" and here.parent.parent.name == "data":
        return here.parent.parent

    for candidate in (
        Path.home() / "AstrBot" / "data",
        Path.home() / "astrbot" / "data",
    ):
        if candidate.is_dir():
            return candidate
    return None


def season_key(season: str | None) -> tuple[int, Any]:
    """赛季排序键：M-7 > M-6；月份格式按年月比较。"""
    s = (season or "").strip()
    m = re.fullmatch(r"M-(\d+)", s, re.IGNORECASE)
    if m:
        return (1, int(m.group(1)))
    m = re.fullmatch(r"(\d{4})-(\d{2})", s)
    if m:
        return (2, int(m.group(1)) * 100 + int(m.group(2)))
    return (0, s)


def fmt_label(fmt: str) -> str:
    return FORMAT_LABELS.get(fmt, fmt)


def source_label(source: str) -> str:
    return SOURCE_LABELS.get(source, source)


# ---------------------------------------------------------------------------
# 查询层
# ---------------------------------------------------------------------------
class ChampionsDB:
    """只读查询封装。数据库不存在时返回空结果，不抛异常。"""

    def __init__(self, path: str | Path | None = None):
        self.path = resolve_db_path(path)

    # -- 基础 ------------------------------------------------------------
    @property
    def exists(self) -> bool:
        return self.path.is_file()

    def _connect(self) -> sqlite3.Connection | None:
        if not self.exists:
            return None
        try:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=10)
        except sqlite3.Error:
            return None
        conn.row_factory = sqlite3.Row
        return conn

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        conn = self._connect()
        if conn is None:
            return []
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        except sqlite3.Error:
            return []
        finally:
            conn.close()

    # -- 赛季 / 状态 ------------------------------------------------------
    def seasons(self, fmt: str | None = None, source: str = "pokechamdb") -> list[str]:
        if fmt:
            rows = self._query(
                "SELECT DISTINCT season FROM champion_usage WHERE source=? AND format=?",
                (source, fmt),
            )
        else:
            rows = self._query(
                "SELECT DISTINCT season FROM champion_usage WHERE source=?", (source,)
            )
        return sorted((r["season"] for r in rows), key=season_key, reverse=True)

    def latest_season(
        self, fmt: str | None = None, source: str = "pokechamdb"
    ) -> str | None:
        seasons = self.seasons(fmt, source)
        return seasons[0] if seasons else None

    def resolve_season(
        self, season: str | None, fmt: str | None = None, source: str = "pokechamdb"
    ) -> str | None:
        season = (season or "").strip()
        if season and season.lower() not in {"latest", "最新"}:
            return season
        return self.latest_season(fmt, source)

    def status(
        self,
        source: str | None = None,
        fmt: str | None = None,
        season: str | None = None,
    ) -> dict[str, Any]:
        """返回最近一次成功同步信息（本地写入时间 + 数据源声明时间）。"""
        clauses = ["status = 'ok'"]
        params: list[Any] = []
        if source:
            clauses.append("source = ?")
            params.append(source)
        if fmt:
            clauses.append("format = ?")
            params.append(fmt)
        if season:
            clauses.append("season = ?")
            params.append(season)
        rows = self._query(
            f"SELECT * FROM champion_sync_log WHERE {' AND '.join(clauses)} "
            "ORDER BY id DESC LIMIT 1",
            tuple(params),
        )
        if not rows:
            return {}
        row = rows[0]
        row["scope"] = row.get("scope")
        return row

    # -- 排名 ------------------------------------------------------------
    def top_usage(
        self,
        fmt: str = "single",
        season: str | None = None,
        source: str = "pokechamdb",
        limit: int = 10,
    ) -> list[dict]:
        season = self.resolve_season(season, fmt, source)
        if not season:
            return []
        return self._query(
            """
            SELECT u.*, p.name_zh, p.name_en, p.national_no
            FROM champion_usage u
            LEFT JOIN champion_pokemon p ON p.pokemon_id = u.pokemon_id
            WHERE u.format = ? AND u.season = ? AND u.source = ?
            ORDER BY CASE WHEN u.rank IS NULL THEN 1 ELSE 0 END,
                     u.rank, u.usage_pct DESC
            LIMIT ?
            """,
            (fmt, season, source, max(1, int(limit))),
        )

    def count(
        self,
        fmt: str = "single",
        season: str | None = None,
        source: str = "pokechamdb",
    ) -> int:
        """该格式该赛季榜单条目数。"""
        season = self.resolve_season(season, fmt, source)
        if not season:
            return 0
        rows = self._query(
            """SELECT COUNT(*) AS n FROM champion_usage
               WHERE format = ? AND season = ? AND source = ?""",
            (fmt, season, source),
        )
        return rows[0]["n"] if rows else 0

    def by_rank(
        self,
        rank: int,
        fmt: str = "single",
        season: str | None = None,
        source: str = "pokechamdb",
    ) -> dict | None:
        """按名次查宝可梦（该格式该赛季榜单第 N 名），并附榜单总数 total。"""
        season = self.resolve_season(season, fmt, source)
        if not season:
            return None
        rows = self._query(
            """
            SELECT u.*, p.name_zh, p.name_en, p.national_no
            FROM champion_usage u
            LEFT JOIN champion_pokemon p ON p.pokemon_id = u.pokemon_id
            WHERE u.format = ? AND u.season = ? AND u.source = ? AND u.rank = ?
            LIMIT 1
            """,
            (fmt, season, source, int(rank)),
        )
        if not rows:
            return None
        row = rows[0]
        row["total"] = self.count(fmt, season, source)
        return row

    # -- 单只宝可梦定位 ---------------------------------------------------
    def resolve_pokemon(
        self,
        keyword: str,
        fmt: str = "single",
        season: str | None = None,
        source: str = "pokechamdb",
    ) -> dict | None:
        """按 slug / 中文名 / 英文名定位（限该赛季榜单内的宝可梦）。"""
        season = self.resolve_season(season, fmt, source)
        if not season or not keyword:
            return None
        kw = keyword.strip()
        rows = self._query(
            """
            SELECT u.*, p.name_zh, p.name_en, p.national_no
            FROM champion_usage u
            LEFT JOIN champion_pokemon p ON p.pokemon_id = u.pokemon_id
            WHERE u.format = ? AND u.season = ? AND u.source = ?
            """,
            (fmt, season, source),
        )
        low = kw.lower()
        for row in rows:
            if (row.get("pokemon_id") or "").lower() == low:
                return row
        for row in rows:
            names = {
                (row.get("name_zh") or "").lower(),
                (row.get("name_en") or "").lower(),
            }
            if low in names:
                return row
        for row in rows:
            if (
                low in (row.get("name_zh") or "").lower()
                or low in (row.get("pokemon_id") or "").lower()
            ):
                return row
        # 近似名兜底：图鉴库与 PokéChamp DB 译名偶有差异（谜拟丘 vs 谜拟Ｑ）
        names = {r["name_zh"] for r in rows if r.get("name_zh")}
        for close in difflib.get_close_matches(keyword, names, n=1, cutoff=0.6):
            for row in rows:
                if row.get("name_zh") == close:
                    return row
        return None

    def pokemon_ranks(
        self,
        keyword: str,
        season: str | None = None,
        source: str = "pokechamdb",
    ) -> list[dict]:
        """查某宝可梦在单打/双打的排名，返回 [{format, rank, usage_pct, total, ...}]。

        season 留空取各格式本地最新赛季；某格式榜单没有该宝可梦时不返回该格式。
        """
        out: list[dict] = []
        for fmt in ("single", "double"):
            row = self.resolve_pokemon(keyword, fmt, season, source)
            if not row:
                continue
            out.append(
                {
                    "format": fmt,
                    "season": row["season"],
                    "rank": row["rank"],
                    "usage_pct": row.get("usage_pct"),
                    "total": self.count(fmt, row["season"], source),
                    "name_zh": row.get("name_zh"),
                    "pokemon_id": row["pokemon_id"],
                }
            )
        return out

    # -- 配置使用率 -------------------------------------------------------
    def pokemon_options(
        self,
        pokemon_id: str,
        fmt: str = "single",
        season: str | None = None,
        option_type: str = "move",
        top_n: int = 10,
        source: str = "pokechamdb",
    ) -> list[dict]:
        season = self.resolve_season(season, fmt, source)
        if not season:
            return []
        return self._query(
            """
            SELECT o.*
            FROM champion_option o
            JOIN champion_usage u ON u.usage_id = o.usage_id
            WHERE u.pokemon_id = ? AND u.format = ? AND u.season = ?
              AND u.source = ? AND o.option_type = ?
            ORDER BY CASE WHEN o.usage_pct IS NULL THEN 1 ELSE 0 END,
                     o.usage_pct DESC, o.rank, o.option_id
            LIMIT ?
            """,
            (pokemon_id, fmt, season, source, option_type, max(1, int(top_n))),
        )

    def option_types(
        self,
        pokemon_id: str,
        fmt: str = "single",
        season: str | None = None,
        source: str = "pokechamdb",
    ) -> list[str]:
        season = self.resolve_season(season, fmt, source)
        if not season:
            return []
        rows = self._query(
            """
            SELECT DISTINCT o.option_type
            FROM champion_option o
            JOIN champion_usage u ON u.usage_id = o.usage_id
            WHERE u.pokemon_id = ? AND u.format = ? AND u.season = ? AND u.source = ?
            """,
            (pokemon_id, fmt, season, source),
        )
        order = list(OPTION_LABELS)
        return sorted(
            (r["option_type"] for r in rows),
            key=lambda t: order.index(t) if t in order else 99,
        )

    # -- 反查 ------------------------------------------------------------
    def find_pokemon_by_item(
        self,
        item_name: str,
        fmt: str = "single",
        season: str | None = None,
        source: str = "pokechamdb",
        limit: int = 15,
    ) -> list[dict]:
        season = self.resolve_season(season, fmt, source)
        if not season:
            return []
        return self._query(
            """
            SELECT p.name_zh, p.pokemon_id, u.rank, u.usage_pct AS pokemon_usage_pct,
                   o.option_value AS item, o.usage_pct AS item_usage_pct
            FROM champion_option o
            JOIN champion_usage u ON u.usage_id = o.usage_id
            LEFT JOIN champion_pokemon p ON p.pokemon_id = u.pokemon_id
            WHERE o.option_type = 'item' AND o.option_value LIKE ?
              AND u.format = ? AND u.season = ? AND u.source = ?
            ORDER BY o.usage_pct DESC
            LIMIT ?
            """,
            (f"%{item_name}%", fmt, season, source, max(1, int(limit))),
        )

    # -- 趋势 ------------------------------------------------------------
    def trend(
        self,
        pokemon_id: str,
        fmt: str = "single",
        option_type: str | None = None,
        option_value: str | None = None,
        source: str = "pokechamdb",
    ) -> list[dict]:
        rows = self._query(
            """
            SELECT u.season, u.rank, u.usage_pct,
                   o.option_value, o.usage_pct AS option_usage_pct
            FROM champion_usage u
            LEFT JOIN champion_option o
                ON o.usage_id = u.usage_id AND o.option_type = ?
                   AND (? IS NULL OR o.option_value = ?)
            WHERE u.pokemon_id = ? AND u.format = ? AND u.source = ?
            ORDER BY u.season
            """,
            (option_type or "", option_value, option_value, pokemon_id, fmt, source),
        )
        return sorted(rows, key=lambda r: season_key(r["season"]))

    # -- 队伍（可选） -----------------------------------------------------
    def team_snapshots(
        self,
        fmt: str = "single",
        season: str | None = None,
        min_rating: float | None = None,
        source: str = "pokedb",
        limit: int = 10,
    ) -> list[dict]:
        season = self.resolve_season(season, fmt, source)
        if not season:
            return []
        sql = "SELECT * FROM champion_team WHERE format=? AND season=? AND source=?"
        params: list[Any] = [fmt, season, source]
        if min_rating is not None:
            sql += " AND rating_value >= ?"
            params.append(min_rating)
        sql += " ORDER BY CASE WHEN rank IS NULL THEN 1 ELSE 0 END, rank LIMIT ?"
        params.append(max(1, int(limit)))
        return self._query(sql, tuple(params))

    def database_summary(self) -> dict[str, Any]:
        if not self.exists:
            return {"exists": False, "path": str(self.path)}
        row = self._query("PRAGMA user_version")
        counts = {}
        for table in (
            "champion_pokemon",
            "champion_usage",
            "champion_option",
            "champion_team",
            "champion_sync_log",
        ):
            r = self._query(f"SELECT COUNT(*) AS n FROM {table}")
            counts[table] = r[0]["n"] if r else 0
        return {
            "exists": True,
            "path": str(self.path),
            **counts,
            "user_version": row[0]["user_version"] if row else None,
        }
