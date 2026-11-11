#!/usr/bin/env python3
"""One-shot: wrap the console plugin's Chinese literals so the daemon's dictionary can
translate them. Idempotent-ish (it refuses if it finds `t(` already applied to a key).

Not part of the test suite — it is the migration, run once, and kept only so the diff is
reproducible. Anything it cannot match is printed and must be fixed by hand.
"""

from __future__ import annotations

import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "core"))
from eventmem_core.ui_strings import EN  # noqa: E402

TARGET = pathlib.Path(__file__).resolve().parents[1] / (
    "adapters/hermes/desktop-plugins/eventmem-gui/plugin.js"
)

BLOCK = '''
/* ------------------------------------------------------------------ i18n
   One dictionary for both UIs, served by the daemon at /api/i18n and keyed by the Chinese
   source string — no build step means no key catalog to keep in sync. `t` degrades to the
   Chinese text, never to a bare key. `fmt` fills `{0}`/`{1}` placeholders.

   LANG/MAP are module-level because module-scope tables (TABS, FIELDS) are read at render
   time; the console assigns them once the fetch lands and re-renders.
*/
let LANG = 'zh'
let MAP = {}
const t = (text) => (LANG === 'en' && MAP[text]) || text
const fmt = (text, ...args) => t(text).replace(/\\{(\\d+)\\}/g, (whole, index) => (args[Number(index)] ?? whole))
'''

# Template literals with interpolation: `a ${x} b` cannot be looked up, so they become
# fmt('a {0} b', x). Order matters — do these before the plain-literal pass.
FMT = [
    (
        '`最新 ${when(stats.newest)}`',
        "fmt('最新 {0}', when(stats.newest))",
    ),
    (
        '`${NI(stats && stats.pairs)} 对已判`',
        "fmt('{0} 对已判', NI(stats && stats.pairs))",
    ),
    (
        '`落后 ${dur(cursor.behind)}`',
        "fmt('落后 {0}', dur(cursor.behind))",
    ),
    (
        '`${NI(r.pairs)} 对 / ${NI(r.edges)} 边`',
        "fmt('{0} 对 / {1} 边', NI(r.pairs), NI(r.edges))",
    ),
    (
        "`${okText || '失败'}：${result.reason || result.error || '不可用'}`",
        "fmt('{0}：{1}', okText || t('失败'), result.reason || result.error || t('不可用'))",
    ),
    (
        "`运行中 :${pack.port ?? '?'}`",
        "fmt('运行中 :{0}', pack.port ?? '?')",
    ),
    (
        '`已创建 ${name}`',
        "fmt('已创建 {0}', name)",
    ),
    (
        "`${(packs.data && packs.data.dir) || ''} 里还没有后端`",
        "fmt('{0} 里还没有后端', (packs.data && packs.data.dir) || '')",
    ),
    (
        "`安装失败：${current.error || ''}`",
        "fmt('安装失败：{0}', current.error || '')",
    ),
    (
        '`装完会自动把 ${target} 加进链路`',
        "fmt('装完会自动把 {0} 加进链路', target)",
    ),
    (
        '`每事件 ≈ ${pairs} 对 ≈ ${dur(pairs * 8)}（按 8 s/对估算）`',
        "fmt('每事件 ≈ {0} 对 ≈ {1}（按 8 s/对估算）', pairs, dur(pairs * 8))",
    ),
    (
        '`跳过：${result.skipped}`',
        "fmt('跳过：{0}', result.skipped)",
    ),
    (
        "`跑完：${NI(result.pairs)} 对，写 ${NI(result.edges)} 条边${result.backend ? `（${result.backend}）` : ''}`",
        "fmt('跑完：{0} 对，写 {1} 条边{2}', NI(result.pairs), NI(result.edges), "
        "result.backend ? fmt('（{0}）', result.backend) : '')",
    ),
]

# The one template literal that only had an interpolation in the middle of an escape.
FMT_REGEX = (
    re.compile(r"`有 pack 加载失败：(?:\\n)\$\{problems\.join\('(?:\\n)'\)\}`"),
    "fmt('有 pack 加载失败：\\n{0}', problems.join('\\n'))",
)


def main() -> int:
    src = TARGET.read_text(encoding="utf-8")
    if "const fmt = (text, ...args)" in src:
        print("already migrated")
        return 0

    anchor = "const PAGE = '/eventmem-gui'\n"
    if anchor not in src:
        print("anchor not found")
        return 1
    src = src.replace(anchor, anchor + BLOCK, 1)

    missing: list[str] = []
    for old, new in FMT:
        if old not in src:
            missing.append(old)
            continue
        src = src.replace(old, new, 1)

    pattern, replacement = FMT_REGEX
    if pattern.search(src):
        src = pattern.sub(lambda _: replacement, src, count=1)
    else:
        missing.append("有 pack 加载失败：…")

    wrapped = 0
    for key in EN:
        if "{" in key:  # fmt keys carry placeholders; fmt() already looks them up
            continue
        source_form = key.replace("\n", "\\n")
        needle = "'" + source_form + "'"
        if needle not in src:
            missing.append(key[:40])
            continue
        already = "t(" + needle + ")"
        if already in src:
            continue
        src = src.replace(needle, already)
        wrapped += 1

    # Module-scope tables are read at render time, so translate the label there.
    src = src.replace("                  children: label\n", "                  children: t(label)\n", 1)
    src = src.replace("jsx('span', { className: 'emg-label', children: label })",
                      "jsx('span', { className: 'emg-label', children: t(label) })", 1)

    TARGET.write_text(src, encoding="utf-8")
    print(f"wrapped {wrapped} literals, {len(FMT) + 1 - len(missing)} fmt conversions")
    if missing:
        print("NOT FOUND (fix by hand):")
        for item in missing:
            print("  -", item)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
