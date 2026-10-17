"""本地 TSV 知识库查询（数据由 pokemon-dataset-zh 项目 build_render.py 生成，
https://github.com/42arch/pokemon-dataset-zh）。

查询逻辑与技能版 scripts/query.py 同源：纯数字关键词按 id/id_raw/national_id
精确匹配，其他关键词按名称列（中/日/英）大小写不敏感子串匹配。
"""

import csv
import difflib
import re
from pathlib import Path
from typing import ClassVar

try:
    from .battle_engine import DEFENSIVE_ABILITY_IMMUNITY
except ImportError:
    from battle_engine import DEFENSIVE_ABILITY_IMMUNITY

DATA_DIR = Path(__file__).resolve().parent / "data"

TABLES = {
    "simple": "simple_pokedex.tsv",
    "pokemon": "pokemon.tsv",
    "ability": "ability_list.tsv",
    "abilities": "abilities.tsv",
    "move": "move_list.tsv",
    "moves": "moves.tsv",
    "item": "item_list.tsv",
    "pokedex": "pokedex.tsv",
    "types": "types.tsv",
    "wishmaker": "wishmaker.tsv",
}
SEARCH_TABLES = ["simple", "pokemon", "ability", "move", "item", "pokedex"]
ID_COLS = ("id", "id_raw", "national_id")
NAME_COLS = ("name_zh", "name_ja", "name_jp", "name_en", "name", "form_name", "dex")
SUMMARY_COLS = (
    "id",
    "id_raw",
    "national_id",
    "dex",
    "category_path",
    "name_zh",
    "form_name",
    "name_ja",
    "name_jp",
    "name_en",
)

STAT_RE = re.compile(
    r"HP(\d+)/攻击(\d+)/防御(\d+)/特攻(\d+)/特防(\d+)/速度(\d+)（总和(\d+)）"
)


def _match_row(row, keyword, mode=None):
    if mode == "id" or (mode is None and keyword.isdigit()):
        norm = str(int(keyword))
        return any(row.get(c) in (keyword, norm) for c in ID_COLS if row.get(c))
    if mode in ("name", None):
        kw = keyword.lower()
        return any(kw in (row.get(c) or "").lower() for c in NAME_COLS if c in row)
    return False


