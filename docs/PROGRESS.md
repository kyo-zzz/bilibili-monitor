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
