# 项目进度报告

> 对应计划：图表修复、功能完善、GitHub 开源与部署/视频方案
> 报告随各子任务完成逐节追加。

## 任务 1：图表标题完整显示 + 美化（已完成）

**完成内容**

- `bmon/charts.py` 重写标题折行逻辑：
  - 新增 `_wrap_lines` 基础折行函数；`_wrap_title` 改为"完整保留全部文字、绝不截断"，
    行宽按 30 → 36 → 42 → 50 → 60 自适应放宽，确保任意长度标题最多 3 行完整显示
  - 删除原有 "…" 截断逻辑
- Top 横向榜（`_top`）美化：
  - 标签字号随最大折行数自适应（9 / 8.4 / 7.8），避免多行标签互相挤压
  - 数值标签智能定位：长条放条内（白色加粗），短条放条外；前三名数值金色加粗强调
  - 条形左侧新增淡灰名次序号（01、02、03…）
  - 隐藏 x 轴刻度，仅保留淡虚线网格；去掉上/右/下边框，视觉更干净
  - 图例改为横排、无边框，位于右下角空白区
- 布局调整：四宫格仪表盘 figsize (17, 11) → (17.5, 13)，
  单张 top 图 (15, 9) → (15, 10)，给 Top 榜更多纵向空间

**改动文件**

- `bmon/charts.py`

**验证结果**

- `python main.py chart --period both --type dashboard` 成功生成
  `dashboard_weekly_2026-W34.png` / `dashboard_monthly_2026-08.png`
- `python main.py chart --period weekly --type top` 成功生成 `top_weekly_2026-W34.png`
- 人工检查 PNG：长标题（如「走近星穹——远坂凛：如何在宇宙时代学会智能机」）
  完整折两行显示、无截断；游戏仅以颜色区分、图例清晰；名次与数值标签正常

**产物路径**

- `output/charts/dashboard_weekly_2026-W34.png`
- `output/charts/dashboard_monthly_2026-08.png`
- `output/charts/top_weekly_2026-W34.png`

## 任务 2：快照功能验证与润色（已完成）

**现状说明**

快照功能在代码中已实现（CLI `snapshot` 子命令 + Web 页 `/snapshot`），
本轮完成验证与小润色，并补入 README（任务 4 一并处理）。

**验证结果（CLI）**

- 库内快照数据：440 条，时间范围 2026-08-16 04:41 ~ 2026-08-20 21:30
- 时刻查询：`python main.py snapshot --at "2026-08-19 21:00"` 正常返回各视频该时刻播放量
- 时段查询：`python main.py snapshot --from "2026-08-18 00:00" --to "2026-08-20 21:00"`
  正常返回期初/期末/增量三列，增量排序正确
- CSV 导出：`--csv` 导出 utf-8-sig 格式正常，标题完整无截断

**润色改动**

- `static/style.css`：`.t-title` 补充 `white-space: normal; line-height: 1.5; word-break: break-all;`，
  网页表格长标题自动折行完整显示，不产生截断观感

**Web 页验证**

`/snapshot` 页面（时刻/时段切换、筛选、CSV 导出）随任务 3 一并浏览器逐页点检，结果见任务 3 节。

## 任务 3：Web GUI 验证与润色（已完成）

**现状说明**

Web GUI 已实现（`python main.py gui`，Flask，仅监听 127.0.0.1），
含总览 / 视频数据 / 播放趋势 / 快照查询 / 运行控制五页，
采集操作通过子进程调用 main.py，与 CLI 行为一致。
本轮经浏览器逐页点检全部通过，无需代码修复；README 补充 GUI 章节见任务 4。

**浏览器逐页点检结果（全部 HTTP 200，无 JS 报错）**

- 总览页 `/`：统计卡片（183 视频 / 5.02 亿累计播放 / +197.3 万增量）、
  账号概况、 6 张图表 PNG 全部正常加载
- 视频数据 `/videos`：表格分页正常（183 条/7 页）；关键词 "PV" 筛选生效（183 → 66 条）
- 播放趋势 `/trend`：视频下拉 183 项，ECharts 折线图随选择正常更新，
  `/api/trend` 返回 200
- 快照查询 `/snapshot`：指定时刻查询返回 183 行数据；时段对比模式正确显示
  期初/期末/增量列，增量降序排列正确
