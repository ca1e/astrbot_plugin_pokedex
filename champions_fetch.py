#!/usr/bin/env python3
"""champions_fetch.py — 宝可梦冠军赛季数据独立抓取脚本。

插件运行时只读 champions.db，本脚本负责把数据写进去（Playwright headless）。
默认写入 AstrBot 数据目录 data/plugin_data/astrbot_plugin_pokedex/champions.db，
与插件 champions_db.resolve_db_path() 保持一致；也可用 --db 指定。

用法（在插件目录，建议 uv venv）::

    uv pip install -r requirements-scraper.txt
    uv run playwright install chromium

    uv run python champions_fetch.py                     # 最新赛季 单打+双打，详情取 Top20
    uv run python champions_fetch.py --season M-7 --format single
    uv run python champions_fetch.py --top 50            # 多抓一些详情
    uv run python champions_fetch.py --all               # 全部宝可梦详情（较慢，约 0.5h）
    uv run python champions_fetch.py --with-opendata     # 附带开放数据（使用率/上位队伍）
    uv run python champions_fetch.py --force             # 忽略"未更新则跳过"
    uv run python champions_fetch.py --pokemon garchomp  # 只补一只详情（调试）

数据源：
  * PokéChamp DB https://pokechamdb.com （必抓：排名 + 招式/道具/特性/性格/EV/队友）
  * poke.itlibra.com 开放数据（可选：月度使用率百分比，PokéChamp 不提供 %）
  * champs.pokedb.tokyo 开放数据（可选：上位队伍快照）

频率约定：每日一次。脚本会比对页面"最后更新"时间戳，相同则跳过写入。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import socket
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from champions_db import SCHEMA, resolve_db_path

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
BASE = "https://pokechamdb.com"
LANG = "zh-Hans"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 30
RETRIES = 2
DETAIL_DELAY = (0.8, 1.6)  # 详情页之间的礼貌延时（秒）
PROBE_PORTS = (7890, 7897, 34523, 1087, 1080, 8118)  # 常见本机代理端口

ITLIBRA_URLS = {
    "single": "https://poke.itlibra.com/opendata/usage-championssingles.json",
    "double": "https://poke.itlibra.com/opendata/usage-championsdoubles.json",
}
POKEDB_URL = "https://champs.pokedb.tokyo/opendata/s{no}_{fmt}_ranked_teams.json"

# ===========================================================================
# DOM 提取脚本（Playwright 在页面上下文执行）。
# 站点为 Next.js SSR，无公开 API；以下定位全部基于可见文本/链接形态，
# 不依赖易变 class。若站点改版，用 `uv run playwright codegen <url>` 或在
# headed 模式下 page.pause() 对照 Elements 面板修正。
# ===========================================================================
JS_RANKING = r"""
() => {
  const rows = [];
  document.querySelectorAll('a[href*="/pokemon/"]').forEach(a => {
    const m = (a.getAttribute('href') || '').match(/\/pokemon\/([^?\/]+)/);
    if (!m) return;
    let rank = null, name = null;
    Array.from(a.querySelectorAll('span')).forEach(sp => {
      const t = (sp.textContent || '').trim();
      if (rank === null && /^\d+$/.test(t)) { rank = parseInt(t, 10); return; }
      if (sp.className && sp.className.indexOf('truncate') !== -1 && t) name = t;
    });
    const img = a.querySelector('img');
    const alt = img ? (img.getAttribute('alt') || null) : null;
    rows.push({ slug: m[1], rank, name: name || alt });
  });
  return rows;
}
"""

JS_CARDS = r"""
() => {
  const LABELS = { '招式': 'move', '道具': 'item', '特性': 'ability',
                   '性格': 'nature', '队友': 'teammate' };
  const out = {};
  const spans = Array.from(document.querySelectorAll('span'));
  for (const [label, type] of Object.entries(LABELS)) {
    const labelEl = spans.find(s => (s.textContent || '').trim() === label);
    if (!labelEl) continue;
    let card = labelEl;
    while (card && !card.querySelector('ul')) card = card.parentElement;
    if (!card) continue;
    const items = [];
    card.querySelectorAll('ul > li').forEach((li, idx) => {
      let name = null, pct = null, rank = null;
      Array.from(li.querySelectorAll('span')).forEach(sp => {
        const t = (sp.textContent || '').trim();
        if (!t) return;
        if (/^#\d+$/.test(t)) { rank = parseInt(t.slice(1), 10); return; }
        if (/^[\d.]+%$/.test(t)) { pct = parseFloat(t); return; }
        if (sp.className && sp.className.indexOf('truncate') !== -1) name = t;
      });
      if (name) items.push({ name, pct, rank, order: idx + 1 });
    });
    if (items.length) out[type] = items;
  }
  return out;
}
"""

JS_EV_TABLE = r"""
() => {
  const rows = [];
  for (const t of Array.from(document.querySelectorAll('table'))) {
    const heads = Array.from(t.querySelectorAll('thead th'))
      .map(th => (th.textContent || '').trim());
    if (heads.indexOf('HP') === -1 || heads.indexOf('使用率') === -1) continue;
    t.querySelectorAll('tbody tr').forEach(tr => {
      const c = Array.from(tr.querySelectorAll('td')).map(td => (td.textContent || '').trim());
      if (c.length >= 8) {
        rows.push({ rank: c[0], hp: c[1], atk: c[2], def: c[3],
                    spa: c[4], spd: c[5], spe: c[6], pct: c[7] });
      }
    });
    break;
  }
  return rows;
}
"""

JS_UPDATED = r"""
() => {
  for (const n of Array.from(document.querySelectorAll('p,span,div,time'))) {
    const t = (n.textContent || '').trim();
    if (t.length > 120) continue;
    const m = t.match(/(?:最后更新|更新)[^0-9]{0,20}(\d{4}\/\d{1,2}\/\d{1,2}\s+\d{1,2}:\d{2})/);
    if (m) return m[1];
  }
  return null;
}
"""

JS_SEASONS = r"""
() => {
  // 展开赛季下拉框后收集所有 season= 链接；失败返回空数组
  const btn = Array.from(document.querySelectorAll('button'))
    .find(b => /^M-\d+$/.test((b.textContent || '').trim()));
  if (!btn) return [];
  btn.click();
  return Array.from(document.querySelectorAll('a[href*="season="]'))
    .map(a => ((a.getAttribute('href') || '').match(/season=(M-\d+)/) || [])[1])
    .filter(Boolean);
}
"""

RE_UPDATED_FALLBACK = re.compile(
    r"最后更新[^0-9]{0,20}(\d{4}/\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2})"
)


class FetchError(RuntimeError):
    pass


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# 代理与 HTTP
# ---------------------------------------------------------------------------
def detect_proxy(explicit: str | None) -> str | None:
    if explicit == "none":
        return None
    if explicit:
        return explicit
    for var in ("POKEMON_TRACKER_PROXY", "HTTPS_PROXY", "https_proxy", "ALL_PROXY"):
        if os.environ.get(var):
            val = os.environ[var].strip()
            return None if val.lower() in {"none", "direct"} else val
    for port in PROBE_PORTS:  # 本机常见代理端口探测（未开放则直连）
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return f"http://127.0.0.1:{port}"
        except OSError:
            continue
    return None


def http_get_json(url: str, proxy: str | None, timeout: int = REQUEST_TIMEOUT):
    handlers = (
        [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else []
    )
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "application/json"}
    )
    last_exc: Exception | None = None
    for attempt in range(RETRIES + 1):
        try:
            with opener.open(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last_exc = e
            if attempt < RETRIES:
                time.sleep(2 * (attempt + 1))
    raise FetchError(f"GET {url} 失败: {last_exc}")


# ---------------------------------------------------------------------------
# 浏览器会话
# ---------------------------------------------------------------------------
class Browser:
    def __init__(
        self, proxy: str | None, headless: bool = True, timeout_ms: int | None = None
    ):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        kwargs = {"headless": headless}
        if proxy:
            kwargs["proxy"] = {"server": proxy}
        self._browser = self._pw.chromium.launch(**kwargs)
        self._ctx = self._browser.new_context(user_agent=UA, locale="zh-CN")
        self._ctx.set_default_timeout(timeout_ms or REQUEST_TIMEOUT * 1000)
        log(f"浏览器就绪（proxy={proxy or '直连'}）")

    def goto(self, url: str, wait_sel: str):
        """导航并等待关键区块；超时/网络错误统一转为 FetchError（可重试）。"""
        page = self._ctx.new_page()
        try:
            page.goto(
                url, wait_until="domcontentloaded", timeout=REQUEST_TIMEOUT * 1000
            )
            page.locator(wait_sel).first.wait_for(state="visible", timeout=15000)
        except Exception as e:  # Playwright TimeoutError 等一并转为 FetchError
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass
            raise FetchError(f"打开页面失败：{e}") from e
        return page

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self):
        try:
            self._ctx.close()
            self._browser.close()
            self._pw.stop()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 写库
# ---------------------------------------------------------------------------
def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def upsert_pokemon(conn, pokemon_id, name_zh=None, name_en=None, national_no=None):
    if not pokemon_id:
        return
    conn.execute(
        """INSERT INTO champion_pokemon (pokemon_id, name_zh, name_en, national_no)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(pokemon_id) DO UPDATE SET
             name_zh = COALESCE(excluded.name_zh, champion_pokemon.name_zh),
             name_en = COALESCE(excluded.name_en, champion_pokemon.name_en),
             national_no = COALESCE(excluded.national_no, champion_pokemon.national_no)""",
        (pokemon_id, name_zh, name_en, national_no),
    )


def upsert_usage(
    conn,
    *,
    pokemon_id,
    fmt,
    season,
    source,
    rank=None,
    usage_pct=None,
    battle_count=None,
    updated_at=None,
) -> int:
    upsert_pokemon(conn, pokemon_id)
    conn.execute(
        """INSERT INTO champion_usage
             (pokemon_id, format, season, source, usage_pct, rank, battle_count, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(pokemon_id, format, season, source) DO UPDATE SET
             usage_pct = COALESCE(excluded.usage_pct, champion_usage.usage_pct),
             rank = COALESCE(excluded.rank, champion_usage.rank),
             battle_count = COALESCE(excluded.battle_count, champion_usage.battle_count),
             updated_at = COALESCE(excluded.updated_at, champion_usage.updated_at)""",
        (pokemon_id, fmt, season, source, usage_pct, rank, battle_count, updated_at),
    )
    return conn.execute(
        """SELECT usage_id FROM champion_usage
           WHERE pokemon_id=? AND format=? AND season=? AND source=?""",
        (pokemon_id, fmt, season, source),
    ).fetchone()[0]


def replace_options(conn, usage_id, options):
    conn.execute("DELETE FROM champion_option WHERE usage_id=?", (usage_id,))
    seen, payload = set(), []
    for opt in options:
        key = (opt["option_type"], opt["option_value"])
        if key in seen:
            continue
        seen.add(key)
        extra = opt.get("extra")
        payload.append(
            (
                usage_id,
                opt["option_type"],
                opt["option_value"],
                opt.get("usage_pct"),
                opt.get("rank"),
                json.dumps(extra, ensure_ascii=False) if extra else None,
            )
        )
    if payload:
        conn.executemany(
            """INSERT INTO champion_option
                 (usage_id, option_type, option_value, usage_pct, rank, extra)
               VALUES (?, ?, ?, ?, ?, ?)""",
            payload,
        )
    return len(payload)


def log_sync(
    conn,
    *,
    source,
    scope,
    status,
    fmt=None,
    season=None,
    source_updated_at=None,
    message=None,
    items_written=0,
):
    conn.execute(
        """INSERT INTO champion_sync_log
             (source, format, season, scope, source_updated_at, status,
              message, items_written, run_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            source,
            fmt,
            season,
            scope,
            source_updated_at,
            status,
            message,
            items_written,
            utcnow(),
        ),
    )


def last_sync(conn, source, fmt, season, scope):
    row = conn.execute(
        """SELECT source_updated_at, run_at FROM champion_sync_log
           WHERE source=? AND format=? AND season=? AND scope=?
             AND status='ok' ORDER BY id DESC LIMIT 1""",
        (source, fmt, season, scope),
    ).fetchone()
    return row


# ---------------------------------------------------------------------------
# 抓取：PokéChamp DB
# ---------------------------------------------------------------------------
def latest_season_id(br: Browser, fallback: str) -> str:
    """从赛季下拉框读取可选赛季，取编号最大的；失败回退默认值。"""
    try:
        page = br.goto(
            f"{BASE}/{LANG}?view=pokemon&format=single", 'a[href*="/pokemon/"]'
        )
        seasons = [s for s in (page.evaluate(JS_SEASONS) or []) if s]
        page.close()
        nums = [
            int(m.group(1))
            for s in seasons
            if (m := re.fullmatch(r"M-(\d+)", s, re.IGNORECASE))
        ]
        if nums:
            best = f"M-{max(nums)}"
            log(f"站点最新赛季：{best}")
            return best
    except Exception as e:  # noqa: BLE001
        log(f"赛季探测失败（{e}），回退 {fallback}")
    return fallback


def _with_retries(fn, label: str, attempts: int = RETRIES + 1):
    """统一重试包装：失败退避 3s/6s 后再试，最后一次仍失败抛 FetchError。"""
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except FetchError as e:
            last = e
            if i < attempts - 1:
                wait = 3 * (i + 1)
                log(f"{label} 第{i + 1}次失败（{e}），{wait}s 后重试")
                time.sleep(wait)
    raise FetchError(f"{label} 重试 {attempts} 次后仍失败：{last}")


def scrape_ranking(br: Browser, season: str, fmt: str) -> dict:
    def once() -> dict:
        url = f"{BASE}/{LANG}?format={fmt}&season={season}&view=pokemon"
        page = br.goto(url, 'a[href*="/pokemon/"]')
        try:
            rows = page.evaluate(JS_RANKING) or []
            updated = page.evaluate(JS_UPDATED) or None
            if not updated:
                m = RE_UPDATED_FALLBACK.search(page.content())
                updated = m.group(1) if m else None
        finally:
            page.close()

        items, seen = [], set()
        for r in rows:
            slug = (r.get("slug") or "").strip()
            if not slug or slug in seen:
                continue
            seen.add(slug)
            items.append(
                {
                    "pokemon_id": slug,
                    "name_zh": (r.get("name") or "").strip() or None,
                    "rank": r.get("rank"),
                }
            )
        items.sort(key=lambda r: (r["rank"] is None, r["rank"] or 0))
        if not items:
            raise FetchError(f"排名页解析为空：{url}（可能限流或页面改版）")
        log(f"排名 [{fmt}/{season}] {len(items)} 只，站点更新 {updated or '未知'}")
        return {"items": items, "updated_at": updated}

    return _with_retries(once, f"排名 [{fmt}/{season}]")


def scrape_detail(br: Browser, pokemon_id: str, season: str, fmt: str) -> dict:
    def once() -> dict:
        url = f"{BASE}/{LANG}/pokemon/{pokemon_id}?season={season}&format={fmt}"
        page = br.goto(url, "text=招式")
        try:
            if fmt == "double":
                # 详情页 SSR 固定输出单打数据，客户端水合后才按 format 参数
                # 重渲染为双打。SSR HTML 中没有「双打」字样，因此它出现即代表
                # React 已用双打数据完成重渲染（同一次 commit，数据同步就绪）。
                page.locator("text=双打").first.wait_for(state="visible", timeout=15000)
            cards = page.evaluate(JS_CARDS) or {}
            ev_rows = page.evaluate(JS_EV_TABLE) or []
            updated = page.evaluate(JS_UPDATED) or None
            # meta description 是 SSR 的单打数据，仅单打格式可用作排名兜底
            desc = (
                page.evaluate(
                    "() => { const m = document.querySelector('meta[name=\"description\"]');"
                    " return m ? (m.getAttribute('content') || '') : ''; }"
                )
                if fmt == "single"
                else ""
            )
            html = "" if updated else page.content()
        finally:
            page.close()
        if not updated and html:
            m = RE_UPDATED_FALLBACK.search(html)
            updated = m.group(1) if m else None
        if not cards and not ev_rows:
            raise FetchError(f"详情页无配置数据：{pokemon_id}（可能限流或页面改版）")
        return cards, ev_rows, updated, desc

    cards, ev_rows, updated, desc = _with_retries(
        once, f"详情 {pokemon_id} [{fmt}/{season}]"
    )

    out: dict = {
        "updated_at": updated,
        "name_zh": None,
        "rank": None,
        "moves": [],
        "items": [],
        "abilities": [],
        "natures": [],
        "teammates": [],
    }
    # JS 端键为单数（move/item/...），Python 端键为复数
    for key, jkey in (
        ("moves", "move"),
        ("items", "item"),
        ("abilities", "ability"),
        ("natures", "nature"),
        ("teammates", "teammate"),
    ):
        for r in cards.get(jkey, []):
            out[key].append(
                {"name": r["name"], "usage_pct": r.get("pct"), "rank": r.get("rank")}
            )
    evs = []
    for r in ev_rows:
        try:
            nums = [
                int(float(r[k] or 0)) for k in ("hp", "atk", "def", "spa", "spd", "spe")
            ]
            pct = float(str(r.get("pct", "")).rstrip("%")) if r.get("pct") else None
        except (TypeError, ValueError):
            continue
        rank = int(r["rank"]) if str(r.get("rank", "")).isdigit() else None
        evs.append(
            {
                "value": "-".join(map(str, nums)),
                "usage_pct": pct,
                "rank": rank,
                "extra": dict(zip(("hp", "atk", "def", "spa", "spd", "spe"), nums)),
            }
        )
    out["ev_spreads"] = evs

    # meta description 含「使用率第N名」，补充排名（排名页兜底）
    m = re.search(r"使用率第(\d+)名", desc or "")
    if m:
        out["rank"] = int(m.group(1))
    return out


def write_detail(conn, detail: dict, *, pokemon_id: str, season: str, fmt: str) -> int:
    usage_id = upsert_usage(
        conn,
        pokemon_id=pokemon_id,
        fmt=fmt,
        season=season,
        source="pokechamdb",
        rank=detail.get("rank"),
        updated_at=detail.get("updated_at"),
    )
    options = []
    for key, otype in (
        ("moves", "move"),
        ("items", "item"),
        ("abilities", "ability"),
        ("natures", "nature"),
    ):
        for r in detail.get(key, []):
            options.append(
                {
                    "option_type": otype,
                    "option_value": r["name"],
                    "usage_pct": r.get("usage_pct"),
                    "rank": r.get("rank") or r.get("order"),
                }
            )
    for r in detail.get("ev_spreads", []):
        options.append(
            {
                "option_type": "ev_spread",
                "option_value": r["value"],
                "usage_pct": r.get("usage_pct"),
                "rank": r.get("rank"),
                "extra": r.get("extra"),
            }
        )
    for r in detail.get("teammates", []):
        options.append(
            {
                "option_type": "teammate",
                "option_value": r["name"],
                "usage_pct": r.get("usage_pct"),
                "rank": r.get("rank"),
            }
        )
    return replace_options(conn, usage_id, options)


# ---------------------------------------------------------------------------
# 开放数据（可选）
# ---------------------------------------------------------------------------
def sync_itlibra(conn, fmt: str, proxy: str | None) -> tuple[int, int]:
    """月度使用率（含百分比）。season 用数据源的月份（如 2026-09），source=itlibra。"""
    url = ITLIBRA_URLS[fmt]
    data = http_get_json(url, proxy)
    month = data.get("month") or "unknown"
    count = 0
    for p in data.get("pokemon", []):
        usage_id = upsert_usage(
            conn,
            pokemon_id=p.get("id") or "",
            fmt=fmt,
            season=month,
            source="itlibra",
            rank=p.get("rank"),
            usage_pct=p.get("usage"),
        )
        options = []
        for key, otype in (
            ("moves", "move"),
            ("items", "item"),
            ("abilities", "ability"),
        ):
            for r in p.get(key) or []:
                options.append(
                    {
                        "option_type": otype,
                        "option_value": r.get("id") or "",
                        "usage_pct": r.get("pct"),
                    }
                )
        for r in p.get("spreads") or []:
            evs = r.get("evs") or []
            if len(evs) == 6:
                options.append(
                    {
                        "option_type": "ev_spread",
                        "option_value": "-".join(str(int(x)) for x in evs),
                        "usage_pct": r.get("pct"),
                        "extra": {"nature": r.get("nature")},
                    }
                )
        for i, t in enumerate(p.get("teammates") or [], 1):
            options.append(
                {"option_type": "teammate", "option_value": str(t), "rank": i}
            )
        replace_options(conn, usage_id, options)
        count += 1
    log_sync(
        conn,
        source="itlibra",
        fmt=fmt,
        season=month,
        scope="usage",
        status="ok",
        source_updated_at=month,
        items_written=count,
    )
    return count, 0


def sync_pokedb_teams(conn, season: str, fmt: str, proxy: str | None) -> int:
    m = re.fullmatch(r"M-(\d+)", season, re.IGNORECASE)
    if not m:
        return 0
    url = POKEDB_URL.format(no=int(m.group(1)), fmt=fmt)
    try:
        data = http_get_json(url, proxy)
    except FetchError as e:
        log(f"pokedb 队伍跳过：{e}")
        return 0
    season_no = data.get("season_number")
    team_n = 0
    conn.execute(
        "DELETE FROM champion_team WHERE format=? AND season=? AND source='pokedb'",
        (fmt, season),
    )
    for t in data.get("teams", []):
        cur = conn.execute(
            """INSERT INTO champion_team
                 (format, season, rank, rating_value, source, season_number)
               VALUES (?, ?, ?, ?, 'pokedb', ?)""",
            (fmt, season, t.get("rank"), t.get("rating_value"), season_no),
        )
        team_n += 1
        for i, mem in enumerate((t.get("team") or [])[:6], 1):
            conn.execute(
                """INSERT INTO champion_team_member
                     (team_id, slot, pokemon_id, name_raw, source_id, terastal, item)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    cur.lastrowid,
                    i,
                    None,
                    mem.get("pokemon"),
                    mem.get("id"),
                    mem.get("terastal") or None,
                    mem.get("item") or None,
                ),
            )
    log_sync(
        conn,
        source="pokedb",
        fmt=fmt,
        season=season,
        scope="teams",
        status="ok",
        items_written=team_n,
    )
    return team_n


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run(args) -> int:
    db_path = resolve_db_path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    proxy = detect_proxy(args.proxy)
    formats = ["single", "double"] if args.format == "both" else [args.format]
    summary: list[str] = []

    with Browser(proxy, headless=not args.headed) as br:
        season = args.season
        if season.lower() in {"latest", "最新", ""}:
            season = latest_season_id(br, "M-7")

        for fmt in formats:
            # ---- 排名 ----
            scope = "ranking"
            prev = last_sync(conn, "pokechamdb", fmt, season, scope)
            try:
                data = scrape_ranking(br, season, fmt)
                if (
                    prev
                    and not args.force
                    and prev["source_updated_at"]
                    and prev["source_updated_at"] == data["updated_at"]
                ):
                    log_sync(
                        conn,
                        source="pokechamdb",
                        fmt=fmt,
                        season=season,
                        scope=scope,
                        status="skipped",
                        source_updated_at=data["updated_at"],
                        message="页面时间戳未变化，跳过",
                    )
                    log(f"[{fmt}/{season}] 排名未更新，跳过写入")
                else:
                    with conn:
                        for it in data["items"]:
                            upsert_pokemon(conn, it["pokemon_id"], it["name_zh"])
                            upsert_usage(
                                conn,
                                pokemon_id=it["pokemon_id"],
                                fmt=fmt,
                                season=season,
                                source="pokechamdb",
                                rank=it["rank"],
                                updated_at=data["updated_at"],
                            )
                        log_sync(
                            conn,
                            source="pokechamdb",
                            fmt=fmt,
                            season=season,
                            scope=scope,
                            status="ok",
                            source_updated_at=data["updated_at"],
                            items_written=len(data["items"]),
                        )
                    summary.append(f"{fmt} 排名 {len(data['items'])} 只")
            except Exception as e:  # FetchError 已含重试；其余异常兜底继续
                log(f"[{fmt}/{season}] 排名抓取失败：{e}")
                with conn:
                    log_sync(
                        conn,
                        source="pokechamdb",
                        fmt=fmt,
                        season=season,
                        scope=scope,
                        status="failed",
                        message=str(e)[:500],
                    )
                continue  # 排名失败则详情也没法按名次截取

            # ---- 详情 ----
            # 单双打详情都抓：详情页 SSR 固定单打，双打数据在水合后渲染，
            # scrape_detail 内部对 double 额外等待「双打」标记出现。
            targets = data["items"] if args.all else data["items"][: args.top]
            ok = fail = 0
            for it in targets:
                pid = it["pokemon_id"]
                scope = f"detail:{pid}"
                prev = last_sync(conn, "pokechamdb", fmt, season, scope)
                try:
                    detail = scrape_detail(br, pid, season, fmt)
                    if (
                        prev
                        and not args.force
                        and not args.force_detail
                        and prev["source_updated_at"] == detail.get("updated_at")
                    ):
                        continue  # 未变化（--force-detail 时强制重抓）
                    with conn:
                        n = write_detail(
                            conn, detail, pokemon_id=pid, season=season, fmt=fmt
                        )
                        log_sync(
                            conn,
                            source="pokechamdb",
                            fmt=fmt,
                            season=season,
                            scope=scope,
                            status="ok",
                            source_updated_at=detail.get("updated_at"),
                            items_written=n,
                        )
                    ok += 1
                except FetchError as e:
                    log(f"详情 {pid} 失败：{e}")
                    with conn:
                        log_sync(
                            conn,
                            source="pokechamdb",
                            fmt=fmt,
                            season=season,
                            scope=scope,
                            status="failed",
                            message=str(e),
                        )
                    fail += 1
                except Exception as e:  # noqa: BLE001
                    log(f"详情 {pid} 异常：{e}")
                    with conn:
                        log_sync(
                            conn,
                            source="pokechamdb",
                            fmt=fmt,
                            season=season,
                            scope=scope,
                            status="failed",
                            message=str(e)[:500],
                        )
                    fail += 1
                time.sleep(random.uniform(*DETAIL_DELAY))
            log(f"[{fmt}/{season}] 详情完成：成功 {ok}，失败 {fail}")
            summary.append(f"{fmt} 详情 {ok} 只")

            # ---- 可选开放数据 ----
            if args.with_opendata:
                try:
                    with conn:
                        n, _ = sync_itlibra(conn, fmt, proxy)
                    summary.append(f"{fmt} itlibra 使用率 {n} 只")
                except FetchError as e:
                    log(f"itlibra [{fmt}] 失败（跳过）：{e}")
                try:
                    with conn:
                        n = sync_pokedb_teams(conn, season, fmt, proxy)
                    summary.append(f"{fmt} pokedb 队伍 {n} 支")
                except FetchError as e:
                    log(f"pokedb [{fmt}] 失败（跳过）：{e}")

    conn.close()
    print("\n=== 抓取完成 ===")
    print(f"赛季 {season} ｜ 数据库 {db_path}")
    for s in summary:
        print(f"  · {s}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="宝可梦冠军赛季数据抓取（写入 champions.db）"
    )
    ap.add_argument("--season", default="latest", help="赛季，如 M-7；默认自动探测最新")
    ap.add_argument("--format", default="both", choices=["single", "double", "both"])
    ap.add_argument("--top", type=int, default=20, help="抓取详情的排名前 N（默认 20）")
    ap.add_argument("--all", action="store_true", help="抓取榜单全部宝可梦详情（较慢）")
    ap.add_argument("--pokemon", help="只补一只详情（调试），如 garchomp")
    ap.add_argument(
        "--with-opendata",
        action="store_true",
        help="附带 itlibra 使用率与 pokedb 上位队伍",
    )
    ap.add_argument("--db", help="champions.db 路径（默认 AstrBot plugin_data 目录）")
    ap.add_argument("--proxy", help="代理地址；none=直连；默认自动探测本机代理")
    ap.add_argument("--force", action="store_true", help="忽略未更新跳过（排名+详情）")
    ap.add_argument(
        "--force-detail",
        action="store_true",
        help="排名未更新时依然强制重抓全部配置明细",
    )
    ap.add_argument("--headed", action="store_true", help="显示浏览器窗口（调试）")
    args = ap.parse_args()

    if args.pokemon:
        db_path = resolve_db_path(args.db)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path, timeout=30)
        conn.executescript(SCHEMA)
        season = (
            args.season if re.fullmatch(r"M-\d+", args.season, re.IGNORECASE) else "M-7"
        )
        fmt = args.format if args.format != "both" else "single"
        with Browser(detect_proxy(args.proxy), headless=not args.headed) as br:
            detail = scrape_detail(br, args.pokemon, season, fmt)
        with conn:
            n = write_detail(
                conn, detail, pokemon_id=args.pokemon, season=season, fmt=fmt
            )
            log_sync(
                conn,
                source="pokechamdb",
                fmt=fmt,
                season=season,
                scope=f"detail:{args.pokemon}",
                status="ok",
                source_updated_at=detail.get("updated_at"),
                items_written=n,
            )
        conn.close()
        print(f"已写入 {args.pokemon}（{fmt}/{season}）{n} 项配置 → {db_path}")
        return 0

    return run(args)


if __name__ == "__main__":
    sys.exit(main())