class PokedexDB:
    """对打包在插件 data/ 目录下 TSV 的只读查询。"""

    def __init__(self, data_dir: Path = DATA_DIR):
        self.data_dir = Path(data_dir)
        self._cache: dict[str, list[dict]] = {}
        self._chart: dict[tuple[str, str], float] | None = None
        self._move_index: dict[str, dict] | None = None

    def table(self, name: str) -> list[dict]:
        if name not in self._cache:
            with (self.data_dir / TABLES[name]).open(encoding="utf-8", newline="") as f:
                self._cache[name] = list(csv.DictReader(f, delimiter="\t"))
        return self._cache[name]

    def query(self, table: str, keyword: str, mode=None) -> list[dict]:
        return [r for r in self.table(table) if _match_row(r, keyword, mode)]

    def search(self, keyword: str) -> dict[str, list[dict]]:
        out = {}
        for t in SEARCH_TABLES:
            hits = self.query(t, keyword, mode="name")
            if hits:
                out[t] = hits
        return out

    # ---------- 便捷方法 ----------
    def pick_best(self, hits: list[dict], keyword: str) -> dict:
        """多行命中时优选：形态名精确命中 > 种族名/英文名精确 > 默认形态行。"""
        kw = keyword.strip().lower()

        def score(r: dict) -> int:
            s = 0
            if kw.isdigit() and (r.get("id") == str(int(kw)) or r.get("id_raw") == kw):
                s += 6
            fn = (r.get("form_name") or "").lower()
            if fn and fn == kw:
                s += 8
            if (r.get("name_zh") or "").lower() == kw or (
                r.get("name_en") or ""
            ).lower() == kw:
                s += 4
            if not r.get("form_name"):
                s += 2
            return s

        return max(hits, key=score)

    def pokemon(self, keyword: str):
        hits = self.query("pokemon", keyword)
        return self.pick_best(hits, keyword) if hits else None

    def by_type(self, type_name: str, limit: int = 40) -> list[dict]:
        """按属性筛选默认形态的宝可梦（types 列包含即命中，按编号排序）。"""
        hits = [
            r
            for r in self.table("pokemon")
            if not r.get("form_name") and type_name in (r.get("types") or "")
        ]
        hits.sort(key=lambda r: int(r["id"]) if r.get("id", "").isdigit() else 9999)
        return hits[:limit]

    def matchups(self, type_names: list[str]) -> dict | None:
        """防守属性组合 -> 弱点/抗性/免疫（基于本地 types.tsv 基准属性表）。"""
        chart = {
            (r["defender"], r["attacker"]): float(r["damage"])
            for r in self.table("types")
        }
        attackers = {a for _, a in chart}
        if not type_names or not all(t in attackers for t in type_names):
            return None
        out = {}
        for atk in sorted(attackers):
            m = 1.0
            for dt in type_names:
                m *= chart.get((dt, atk), 1.0)
            if m == 1:
                continue
            if m == 0:
                out.setdefault("immune", []).append(atk)
            else:
                key = "weak" if m > 1 else "resist"
                out.setdefault(key, []).append(f"{atk}×{m:g}")
        # 受击类特性修正：当前 >0× 的进攻属性中，有哪些可被特性免疫（如避雷针→电）
        corrections = []
        for ab, t in DEFENSIVE_ABILITY_IMMUNITY.items():
            if t not in attackers:
                continue
            m = 1.0
            for dt in type_names:
                m *= chart.get((dt, t), 1.0)
            if m > 0:
                corrections.append((t, ab, m))
        grouped: dict[str, tuple[float, list[str]]] = {}
        for t, ab, m in corrections:
            cur_m, abil = grouped.get(t, (m, []))
            grouped[t] = (cur_m, [*abil, ab])
        ability_corrections = [
            {"type": t, "current_mult": m, "abilities": abil}
            for t, (m, abil) in sorted(grouped.items(), key=lambda x: -x[1][0])
        ]
        return {
            "weak": out.get("weak", []),
            "resist": out.get("resist", []),
            "immune": out.get("immune", []),
            "ability_corrections": ability_corrections,
        }

    def ability_immunities(self, row: dict) -> list[tuple[str, str]]:
        """该宝可梦（含隐藏特性）可经受击类特性获得的 (免疫属性, 特性) 列表。

        属性表已免疫的属性不再重复列出。
        """
        has = {
            a
            for a in (
                (row.get("abilities") or "")
                + "、"
                + (row.get("hidden_abilities") or "")
            ).split("、")
            if a
        }
        existing = {t for t in (row.get("immune_to") or "").split("、") if t}
        out, seen = [], set()
        for ab, t in DEFENSIVE_ABILITY_IMMUNITY.items():
            if ab in has and t not in existing and t not in seen:
                out.append((t, ab))
                seen.add(t)
        return out

    # ---------- 属性相性（进攻方向 / 互克倍率） ----------
    def type_chart(self) -> dict[tuple[str, str], float]:
        """{(防守属性, 进攻属性): 倍率}，来自 types.tsv，缓存。"""
        if self._chart is None:
            self._chart = {
                (r["defender"], r["attacker"]): float(r["damage"])
                for r in self.table("types")
            }
        return self._chart

    def attack_matchups(self, type_name: str) -> dict | None:
        """进攻方向相性：该属性攻击各防守属性的倍率分组（super/weak/none）。"""
        chart = self.type_chart()
        if type_name not in {a for _, a in chart}:
            return None
        out = {"super": [], "weak": [], "none": []}
        for (dfn, atk), m in chart.items():
            if atk != type_name:
                continue
            if m > 1:
                out["super"].append(f"{dfn}×{m:g}")
            elif 0 < m < 1:
                out["weak"].append(f"{dfn}×{m:g}")
            elif m == 0:
                out["none"].append(dfn)
        return out

    def matchup_multiplier(self, atk_types: list[str], def_types: list[str]) -> float:
        """进攻属性组合打防守属性组合的倍率（逐项乘法合成）。"""
        chart = self.type_chart()
        m = 1.0
        for a in atk_types:
            for d in def_types:
                m *= chart.get((d, a), 1.0)
        return m

    def find_counters(self, keyword: str, limit: int = 8) -> dict | None:
        """推荐克制目标宝可梦的候选（双向验证：打目标 ≥2× 且被目标打 ≤1×）。

        只在默认形态中筛选，按 打击倍率、承伤倍率、种族值总和 排序。
        返回 {"target", "target_types", "counters": [...]}，查无此宝可梦返回 None。
        """
        target = self.pokemon(keyword)
        if not target:
            return None
        t_types = [t for t in (target.get("types") or "").split("/") if t]
        found = []
        for row in self.table("pokemon"):
            if row.get("form_name") or row.get("id") == target.get("id"):
                continue
            c_types = [t for t in (row.get("types") or "").split("/") if t]
            if not c_types:
                continue
            atk = self.matchup_multiplier(c_types, t_types)
            dfn = self.matchup_multiplier(t_types, c_types)
            if atk < 2 or dfn > 1:
                continue
            parsed = self.first_stats(row)
            found.append(
                {
                    "name": row.get("name_zh", ""),
                    "id": row.get("id_raw", ""),
                    "types": row.get("types", ""),
                    "atk_mult": atk,
                    "def_mult": dfn,
                    "total": parsed[1] if parsed else 0,
                }
            )
        found.sort(key=lambda r: (-r["atk_mult"], r["def_mult"], -r["total"]))
        return {
            "target": target.get("name_zh", ""),
            "target_types": target.get("types", ""),
            "counters": found[:limit],
        }

    # ---------- 招式表 / 配招 ----------
    # 对战引擎支持的状态招（名称与招式总表一致，供推荐配招优先选取）
    STATUS_MOVES: ClassVar[dict[str, str]] = {
        "电磁波": "麻痹",
        "麻痹粉": "麻痹",
        "磷火": "灼伤",
        "剧毒": "中毒",
        "毒粉": "中毒",
        "毒瓦斯": "中毒",
    }
    MOVE_LEVEL_RE = re.compile(r"^(.+?)[（(](\d{1,2}|—)[）)]$")

    def learnset(self, row: dict) -> dict:
        """解析学习招式表，返回 {"level_up": [(招式名, 等级|None)], "machine": [...], "egg": [...]}。

        兼容两种格式：新格式 "名称(等级)"（"—" 按初始招式计 1 级）与旧格式纯名称
        （等级 None，表示可学但等级未知）。单元格含多形态分组时仅取默认形态组；
        形态行回退同种族默认形态行。
        """
        src = row
        if row.get("form_name"):
            src = next(
                (
                    r
                    for r in self.table("pokemon")
                    if not r.get("form_name") and r.get("id") == row.get("id")
                ),
                row,
            )
        out: dict[str, list[tuple[str, int | None]]] = {
            "level_up": [],
            "machine": [],
            "egg": [],
        }
        for key in ("level_up_moves", "machine_moves", "egg_moves"):
            cell = (src.get(key) or "").strip()
            if not cell:
                continue
            grp = cell.split("；")[0]  # 默认形态组（无「XX形态：」前缀）
            for name in grp.split("、"):
                name = name.strip()
                if not name:
                    continue
                if key == "level_up_moves":
                    m = self.MOVE_LEVEL_RE.match(name)
                    if m:
                        lv = m.group(2)
                        out["level_up"].append(
                            (m.group(1), 1 if lv == "—" else int(lv))
                        )
                        continue
                out[key.replace("_moves", "")].append((name, None))
        return out

    def move_index(self) -> dict[str, dict]:
        """{招式中文名: move_list 行}，缓存（953 招全覆盖）。"""
        if self._move_index is None:
            self._move_index = {
                r["name_zh"]: r for r in self.table("move") if r.get("name_zh")
            }
        return self._move_index

    @staticmethod
    def move_power(move_row: dict) -> int | None:
        """招式威力数值（"变化"/"—"/空 返回 None）。"""
        m = re.search(r"\d+", move_row.get("power", "") or "")
        return int(m.group()) if m else None

    def recommended_moveset(self, row: dict) -> tuple[list[str], bool]:
        """推荐配招：学习表内高威力招优先（STAB 且属性分散）×3 + 状态招 ×1。

        状态招优先选对战引擎支持的（STATUS_MOVES）。返回 (招式列表, 是否全部
        来自学习表)；学习表为空时回退为全局同属性高威力招（第二个值为 False，
        调用方应标注"演示配招"）。
        """
        learn = self.learnset(row)
        p_types = [t for t in (row.get("types") or "").split("/") if t]
        idx = self.move_index()
        damage: list[tuple[str, int, str]] = []
        status: list[str] = []
        seen: set[str] = set()
        for name, _lv in learn["level_up"] + learn["machine"] + learn["egg"]:
            if name in seen:
                continue
            seen.add(name)
            info = idx.get(name)
            if not info:
                continue
            if (info.get("category") or "") == "变化":
                if name in self.STATUS_MOVES:
                    status.append(name)
            else:
                power = self.move_power(info)
                if power:
                    damage.append((name, power, info.get("type", "")))

        if not damage and not status:
            demo = [
                (n, self.move_power(i), i.get("type", ""))
                for n, i in idx.items()
                if (i.get("category") or "") != "变化"
            ]
            demo = sorted(
                (x for x in demo if x[1] and x[2] in p_types), key=lambda x: -x[1]
            )
            return [n for n, _p, _t in demo[:4]], False

        damage.sort(key=lambda x: -x[1])
        picks: list[str] = []
        used_types: set[str] = set()
        for name, _p, mtype in damage:  # STAB 且属性分散
            if len(picks) >= 3:
                break
            if mtype in p_types and mtype not in used_types:
                picks.append(name)
                used_types.add(mtype)
        for name, _p, _t in damage:  # 补最高威力
            if len(picks) >= 3:
                break
            if name not in picks:
                picks.append(name)
        if status and len(picks) < 4:
            picks.append(status[0])
        for name, _p, _t in damage:
            if len(picks) >= 4:
                break
            if name not in picks:
                picks.append(name)
        return picks[:4], True

    def validate_moveset(
        self, keyword: str, moves: list[str], level: int | None = None
    ) -> dict:
        """校验配招合法性：升级招按等级过滤、招式学习器合法、遗传招单独标注。"""
        row = self.pokemon(keyword)
        if not row:
            return {"error": f"没有查到「{keyword}」的相关信息"}
        learn = self.learnset(row)
        level_names = {
            n for n, lv in learn["level_up"] if lv is None or (level and lv <= level)
        }
        machine_names = {n for n, _ in learn["machine"]}
        egg_names = {n for n, _ in learn["egg"]}

        result = {
            "pokemon": row.get("name_zh", ""),
            "level": level,
            "valid": [],
            "invalid": [],
            "egg_only": [],
            "is_valid": True,
        }
        for mv in moves:
            clean = mv.strip()
            if clean in level_names or clean in machine_names:
                result["valid"].append(clean)
            elif clean in egg_names:
                result["egg_only"].append(clean)
            else:
                result["invalid"].append(clean)
                result["is_valid"] = False
        if result["invalid"]:
            pool = sorted(level_names | machine_names | egg_names)
            result["suggestions"] = []
            for mv in result["invalid"][:3]:
                result["suggestions"].extend(
                    difflib.get_close_matches(mv, pool, n=3, cutoff=0.4)
                )
        return result

    # ---------- 队伍分析 / 种族值筛选 ----------
    def analyze_team(self, names: list[str]) -> dict:
        """分析 1-6 只队伍：各成员弱点、共同弱点（≥2 只且过半）、全队抗性、属性覆盖。"""
        members = []
        for kw in names:
            row = self.pokemon(kw)
            if not row:
                members.append({"keyword": kw, "error": "未找到"})
                continue
            t_types = [t for t in (row.get("types") or "").split("/") if t]
            weak = []
            for atk in sorted({a for _, a in self.type_chart()}):
                m = self.matchup_multiplier([atk], t_types)
                if m > 1:
                    weak.append((atk, m))
            members.append(
                {
                    "keyword": kw,
                    "name": row.get("name_zh", ""),
                    "types": row.get("types", ""),
                    "weak": weak,
                }
            )
        ok = [m for m in members if "weak" in m]
        counter: dict[str, list[str]] = {}
        for m in ok:
            for atk, _mult in m["weak"]:
                counter.setdefault(atk, []).append(m["name"])
        threshold = max(2, (len(ok) + 1) // 2)
        common = sorted(
            ((a, ns) for a, ns in counter.items() if len(ns) >= threshold),
            key=lambda x: -len(x[1]),
        )
        resistant = [
            a
            for a in sorted({a for _, a in self.type_chart()})
            if a not in counter  # 无任何成员怕它 = 全队对它 ≤1×
        ]
        coverage = sorted({t for m in ok for t in (m["types"] or "").split("/") if t})
        return {
            "members": members,
            "common_weaknesses": common,
            "team_resists": resistant,
            "attack_coverage": coverage,
        }

    def by_stats(
        self, conditions: list[tuple[str, str, int]], limit: int = 10
    ) -> list[dict]:
        """按种族值条件筛选默认形态，按总和降序。

        conditions 为 [(能力项, 运算符, 数值)]，能力项支持
        HP/攻击/防御/特攻/特防/速度/总和，运算符支持 >= <= > < =。
        """
        ops = {
            ">=": lambda a, b: a >= b,
            "<=": lambda a, b: a <= b,
            ">": lambda a, b: a > b,
            "<": lambda a, b: a < b,
            "=": lambda a, b: a == b,
        }
        out = []
        for row in self.table("pokemon"):
            if row.get("form_name"):
                continue
            parsed = self.first_stats(row)
            if not parsed:
                continue
            vals = dict(parsed[0])
            vals["总和"] = parsed[1]
            if all(ops[op](vals.get(label, 0), val) for label, op, val in conditions):
                out.append(
                    {
                        "name": row.get("name_zh", ""),
                        "id": row.get("id_raw", ""),
                        "types": row.get("types", ""),
                        "total": parsed[1],
                    }
                )
        out.sort(key=lambda r: -r["total"])
        return out[:limit]

    # ---------- 许愿星基拉祈（WishMaker） ----------
    WISHMAKER_IV_KEYS = ("hp", "atk", "def", "spa", "spd", "spe")

    def wishmaker_query(
        self,
        nature: str | None = None,
        min_iv: int = 0,
        hp_type: str | None = None,
        tsv: int | None = None,
        shiny_only: bool = False,
        limit: int = 20,
    ) -> tuple[int, list[dict]]:
        """筛选许愿星基拉祈目标（数据覆盖全部 65536 个 WishMaker 种子）。

        nature/hp_type 传中文名精确匹配，min_iv 为六项个体下限；tsv=TID^SID 时
        附加 shiny 字段（WishMaker PID 判定式与第三代一致）。返回 (命中总数, 前
        limit 条)，按个体总和降序、seed 升序。
        """
        iv_keys = self.WISHMAKER_IV_KEYS
        hits = []
        for r in self.table("wishmaker"):
            if nature and r["nature"] != nature:
                continue
            if hp_type and r["hp_type"] != hp_type:
                continue
            ivs = [int(r[k]) for k in iv_keys]
            if min(ivs) < min_iv:
                continue
            row = dict(r, total=sum(ivs), shiny=None)
            if tsv is not None:
                pid = int(r["pid"], 16)
                row["shiny"] = ((pid >> 16) ^ (pid & 0xFFFF) ^ tsv) < 8
            if shiny_only and not row["shiny"]:
                continue
            hits.append(row)
        hits.sort(key=lambda r: (-r["total"], int(r["seed"], 16)))
        return len(hits), hits[:limit]

    def wishmaker_seed(self, seed_hex: str) -> dict | None:
        """按 16 进制种子查单条许愿星基拉祈数据（如 "353d"）。"""
        seed_hex = seed_hex.strip().lower().removeprefix("0x")
        return next((r for r in self.table("wishmaker") if r["seed"] == seed_hex), None)

    def format_row(self, row: dict, fields=None, max_cell=400) -> str:
        cols = fields or list(row.keys())
        lines = []
        for c in cols:
            v = row.get(c, "") or ""
            if len(v) > max_cell:
                v = v[:max_cell] + f"…(截断，原长{len(v)})"
            if v:
                lines.append(f"{c}: {v}")
        return "\n".join(lines)

    def first_stats(self, row: dict):
        """从 stats 字段解析默认形态种族值，返回 (list[(标签,数值)], 总和) 或 None。"""
        m = STAT_RE.search(row.get("stats", "") or "")
        if not m:
            return None
        labels = ["HP", "攻击", "防御", "特攻", "特防", "速度"]
        vals = [int(x) for x in m.groups()[:6]]
        return list(zip(labels, vals)), int(m.group(7))

    def find_image(self, row: dict, images_dir, shiny: bool = False) -> Path | None:
        """在数据集 images 目录中找立绘 {sub}/{id_raw}-{名称}[-形态][-shiny].png。

        闪光立绘 official 子目录无收录，回退 home。形态行优先匹配形态名
        （找不到形态图就返回 None，避免用默认形态图张冠李戴）；
        默认行优先精确同名，再排除 other_forms 中列出的非默认形态后取文件名最短的。
        """
        if not images_dir:
            return None
        suffix = "-shiny" if shiny else ""
        for sub in ("official", "home"):
            base = Path(images_dir) / sub
            if not base.is_dir():
                continue
            prefix = f"{row.get('id_raw', '')}-{row.get('name_zh', '')}"
            form = (row.get("form_name") or "").strip()
            if form:
                exact = base / f"{prefix}-{form}{suffix}.png"
                if exact.is_file():
                    return exact
                cands = [
                    p
                    for p in base.glob(f"{prefix}-{form}*{suffix}.png")
                    if shiny or "-shiny" not in p.name
                ]
                if cands:
                    return min(cands, key=lambda p: (len(p.name), p.name))
                continue
            exact = base / f"{prefix}{suffix}.png"
            if exact.is_file():
                return exact
            cands = [
                p
                for p in base.glob(f"{prefix}-*{suffix}.png")
                if shiny or "-shiny" not in p.name
            ]
            if not cands:
                continue
            non_default = {
                f.split("（")[0]
                for f in (row.get("other_forms") or "").split("、")
                if f
            }
            trim = len(suffix) + 4
            preferred = [
                p for p in cands if p.name[len(prefix) + 1 : -trim] not in non_default
            ] or cands
            return min(preferred, key=lambda p: (len(p.name), p.name))
        return None
