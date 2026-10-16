"""许愿星基拉祈结果的 markdown 文案（可选输出格式，默认走图片卡片）。

适用于天然渲染 markdown 的平台：QQ 官方机器人（use_markdown 标记生效）、
微信个人号（weixin_oc，文本天然按 markdown 渲染）等。内容只使用 QQ 官方
markdown 文档列明的语法子集：标题、加粗、链接、有序列表、引用、分割线
（官方不支持表格与代码块，所以用"列表 + 全角分隔符"表意）。
"""
WISHMAKER_URL = "https://www.pokemonrng.com/zh-emulator-rs-wishmaker/"


def build_wishmaker_markdown(nature: str, iv_floor: int, count: int,
                             rows: list[dict], tsv: int | None = None) -> str:
    """markdown 版许愿星结果：标题 + 有序列表（种子/个体/总和/觉醒）+ 备注。"""
    lines = [f"# 许愿星基拉祈 · {nature or '任意'} · 个体≥{iv_floor}",
             f"共命中 **{count}** 个种子，按个体总和排序，显示前 {len(rows)}："]
    for i, r in enumerate(rows, 1):
        ivs = "/".join(f"**{int(r[k])}**" if int(r[k]) == 31 else r[k]
                       for k in ("hp", "atk", "def", "spa", "spd", "spe"))
        mark = " ✨异色" if r.get("shiny") else ""
        lines.append(f"{i}. 种子 **{r['seed']}**｜{ivs}｜总和 **{r['total']}**"
                     f"｜觉醒 {r['hp_type']}{r['hp_power']}{mark}")
    lines.append("***")
    note = "种子 = 红/蓝宝石接收许愿星基拉祈时的 WishMaker 种子（16位十六进制）"
    if tsv is not None:
        note += f"，异色按 TSV 0x{tsv:04X} 判定"
    lines.append(f"> {note}")
    lines.append(f"> 命中方法见：[🔗许愿星乱数教程]({WISHMAKER_URL})")
    return "\n".join(lines)