- 运行控制 `/control`：任务状态、三个手动操作按钮（立即采集/重建图表/深度回填）、
  日志尾部 40 行均正常渲染（验证过程未点击任何执行按钮）

**功能覆盖确认**

GUI 已覆盖"脚本抓取 + 数据查看"全部诉求：
采集控制（fetch / fetch --full / chart 重建）+ 总览 + 筛选列表 + 趋势 + 快照 + CSV 导出。

**验证截图**

- `output/verify_1_overview.png`（总览页）
- `output/verify_2_videos_filtered_PV.png`（视频筛选页）

## 任务 4：GitHub 开源（已完成）

**完成内容**

- `git init`（main 分支）并完成首个提交（28 个文件，3702 行）
- 公开仓库已创建并推送：**https://github.com/kyo-zzz/bilibili-monitor**

**安全与文件处理**

- `.gitignore` 追加：`config.yaml`（含 cookie 等敏感字段）、`运行截图.png`（无关截图）
- 新增 `config.example.yaml` 配置模板（头部注明"复制为 config.yaml"），README 同步更新首次使用步骤
- 新增 MIT `LICENSE`（© kyo-zzz）
- 提交前经 `git status` 核验：`data/`、`.venv/`、`output/`、`config.yaml` 均未入库

**README 更新**

- 新增「本地 Web GUI」「快照查询」两个章节，命令一览表补 `snapshot` / `gui`
- 项目结构补 `webui.py`、`templates/`、`static/`、`docs/`
- 新增 Web GUI 截图两张（`docs/screenshots/webui_overview.png`、`webui_videos.png`）并在 README 嵌入
- 末尾新增许可证节

**验证结果**

- `gh api` 确认仓库 visibility=public、default_branch=main
- 远程文件清单确认无敏感文件：仅 bmon/templates/static/docs/config.example.yaml 等

## 任务 5：网页部署方案（方案稿已交付，待审阅）

**交付物**：`docs/deploy-plan.md`（仅方案，未实施）

- 核心约束分析：B站风控封杀数据中心 IP → 采集必须留在本地，云端只做展示
- 方案 A（推荐）：本地采集 + 静态报告发布到 GitHub Pages / Cloudflare Pages，零成本零风控风险
- 方案 B：内网穿透（Cloudflare Tunnel / Tailscale / frp）远程直连本机 Web GUI，数据不出本机、改动最小
- 方案 C：云服务器 Flask+gunicorn+nginx，附风控风险与成本分析，不推荐作主方案
- 含三方案对比表、推荐结论与待确认问题清单

## 任务 6：自动视频生成方案（方案稿已交付，待审阅）

**交付物**：`docs/video-gen-plan.md`（仅方案，未实施）

- 视频形态：30~60 秒 1080p 数据周报短片（片头→总览数字→图表入场→Top榜→片尾）
- 方案 A（推荐）：新增 `bmon/video.py` + `main.py video` 子命令，
  moviepy/Pillow 合成，数据与图表同源，纯 Python 无网络依赖
- 方案 B：Remotion（React 程序化视频），动效上限高，作为二期升级路线
- 方案 C：纯 FFmpeg 幻灯片，可作快速原型兜底
- 含自动投稿 B站的可行性说明与风控/登录态风险提示（建议人工投稿）

## 总结与后续建议

**本轮全部六项任务完成：**

| # | 任务 | 状态 |
|---|---|---|
| 1 | 图表标题完整显示 + 美化 | 已完成并验证 |
| 2 | 快照功能验证与润色 | 已完成（CLI + Web 双入口均验证通过） |
| 3 | Web GUI 验证与润色 | 已完成（五页浏览器点检全部通过） |
| 4 | GitHub 开源 | 已完成：https://github.com/kyo-zzz/bilibili-monitor |
| 5 | 网页部署方案 | 方案稿已交付（docs/deploy-plan.md），待审阅 |
| 6 | 自动视频生成方案 | 方案稿已交付（docs/video-gen-plan.md），待审阅 |

**后续建议（按优先级）**

1. 审阅 `docs/deploy-plan.md` 与 `docs/video-gen-plan.md`，确认选型后进入实施
2. 部署建议先启用方案 B（内网穿透，当天可用），二期再上静态公开页
3. 视频生成一期用 moviepy，数据积累越多（快照历史越长）周报内容越丰富
4. 快照数据从 2026-08-16 开始积累，建议保持每日 21:30 计划任务持续运行，
   两周后快照查询/增量对比的价值会显著提升
