"""把 JCR xlsx 导入为期刊分区表（零第三方依赖：zip + XML 直读）。

产出两份文件：

1. **内置子集**：`packs/skills/journal-quartiles/content/quartiles_<tag>.json`
   —— 默认进仓库，供本领域开箱使用（默认 tag=chemistry，按学科类别过滤）；
2. **完整表**：`data/jcr/jcr_quartiles_full.json`
   —— 全量 JCR 条目，数据目录已被 .gitignore 忽略（JCR 为授权数据，不进仓库）；
   用 `RA_JOURNAL_QUARTILES=<该文件>` 启用。

用法::

    python examples/import_jcr_xlsx.py \
        --xlsx "path/to/2026年度JCR期刊名单（完整版）.xlsx" \
        --tag chemistry \
        --categories "CHEMISTRY, MULTIDISCIPLINARY;CHEMISTRY, ORGANIC;CHEMISTRY, PHYSICAL;CHEMISTRY, APPLIED;CHEMISTRY, INORGANIC & NUCLEAR;CHEMISTRY, ANALYTICAL;CHEMISTRY, MEDICINAL;MATERIALS SCIENCE, MULTIDISCIPLINARY;POLYMER SCIENCE;CATALYSIS"
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
PROJECT_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_CATEGORIES = (
    "CHEMISTRY, MULTIDISCIPLINARY", "CHEMISTRY, ORGANIC", "CHEMISTRY, PHYSICAL",
    "CHEMISTRY, APPLIED", "CHEMISTRY, INORGANIC & NUCLEAR",
    "CHEMISTRY, ANALYTICAL", "CHEMISTRY, MEDICINAL", "CATALYSIS",
    "MATERIALS SCIENCE, MULTIDISCIPLINARY", "POLYMER SCIENCE",
    "ELECTROCHEMISTRY", "CHEMISTRY, COMBINATORIAL",
)
VALID_QUARTILES = {"Q1", "Q2", "Q3", "Q4"}


def _shared_strings(z: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in z.namelist():
        return []
    root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    return ["".join(t.text or "" for t in si.iter(f"{NS}t"))
            for si in root.findall(f"{NS}si")]


def _cell_text(cell, shared: list[str]) -> str:
    cell_type = cell.get("t")
    value = cell.find(f"{NS}v")
    if cell_type == "s" and value is not None:
        idx = int(value.text or 0)
        return shared[idx] if idx < len(shared) else ""
    if cell_type == "inlineStr":
        return "".join(t.text or "" for t in cell.iter(f"{NS}t"))
    return value.text if value is not None else ""


def read_rows(xlsx: Path) -> list[dict[str, str]]:
    """读取首个工作表为 dict 列表（键为表头文本）。"""
    with zipfile.ZipFile(xlsx) as z:
        sheets = sorted(n for n in z.namelist()
                        if re.match(r"xl/worksheets/sheet\d+\.xml", n))
        if not sheets:
            raise SystemExit(f"未找到工作表: {xlsx}")
        shared = _shared_strings(z)
        root = ET.fromstring(z.read(sheets[0]))
    rows = root.findall(f".//{NS}row")
    if not rows:
        return []
    header: dict[str, str] = {}
    for cell in rows[0].findall(f"{NS}c"):
        col = re.sub(r"\d+", "", cell.get("r") or "")
        header[col] = _cell_text(cell, shared).strip()
    out: list[dict[str, str]] = []
    for row in rows[1:]:
        record: dict[str, str] = {}
        for cell in row.findall(f"{NS}c"):
            col = re.sub(r"\d+", "", cell.get("r") or "")
            name = header.get(col)
            if name:
                record[name] = _cell_text(cell, shared).strip()
        if record:
            out.append(record)
    return out


def pick(record: dict[str, str], *candidates: str) -> str:
    for name in candidates:
        if record.get(name):
            return record[name]
    for key, value in record.items():
        for candidate in candidates:
            if candidate in key and value:
                return value
    return ""


def normalize(name: str) -> str:
    return " ".join(str(name or "").lower().split())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="导入 JCR xlsx 为分区表")
    ap.add_argument("--xlsx", required=True)
    ap.add_argument("--tag", default="chemistry",
                    help="内置子集文件名后缀：quartiles_<tag>.json")
    ap.add_argument("--categories", default=";".join(DEFAULT_CATEGORIES),
                    help="按学科类别过滤内置子集（; 分隔；留空表示不过滤）")
    ap.add_argument("--full-out", default="data/jcr/jcr_quartiles_full.json")
    ap.add_argument("--subset-out", default="")
    args = ap.parse_args(argv)

    xlsx = Path(args.xlsx)
    if not xlsx.is_file():
        raise SystemExit(f"文件不存在: {xlsx}")
    rows = read_rows(xlsx)
    print(f"读取 {len(rows)} 行（{xlsx.name}）")

    wanted = {c.strip().upper() for c in args.categories.split(";") if c.strip()}
    full: dict[str, str] = {}
    subset: dict[str, str] = {}
    skipped = 0
    for record in rows:
        name = pick(record, "期刊名称", "Journal name", "Title")
        quartile = pick(record, "JIF分区", "JIF Quartile").upper()
        category = pick(record, "学科类别", "Category").upper()
        if not name or quartile not in VALID_QUARTILES:
            skipped += 1
            continue
        key = normalize(name)
        full[key] = quartile
        if not wanted or any(w in category for w in wanted):
            subset[key] = quartile
    print(f"有效条目 {len(full)}（跳过 {skipped}）；内置子集 {len(subset)}")

    full_out = PROJECT_ROOT / args.full_out
    full_out.parent.mkdir(parents=True, exist_ok=True)
    full_out.write_text(json.dumps(full, ensure_ascii=False, indent=0),
                        encoding="utf-8")
    print(f"写入完整表: {full_out}")

    subset_out = (PROJECT_ROOT / args.subset_out if args.subset_out else
                  PROJECT_ROOT / "packs/skills/journal-quartiles/content"
                  / f"quartiles_{args.tag}.json")
    subset_out.parent.mkdir(parents=True, exist_ok=True)
    subset_out.write_text(json.dumps(subset, ensure_ascii=False, indent=0),
                          encoding="utf-8")
    print(f"写入内置子集: {subset_out}")

    for probe in ("journal of the american chemical society",
                  "angewandte chemie international edition", "organic letters",
                  "chemical science", "acs catalysis"):
        print(f"  抽查 {probe}: full={full.get(probe)} subset={subset.get(probe)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
