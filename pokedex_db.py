"""本地 TSV 知识库查询（数据由 pokemon-dataset-zh 项目 build_render.py 生成，
https://github.com/42arch/pokemon-dataset-zh）。

查询逻辑与技能版 scripts/query.py 同源：纯数字关键词按 id/id_raw/national_id
精确匹配，其他关键词按名称列（中/日/英）大小写不敏感子串匹配。
"""
import csv
import re
from pathlib import Path

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
SEARCH_TABLES = ["simple", "pokemon", "ability", "move", "item"]
ID_COLS = ("id", "id_raw", "national_id")
NAME_COLS = ("name_zh", "name_ja", "name_jp", "name_en", "name", "form_name")
SUMMARY_COLS = ("id", "id_raw", "national_id", "dex", "category_path",
                "name_zh", "form_name", "name_ja", "name_jp", "name_en")

STAT_RE = re.compile(r"HP(\d+)/攻击(\d+)/防御(\d+)/特攻(\d+)/特防(\d+)/速度(\d+)（总和(\d+)）")


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
            if (r.get("name_zh") or "").lower() == kw or (r.get("name_en") or "").lower() == kw:
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
        hits = [r for r in self.table("pokemon")
                if not r.get("form_name") and type_name in (r.get("types") or "")]
        hits.sort(key=lambda r: int(r["id"]) if r.get("id", "").isdigit() else 9999)
        return hits[:limit]

    def matchups(self, type_names: list[str]) -> dict | None:
        """防守属性组合 -> 弱点/抗性/免疫（基于本地 types.tsv 基准属性表）。"""
        chart = {(r["defender"], r["attacker"]): float(r["damage"]) for r in self.table("types")}
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
        return {"weak": out.get("weak", []), "resist": out.get("resist", []),
                "immune": out.get("immune", [])}

    # ---------- 许愿星基拉祈（WishMaker） ----------
    WISHMAKER_IV_KEYS = ("hp", "atk", "def", "spa", "spd", "spe")

    def wishmaker_query(self, nature: str | None = None, min_iv: int = 0,
                        hp_type: str | None = None, tsv: int | None = None,
                        shiny_only: bool = False, limit: int = 20) -> tuple[int, list[dict]]:
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
                cands = [p for p in base.glob(f"{prefix}-{form}*{suffix}.png")
                         if shiny or "-shiny" not in p.name]
                if cands:
                    return min(cands, key=lambda p: (len(p.name), p.name))
                continue
            exact = base / f"{prefix}{suffix}.png"
            if exact.is_file():
                return exact
            cands = [p for p in base.glob(f"{prefix}-*{suffix}.png")
                     if shiny or "-shiny" not in p.name]
            if not cands:
                continue
            non_default = {f.split("（")[0] for f in (row.get("other_forms") or "").split("、") if f}
            trim = len(suffix) + 4
            preferred = [p for p in cands
                         if p.name[len(prefix) + 1:-trim] not in non_default] or cands
            return min(preferred, key=lambda p: (len(p.name), p.name))
        return None
