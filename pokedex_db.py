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
            key = "weak" if m > 1 else ("immune" if m == 0 else "resist")
            out.setdefault(key, []).append(f"{atk}×{m:g}")
        return {"weak": out.get("weak", []), "resist": out.get("resist", []),
                "immune": out.get("immune", [])}

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

    def find_image(self, row: dict, images_dir) -> Path | None:
        """在数据集 images 目录中找官方立绘 official/{id_raw}-{名称}[-形态].png（排除闪光）。

        形态行优先匹配形态名（找不到形态图就返回 None，避免用默认形态图张冠李戴）；
        默认行优先精确同名，再排除 other_forms 中列出的非默认形态后取文件名最短的。
        """
        if not images_dir:
            return None
        base = Path(images_dir) / "official"
        if not base.is_dir():
            return None
        prefix = f"{row.get('id_raw', '')}-{row.get('name_zh', '')}"
        form = (row.get("form_name") or "").strip()
        if form:
            exact = base / f"{prefix}-{form}.png"
            if exact.is_file():
                return exact
            cands = [p for p in base.glob(f"{prefix}-{form}*.png") if "-shiny" not in p.name]
            return min(cands, key=lambda p: (len(p.name), p.name)) if cands else None
        exact = base / f"{prefix}.png"
        if exact.is_file():
            return exact
        cands = [p for p in base.glob(f"{prefix}-*.png") if "-shiny" not in p.name]
        if not cands:
            return None
        non_default = {f.split("（")[0] for f in (row.get("other_forms") or "").split("、") if f}
        preferred = [p for p in cands
                     if p.name[len(prefix) + 1:-4] not in non_default] or cands
        return min(preferred, key=lambda p: (len(p.name), p.name))
