"""前端静态资产校验。

四层检查，在没有浏览器的前提下尽可能拦住前端回归：

1. **文件存在**：index.html 引用的样式/脚本，以及 10 个页面模块都在；
2. **JS 语法**：若本机有 ``node``，对每个 ES module 跑 ``node --check``；
3. **装配一致性**：页面注册表的 key / 分组 / 导出接口与页面模块实现一致；
4. **转义策略**：HTML 上下文里不得出现未转义的插值（带"已知漏洞必须被抓到"的自检）。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from research_agent.dashboard.app import STATIC_DIR  # noqa: E402
from tests._tmpdir import make_temp_dir  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

INFRA_MODULES = [
    "js/app.js",
    "js/pages/shell.js",
    "js/pages/registry.js",
    "js/ui/design.js",
    "js/common/dom.js",
    "js/common/api.js",
    "js/common/state.js",
    "js/common/ui.js",
]

#: PWA 形态的工作台页面（key -> (标题, 分组)）
#: 注：规划与检索合成一页（原 search/retrieval 两页割裂了同一条链）
PAGES = {
    "dashboard": ("总览", "总览"),
    "planretrieve": ("规划与检索", "研究入口"),
    "library": ("文献库", "资产"),
    "knowledge": ("知识抽取", "资产"),
    "ontology": ("动态本体", "资产"),
    "experiment": ("研究流程", "研究"),
    "writing": ("写作台", "研究"),
    "review": ("审核中心", "运行"),
    "system": ("系统状态", "运行"),
}

ALL_MODULES = INFRA_MODULES + [f"js/pages/{key}.js" for key in PAGES]


class StaticAssetsTest(unittest.TestCase):
    def setUp(self):
        self.index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    def test_index_references_styles_and_entry(self):
        for rel in ("css/theme.css", "css/shell.css", "vendor/vis-network.min.js"):
            self.assertIn(f"/static/{rel}", self.index, f"index.html 未引用 {rel}")
        self.assertIn('/static/js/app.js', self.index)
        # 旧的 6-tab 单文件脚本不应再被加载
        self.assertNotIn('src="/static/app.js"', self.index)

    def test_shell_containers_exist(self):
        for anchor in ('id="nav"', 'id="pageHost"', 'id="dbSelect"',
                       'id="pageTitle"', 'id="pageSub"', 'id="refreshBtn"',
                       'id="autoRefresh"', 'id="toast"'):
            self.assertIn(anchor, self.index, f"index.html 缺少 {anchor}")

    def test_all_modules_exist(self):
        for rel in ALL_MODULES:
            path = STATIC_DIR / rel
            self.assertTrue(path.is_file(), f"缺少 {rel}")
            self.assertGreater(path.stat().st_size, 150, f"{rel} 内容过短")

    def test_no_legacy_tab_markup(self):
        """旧的 view-*/tab* 结构已被页面路由取代。"""
        for legacy in ('data-tab=', 'view-library', 'tabLibrary'):
            self.assertNotIn(legacy, self.index, f"index.html 仍含旧结构 {legacy}")


class RegistryConsistencyTest(unittest.TestCase):
    """页面注册表与页面模块、index.html 三者必须一致。"""

    def setUp(self):
        self.registry = (STATIC_DIR / "js" / "pages" / "registry.js").read_text(encoding="utf-8")

    def test_registry_lists_all_pages(self):
        for key in PAGES:
            self.assertRegex(
                self.registry,
                rf'\{{ key: "{key}", group: "[^"]+", page: \w+Page \}}',
                f"registry.js 未注册页面 {key}",
            )

    def test_registry_imports_resolve(self):
        """registry.js 的导入必须指向同目录的真实文件。

        这条曾经抓到真实 bug：registry.js 位于 js/pages/ 下却写成
        ``./pages/dashboard.js``，``node --check`` 检查不出路径错误，
        只有真实加载（或本检查）才会暴露。
        """
        for key in PAGES:
            self.assertIn(f'./{key}.js', self.registry, f"registry.js 未导入 {key}")

    def test_all_relative_imports_resolve(self):
        """静态解析所有 ``from "./x.js"``，确认目标文件存在。"""
        import_re = re.compile(r'from\s+"(\.[^"]+)"')
        offenders = []
        for rel in ALL_MODULES:
            path = STATIC_DIR / rel
            base = path.parent
            for match in import_re.finditer(path.read_text(encoding="utf-8")):
                target = (base / match.group(1)).resolve()
                if not target.is_file():
                    offenders.append(f"{rel} -> {match.group(1)}")
        self.assertEqual(offenders, [], "存在无法解析的相对导入:\n" + "\n".join(offenders))

    def test_no_pages_prefix_in_pages_dir(self):
        """js/pages/ 下的模块不得再用 ./pages/ 前缀自引用。"""
        offenders = []
        for rel in ALL_MODULES:
            if not rel.startswith("js/pages/"):
                continue
            text = (STATIC_DIR / rel).read_text(encoding="utf-8")
            if '"./pages/' in text:
                offenders.append(rel)
        self.assertEqual(offenders, [], f"pages/ 下存在自引用前缀: {offenders}")

    def test_each_page_exports_contract(self):
        """每个页面模块必须导出 { title, group, mount, refresh }。"""
        for key, (title, group) in PAGES.items():
            source = (STATIC_DIR / "js" / "pages" / f"{key}.js").read_text(encoding="utf-8")
            for field in ("title:", "group:", "mount(", "refresh("):
                self.assertIn(field, source, f"{key}.js 缺少 {field}")
            self.assertIn(f'title: "{title}"', source, f"{key}.js 标题与注册表不一致")
            self.assertIn(f'group: "{group}"', source, f"{key}.js 分组与注册表不一致")

    def test_view_id_map_dropped_cleanly(self):
        """旧代码里依赖的 view id 不应再被任何模块引用。"""
        offenders = []
        for rel in ALL_MODULES:
            text = (STATIC_DIR / rel).read_text(encoding="utf-8")
            for legacy in ("view-library", "view-writing", "view-system",
                           "__refreshMergedTab"):
                if legacy in text:
                    offenders.append(f"{rel}: {legacy}")
        self.assertEqual(offenders, [], "仍引用旧结构:\n" + "\n".join(offenders))


class JavaScriptSyntaxTest(unittest.TestCase):
    @unittest.skipIf(shutil.which("node") is None, "本机没有 node，跳过 JS 语法校验")
    def test_all_modules_parse(self):
        tmp = make_temp_dir()
        try:
            failures = []
            for rel in ALL_MODULES:
                src = STATIC_DIR / rel
                dest = Path(tmp.name) / (rel.replace("/", "__").replace(".js", ".mjs"))
                dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
                proc = subprocess.run(["node", "--check", str(dest)],
                                      capture_output=True, text=True)
                if proc.returncode != 0:
                    failures.append(f"{rel}:\n{proc.stderr.strip()}")
            self.assertEqual(failures, [], "JS 语法错误:\n" + "\n\n".join(failures))
        finally:
            tmp.cleanup()


class EscapingPolicyTest(unittest.TestCase):
    """HTML 上下文里的插值必须经过 h()/已转义助手/安全常量。"""

    HTML_CONTEXT = re.compile(
        r'(?:data-[\w-]+|class|title|style|value|placeholder)\s*=\s*"\$\{([^}"]+)\}"'
        r"|>\$\{([^}<]+)\}<"
    )
    SAFE_CALL = re.compile(
        r"^(h|attr|trunc|empty|emptyState|decisionBadge|statusBadge|badge|chips|kv|"
        r"metrics|progress|progressRow|card|loading|errorBox|subtabs|fmtTs|fmtFull|"
        r"fmtBytes|fmtNumber)\s*\("
    )
    #: 命名约定：已转义的片段以这些后缀结尾（见 design.js 与各页面模块）
    SAFE_NAMED = re.compile(r"^[a-zA-Z_][\w]*(Html|Options|Chips|Rows|Cards)$")
    SAFE_NUMBERISH = re.compile(
        r"^[a-zA-Z_][\w\.\[\]]*\s*[+\-*/]|"
        r"^[a-zA-Z_][\w\.]*\.(length|count|paper_key|quality|pub_year|position)\b|"
        r"^\d"
    )
    SAFE_LITERAL = re.compile(r"^[\"']")

    def _is_safe(self, expr: str) -> bool:
        expr = expr.strip()
        if not expr:
            return True
        if (self.SAFE_CALL.match(expr) or self.SAFE_NAMED.match(expr)
                or self.SAFE_LITERAL.match(expr) or self.SAFE_NUMBERISH.match(expr)):
            return True
        if "?" in expr and ":" in expr:
            core = expr.split("?", 1)[1]
            parts = [p.strip() for p in core.rsplit(":", 1)]
            return all(self._is_safe(p) for p in parts)
        # 形如 `a || b` / `a ?? b`：任一侧安全即可（另一侧通常是空串/空态）
        for sep in ("||", "??"):
            if sep in expr:
                return any(self._is_safe(part) for part in expr.split(sep))
        return False

    def test_no_unwrapped_interpolation_in_html(self):
        offenders = []
        for rel in ALL_MODULES:
            if rel.endswith("common/dom.js") or rel.endswith("common/api.js"):
                continue
            text = (STATIC_DIR / rel).read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), 1):
                if "${" not in line:
                    continue
                for match in self.HTML_CONTEXT.finditer(line):
                    expr = (match.group(1) or match.group(2) or "").strip()
                    if self._is_safe(expr):
                        continue
                    offenders.append(f"{rel}:{lineno}  {match.group(0)}")
        self.assertEqual(offenders, [],
                         "发现 HTML 上下文中的未转义插值（请用 h() 包裹）:\n"
                         + "\n".join(offenders))

    def test_checker_catches_known_regression(self):
        """自检：历史上真实出现过的漏洞必须被判为不安全。"""
        line = '<button class="chip-x" data-tag="${tag}" title="移除">✕</button>'
        match = self.HTML_CONTEXT.search(line)
        self.assertIsNotNone(match, "规则失效：未匹配到 data-tag 属性插值")
        expr = (match.group(1) or match.group(2) or "").strip()
        self.assertFalse(self._is_safe(expr), "规则失效：未转义的 tag 被判成安全")
        safe = self.HTML_CONTEXT.search('<button data-tag="${h(tag)}">x</button>')
        self.assertTrue(self._is_safe(safe.group(1).strip()),
                        "误报：h(tag) 应被判为安全")

    def test_design_system_exports_helpers(self):
        design = (STATIC_DIR / "js" / "ui" / "design.js").read_text(encoding="utf-8")
        for name in ("card", "metrics", "badge", "progress", "empty",
                     "loading", "errorBox", "openDrawer", "kv", "chips"):
            self.assertIn(f"export function {name}(", design, f"design.js 缺少 {name}")

    def test_theme_tokens_match_pwa(self):
        """视觉以 PWA 为准：关键令牌必须存在且取值一致。"""
        theme = (STATIC_DIR / "css" / "theme.css").read_text(encoding="utf-8")
        for token, value in (
            ("--bg-deep", "#0A0E1A"),
            ("--bg-side", "#0F1629"),
            ("--bg-card", "#162040"),
            ("--accent", "#3B82F6"),
            ("--text-main", "#E8ECF4"),
        ):
            self.assertRegex(theme, rf"{re.escape(token)}:\s*{re.escape(value)}",
                             f"theme.css 的 {token} 与 PWA 不一致")


class FrontendRouteWiringTest(unittest.TestCase):
    """前端调用的接口必须在后端真实存在。

    这类错配 `node --check`、模块加载冒烟、静态检查**都发现不了**，只有用户点下去
    才会 404。这里把每个页面的 `api.get/post/...` 字面路径抽出来，与 FastAPI 路由表
    做**形状比较**（路径参数与模板插值都归一成 ``{x}``）。
    """

    @classmethod
    def setUpClass(cls):
        import importlib.util

        sys.path.insert(0, str(ROOT / "scripts"))
        spec = importlib.util.spec_from_file_location(
            "check_frontend_routes", ROOT / "scripts" / "check_frontend_routes.py")
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)
        # 只用到路由表，不读库；给临时目录避免污染 data/
        cls.tmp = make_temp_dir("routes-test-")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_every_frontend_call_matches_a_route(self):
        from research_agent.dashboard.app import create_app
        app = create_app(str(Path(str(self.tmp)) / "routes.db"))
        table = self.mod.routes(app)
        known = {self.mod.placeholder(path) for _, path in table}
        failures = []
        for page in sorted((STATIC_DIR / "js").rglob("*.js")):
            source = page.read_text(encoding="utf-8")
            for method, path in self.mod._calls_in(source):
                shape = self.mod.placeholder(path)
                if shape in known:
                    continue
                if any(method == m and self.mod.placeholder(rp) == shape
                       for m, rp in table):
                    continue
                failures.append(f"{page.name}: {method} {shape}")
        self.assertEqual(failures, [],
                         "以下前端调用在后端找不到路由（点击会 404）:\n"
                         + "\n".join(failures))

    def test_extractor_handles_nested_templates_and_query(self):
        """自检：抽取器必须能处理 ``${qs({...})}``、尾段插值与 query 插值。

        这些形态都是历史上真写过的；抽取器一旦退化，上面的用例会开始**漏报**
        （把不存在的路由判成存在），所以这里把形态钉死。
        """
        cases = [
            # 尾段插值是**路径段**：必须保留 {x}，否则 /paper/{key} 会被误判成 /paper
            ('api.get(`/api/library/paper/${encodeURIComponent(k)}`)',
             "/api/library/paper/{x}"),
            ('api.get(`/api/library/papers?q=${q}`)',
             "/api/library/papers"),
            ('api.get(`/api/library/papers${qs({ q, limit })}`)',
             "/api/library/papers"),
            ('api.get(`/api/library/citation/${encodeURIComponent(k)}?style=${s}`)',
             "/api/library/citation/{x}"),
            ('api.get(`/api/writing/section-jobs/${jobId}`)',
             "/api/writing/section-jobs/{x}"),
            ('api.post(`/api/library/folders/${folderId}/papers`, {})',
             "/api/library/folders/{x}/papers"),
        ]
        for source, expected in cases:
            calls = self.mod._calls_in(source)
            self.assertTrue(calls, f"抽取失败: {source}")
            shape = self.mod.placeholder(calls[0][1])
            self.assertEqual(shape, expected, f"抽取错误: {source} -> {shape}")

    def test_extractor_reports_method_for_request_helper(self):
        calls = self.mod._calls_in(
            'api.request(`/api/writing/projects/${id}/sections`, '
            '{ method: "PUT", body: {} })')
        self.assertEqual(calls[0][0], "PUT")


if __name__ == "__main__":
    unittest.main(verbosity=2)
