"""PokéAPI 客户端：把 poke-mcp MCP 工具的能力（get_pokemon / get_type_matchups /
find_pokemon 等）整合为插件可直接调用的异步方法，作为本地图鉴的补充数据源。

带内存缓存；属性/世代名支持中文输入，输出尽量本地化（中文名、中文图鉴说明）。
"""
import time

import aiohttp

BASE = "https://pokeapi.co/api/v2"

TYPE_ZH = {
    "normal": "一般", "fire": "火", "water": "水", "grass": "草", "electric": "电",
    "ice": "冰", "fighting": "格斗", "poison": "毒", "ground": "地面", "flying": "飞行",
    "psychic": "超能力", "bug": "虫", "rock": "岩石", "ghost": "幽灵", "dragon": "龙",
    "dark": "恶", "steel": "钢", "fairy": "妖精",
}
TYPE_ZH_REV = {v: k for k, v in TYPE_ZH.items()}
GEN_ZH = {
    "generation-i": 1, "generation-ii": 2, "generation-iii": 3, "generation-iv": 4,
    "generation-v": 5, "generation-vi": 6, "generation-vii": 7, "generation-viii": 8,
    "generation-ix": 9,
}
STAT_ZH = {"hp": "HP", "attack": "攻击", "defense": "防御",
           "special-attack": "特攻", "special-defense": "特防", "speed": "速度"}
CACHE_TTL = 24 * 3600


def _zh_name(names: list) -> str:
    for n in names or []:
        if n.get("language", {}).get("name", "").lower() == "zh-hans":
            return n.get("name", "")
    return ""


def _zh_flavor(flavors: list) -> str:
    pick = None
    for f in flavors or []:
        if f.get("language", {}).get("name", "").lower() == "zh-hans":
            pick = f
            break
    if pick is None:  # 部分物种无简中文本，回退繁中/英文
        for f in flavors or []:
            if f.get("language", {}).get("name", "").lower() == "zh-hant":
                pick = f
                break
    if pick is None:
        for f in flavors or []:
            if f.get("language", {}).get("name") == "en":
                pick = f
                break
    if not pick:
        return ""
    return pick.get("flavor_text", "").replace("\n", " ").replace("\f", " ")


class PokeAPIClient:
    def __init__(self, timeout: int = 15):
        self.timeout = timeout
        self._session: aiohttp.ClientSession | None = None
        self._cache: dict[str, tuple[float, object]] = {}

    async def _get(self, path: str):
        key = path
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL:
            return hit[1]
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout))
        async with self._session.get(f"{BASE}/{path}") as resp:
            if resp.status == 404:
                self._cache[key] = (time.time(), None)
                return None
            resp.raise_for_status()
            data = await resp.json()
        self._cache[key] = (time.time(), data)
        return data

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # ---------- 对应 pokeapi_get_pokemon ----------
    async def get_pokemon(self, name_or_id: str) -> dict | None:
        pk = await self._get(f"pokemon/{name_or_id}")
        if not pk:
            return None
        sp = await self._get(f"pokemon-species/{pk['id']}")
        types = [TYPE_ZH.get(t["type"]["name"], t["type"]["name"])
                 for t in pk.get("types", [])]
        abilities = []
        for a in pk.get("abilities", []):
            ab = await self._get(f"ability/{a['ability']['name']}")
            abilities.append({
                "name_zh": _zh_name(ab.get("names")) if ab else "",
                "name_en": a["ability"]["name"],
                "hidden": a.get("is_hidden", False),
                "effect": (ab or {}).get("effect_entries") and
                          next((e["short_effect"] for e in ab["effect_entries"]
                                if e["language"]["name"] == "en"), ""),
            })
        stats = {STAT_ZH[s["stat"]["name"]]: s["base_stat"]
                 for s in pk.get("stats", [])}
        return {
            "id": pk["id"],
            "name_zh": _zh_name(sp.get("names")) if sp else "",
            "name_en": pk.get("name", ""),
            "genus": next((g.get("genus", "") for g in (sp or {}).get("genera", [])
                           if g.get("language", {}).get("name", "").lower() == "zh-hans"), ""),
            "generation": GEN_ZH.get((sp or {}).get("generation", {}).get("name", ""), ""),
            "types": "/".join(types),
            "stats": stats,
            "abilities": abilities,
            "height_m": pk.get("height", 0) / 10,
            "weight_kg": pk.get("weight", 0) / 10,
            "flavor": _zh_flavor(sp.get("flavor_text_entries")) if sp else "",
            "artwork": f"https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/"
                       f"pokemon/other/official-artwork/{pk['id']}.png",
        }

    # ---------- 对应 pokeapi_get_type_matchups ----------
    async def get_type_matchups(self, type_names: list[str]) -> dict | None:
        """防守方属性组合 -> 各进攻属性倍率。type_names 支持中文。"""
        en_types = []
        for t in type_names:
            t = t.strip()
            en = TYPE_ZH_REV.get(t, t.lower())
            if en not in TYPE_ZH:
                return None
            en_types.append(en)
        if not 1 <= len(en_types) <= 2:
            return None
        mult = {}
        for en in en_types:
            data = await self._get(f"type/{en}")
            if not data:
                return None
            dr = data["damage_relations"]
            for cat, factor in (("double_damage_from", 2), ("half_damage_from", 0.5),
                                ("no_damage_from", 0)):
                for t in dr.get(cat, []):
                    name = t["name"]
                    mult[name] = mult.get(name, 1) * factor
        out = {}
        for name, m in mult.items():
            if m == 1:
                continue  # 双属性组合后抵消为无相性（如 0.5×2）
            label = f"{TYPE_ZH.get(name, name)}×{m:g}"
            if m == 0:
                out.setdefault("immune", []).append(label)
            elif m > 1:
                out.setdefault("weak", []).append(label)
            else:
                out.setdefault("resist", []).append(label)
        return {"weak": out.get("weak", []), "resist": out.get("resist", []),
                "immune": out.get("immune", [])}

    # ---------- 对应 pokeapi_find_pokemon ----------
    async def find_by_type(self, type_zh: str, limit: int = 30) -> list[dict]:
        en = TYPE_ZH_REV.get(type_zh.strip(), type_zh.strip().lower())
        if en not in TYPE_ZH:
            return []
        data = await self._get(f"type/{en}")
        if not data:
            return []
        out = []
        for p in data["pokemon"]:
            url = p["pokemon"]["url"].rstrip("/").split("/")[-1]
            if not url.isdigit() or int(url) > 1025:
                continue
            out.append({"id": int(url), "name_en": p["pokemon"]["name"]})
        out.sort(key=lambda x: x["id"])
        return out[:limit]

    async def ability_effect(self, name_en: str) -> dict | None:
        ab = await self._get(f"ability/{name_en}")
        if not ab:
            return None
        eff = next((e["effect"] for e in ab.get("effect_entries", [])
                    if e["language"]["name"] == "en"), "")
        return {"name_zh": _zh_name(ab.get("names")), "name_en": name_en, "effect": eff}
