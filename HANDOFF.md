# HANDOFF — eventmem（core / adapter 拆分）

给下一个接手的人（或下一个会话的我自己）。状态：**2026-11-09，拆分完成并已上线；0.2.1 把图谱并回 adapter、重写了双语 README。**

## 一句话现状

- 仓库：Mint `~/eventmem` ↔ Windows `D:\eventmem`（单向同步，见文末）。
- 运行时：`~/.hermes/plugins/eventmem` → 软链到本仓库 `adapters/hermes/eventmem`。
- 图谱：`~/.hermes/plugins/eventmem-graph` 与 `~/.hermes/desktop-plugins/eventmem-graph` 也指向本仓库
  `adapters/hermes/` 下的对应目录（`install.sh` 一次建三条软链）。
- 生产库：`~/.hermes/eventmem/events.db`（schema v7，171 条事件，未迁移、未改动）。
- 连边：`eventmem-worker.service` 已换到 `adapters/hermes/run.py worker`，运行中。
- 测试：8 个套件 **237 项**，installs venv 与裸 3.12 双解释器全绿。

## 这次改了什么

1. **core / adapter 拆开**：`core/eventmem_core` 不再 import 任何宿主；
   `adapters/hermes/eventmem` 只做接线。`__init__.py` 从 1721 行拆成
   `provider / tools / middleware / extract / host / settings / cli`。
2. **参数接口**：`CoreSettings`（frozen） + `apply()`；Hermes 侧读
   `plugins.entries.eventmem.settings.*`；worker/nightly 每进程读一次；
   `--print-settings` 可打印生效值。默认值 = 拆分前常量，有回归测试钉住。
3. **i18n 去宿主**：core 不再 `import agent.i18n`；adapter 在 initialize 时
   注册语言 provider。
4. **编码器目录**：`EVENTMEM_MODELS_ROOT` / `CoreSettings.models_root`，
   默认仍是 `<hermes_home>/localmem/models`。

## 没改的东西（故意的）

- schema、store 的 SQL、recall 的融合与渲染、夜间两问与 margin 语义。
- 召回块格式（用户定的契约）。
- 工具名与参数（模型已经学会的那一套）。

## 怎么验证

```bash
cd ~/eventmem
for t in store recall nightly worker i18n plugin settings hermes_settings; do
  python tests/test_$t.py | tail -1
done
# 宿主加载路径（真 Hermes）
~/.hermes/installs/*/environments/*/venv/bin/python - <<'PY'
import sys; sys.path.insert(0, "/home/wacky_ju2/.hermes/hermes-agent")
from plugins import memory
p = memory.load_memory_provider("eventmem", register_skills=False)
print(p.name, p.is_available(), len(p.get_tool_schemas()))
PY
```

## 已知 / 下一步

- **桌面会话仍在用旧模块**：本会话进程启动早于改动，`prefetch` 还是旧的。
  换新代码要重启 `hermes-dashboard` / `hermes-gateway`（或开新会话）。
- `eventmem-graph`（3D 图谱）已并入：`adapters/hermes/eventmem-graph`（dashboard 后端）+
  `adapters/hermes/desktop-plugins/eventmem-graph`（前端）。它直连 events.db、不走 `EventMem`，
  所以不算 provider，也不注册任何东西。
- DeepSeek Harness adapter：照 `docs/architecture.md` 的表格重写
  `provider/tools/middleware/settings/host` 五件套，core 一行不用动。
- 旧的 `hermes-eventmem` 仓库：内容已过时（仍是拆分前的插件），待决定归档还是改名。

## 同步到 Windows

```bash
cd ~/eventmem
tar czf - --exclude=.git --exclude=__pycache__ . | \
  ssh rubp@10.42.0.51 "tar xzf - -C D:\\eventmem"
```

推送仍然只能由 Windows 的 GitHub Desktop 做（两边 git CLI 都没有 GitHub 凭据）。
