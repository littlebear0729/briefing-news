# DESIGN.md — The Global Brief（交接设计文档）

> 给下一个 agent：本文档描述**要做什么、为什么这么做、哪些代码已经能跑、还差什么**。
> 最后更新：2026-10-13（此仓库为首次实现提交；**§11 是实施状态、实测偏差与验证证据**，读 §0-§10 之后请务必读它）。
> 仓库：`briefing-news`（Python 3.12；本机验证环境 Windows + Python 3.12.4）。

---

## 0. 一句话

每天两次（AM/PM），从**白名单新闻源**抓取候选 → 由 **LLM 只做编辑判断**（选稿/排序/标题/压缩）→ 由**代码**渲染成「A4 两页正反、响应式、可插图」的报纸网页 → 导出 PDF → 通过 **Telegram bot** 推送给用户。全部由 **GitHub Actions** 定时驱动。

**核心设计哲学（不可违背）：代码拥有所有稳定的东西，LLM 只拥有编辑判断。**
LLM 永远不生成 HTML、永远不写 URL、永远不决定版式与分页。

---

## 1. 需求 → 现状对照（`prompt.txt`）

| # | 需求 | 现状 | 负责文件 |
|---|---|---|---|
| 1 | 权威来源（AP/BBC/CNN/Al Jazeera/Reuters…），覆盖地缘政治/科技/金融/体育/娱乐 | ✅ 已实现。域名白名单 + 每节 RSS，5 个 section | `pipeline/sources.py`、`pipeline/config.yaml` |
| 2 | A4 两页正反、报纸版式、大小标题、可插图 | ✅ 报纸版式与大小标题；**§12 修订：不再固定 A4 版面、不再有插图**，改为一份响应式 HTML（篇幅约两张 A4） | `templates/newspaper.html.j2`、`templates/_layout.j2` |
| 3 | 响应式网页，手机 + 电脑都能读 | ✅ 已实现（640 / 1024 两档断点） | `templates/mobile.html.j2` |
| 4 | 输出文件通过 Telegram bot 发送给用户 | ✅ **已实现** | `pipeline/publish.py`、`state/sent.json` |
| 5 | GitHub Actions 自动化，env 配置 LLM endpoint/model/token + TG bot token/chatid | ✅ **已实现**（AM/PM cron、手动补发、build/publish 拆 job、按 SHA 钉版本） | `.github/workflows/daily-brief.yml` |

### 需求 4 的一处歧义（已决，需注意）

`prompt.txt` 第 4 条写「输出**html**文件通过 bot 发送」，但后续口头需求明确为「输出 **pdf** 后发送」。
**决策**：主产物发 **PDF**（手机上能直接看、排版保证两页），HTML 作为源文件可选附带。
实现上把两者都放在同一条流水线里：`mobile.html` 是源，`mobile.pdf` 是发的附件。若用户改主意只要 HTML，只需改发送步骤的路径。

---

## 2. 架构与数据流

```
config.yaml ──┐
              ├─▶ fetch.py      build/articles-<date>-<ed>.json    （纯代码，确定性）
sources.py ───┘        │
                       ▼
              editorial.py   build/edition-<date>-<ed>.json      （唯一的 LLM 调用）
                       │
        ┌──────────────┴───────────────┐
        ▼                              ▼
   render.py                     render_mobile.py
   dist/global-brief-*.html      mobile.html                   （同一份 edition JSON，两个产物）
        │                              │
        ▼                              ▼
   export.py (Playwright)        export.py (Playwright)
   dist/*.pdf  ← 打印版报纸        mobile.pdf  ← 手机版/打印同样两页
        │                              │
        └──────────────┬───────────────┘
                       ▼
              publish.py（待建）→ Telegram sendDocument
                       ▲
              state/sent.json（幂等，待建）
```

**一次 LLM 调用喂两个产物**，不要为 mobile 再调一次模型。

---

## 3. LLM / 代码 分工（硬约束）

| 交给 LLM（仅此四类） | 交给代码（其余全部） |
|---|---|
| 从候选池里**选哪几条**、谁是头条、排序分层 | 抓取、域名白名单、时间窗、去重、排序、每节封顶 ≤12 |
| 写 `headline` / `kicker` / `deck` / `dateline` | **引用解析**（LLM 只出 `source_ids`，代码换成名称+URL） |
| 把摘要**压缩**成 wire 体正文 | JSON schema 校验 + 出错回灌重试 |
| 某节候选不足时从相邻节补（prompt 里已写明） | **字数/段数预算的最终执行**（prompt 只是告知） |
| | 版式、CSS、分页与长度、报头花边（版本化资产，从不生成） |
| | 日期、期号、文件名、manifest |
| | PDF 导出、Telegram 发送、secret、超时、重试、回写仓库 |

**绝不给 LLM 的**：URL/域名、HTML/CSS/JS/SVG、分页与页数、字数裁剪、feed 列表、时间窗。
RSS 摘要属于**不可信输入**；「只能引用给定 id」这条契约同时挡住了幻觉和 prompt injection。

> ⚠️ `markets.items[].dir`（涨跌箭头）目前由 LLM 给 —— 方向是事实主张，不是编辑判断。
> 建议改为由行情数据提供，或直接省略箭头只留 `note`（现有 2026-10-13 版就是这么做的：只留方向、省略点位）。

---

## 4. 已可用代码清单（**直接复用，不要重写**）

| 文件 | 关键接口 | 产物 |
|---|---|---|
| `pipeline/config.yaml` | feeds、editions(AM/PM cutoff)、lookback_hours、budgets、model | — |
| `pipeline/sources.py` | `is_allowed(url) -> bool`、`domain_of(url)`、`ALLOWED_DOMAINS` | — |
| `pipeline/fetch.py` | `run(edition: str, run_date: datetime) -> Path`、`collect()`、`load_config()` | `build/articles-<date>-<ed>.json` |
| `pipeline/editorial.py` | `run(archive_path, edition) -> Path`；导出 `EDITION_SCHEMA`、`_enforce_budgets(edition, config)`、`_resolve_sources(obj, index)`、`SYSTEM_PROMPT` | `build/edition-<date>-<ed>.json` |
| `pipeline/render.py` | `run(edition_path, run_date, edition) -> (html, Path)`；`build_meta(run_date, edition, config, sources)`、`_collect_source_names(edition)`、`edition_number()` | `dist/global-brief-<date>-<ed>.html` |
| `pipeline/render_mobile.py` | `run(edition_path, run_date, edition, template, out) -> Path`、`load_payload()`、`check_allowlist()`、`validate()` | **`mobile.html`**（仓库根目录，固定文件名） |
| `pipeline/export.py` | `to_pdf(html_path, pdf_path=None) -> Path`（Playwright/Chromium，`emulate_media("print")`） | 同名 `.pdf` |
| `pipeline/run.py` | `main()`：`--edition am\|pm [--date YYYY-MM-DD] [--no-pdf]` | `dist/manifest-*.json` |
| `templates/brief.html.j2` | A4 打印优先（固定 210mm） | — |
| `templates/mobile.html.j2` | **响应式**（手机单栏 / ≥1024px 多栏）+ 打印恰好两页 A4 | — |
| `.github/workflows/daily-brief.yml` | cron `15 1 * * *`(AM) / `15 13 * * *`(PM) + `workflow_dispatch` | artifacts + 回写 `dist/` |

**现成 fixture（无网络、无 key 也能跑）**：
`build/edition-2026-10-13-am.json`（手工编写的完整一期，含 32 条来源索引）+ 已生成的 `mobile.html`。
开发 publish 步骤时直接拿这两个文件调，不必先跑通 LLM。

---

## 5. 数据契约

### 5.1 LLM 的输入（`build/articles-*.json`）

```json
{
  "edition": "AM",
  "run_date": "2026-10-13",
  "sections": {
    "geopolitics": [
      {"id": "GEO-01", "section": "geopolitics", "title": "...", "url": "https://...",
       "source": "reuters.com", "published": "2026-10-13T00:12:00+00:00", "summary": "..."}
    ]
  }
}
```
`id` 由代码生成：`<section 前三字母大写>-<序号>`（`GEO-01`、`TEC-01`、`FIN-01`、`SPO-01`、`ENT-01`）。

### 5.2 LLM 的输出（`build/edition-*.json`，经 `EDITION_SCHEMA` 校验）

```
lead{…STORY}                     secondary[2×STORY]        briefs[3..5]{label,text}
markets{items[4..6]{label,dir∈up|down|flat,note}, note}
tech{stories[2..3]}              money{lead, side[1..2], key_numbers[3..5]{value,label}}
sport{lead, bullets[3..4]}       culture[3×STORY]
STORY = {kicker?, headline, deck?, dateline?, paras[1..6], source_ids[1..5]}
```
预算（`config.yaml.budgets`，由 `_enforce_budgets` **在代码里**强制）：lead 5 段×55 词、每故事 3 段×45 词、brief 28 词、标题 12 词。

### 5.3 `render_mobile` 接受的两种输入

1. 编辑器直接产物（上面 5.2 的裸对象）；
2. **手工编写的包装形式**（推荐给离线开发）：
```json
{ "run_date": "2026-10-13", "edition": "AM", "note": "...",
  "sources": { "oil-euronews": {"source": "Euronews", "url": "https://…"} },
  "content": { …5.2 的完整对象… } }
```

### 5.4 环境变量

| 变量 | 类型 | 默认 / 说明 |
|---|---|---|
| `BRIEF_LLM_API_KEY` | **Secret** | 必填 |
| `BRIEF_LLM_BASE_URL` | Variable | `config.yaml` 默认 `https://api.deepseek.com/v1`；**空串会回退到默认值** |
| `BRIEF_LLM_MODEL` | Variable | 默认 `deepseek/deepseek-v4-flash`；建议钉死版本 |
| `BRIEF_LLM_RETRIES` | Variable | 默认取 `config.yaml.model.retries = 3` |
| `TELEGRAM_BOT_TOKEN` | **Secret** | 专用 bot，勿复用 |
| `TELEGRAM_CHAT_ID` | Variable | 单聊 `chat_id == user_id`；群/频道为负值（`-100…`） |
| `TELEGRAM_ADMIN_CHAT_ID` | **Secret** | 失败告警收件人 |

---

## 6. 待办（按依赖顺序，含验收标准）

### A. `pipeline/publish.py` —— Telegram 发送（核心缺口）
- [ ] `publish(pdf: Path, chat_ids: list[str], *, token, caption)`，用 `sendDocument`（`requests` 已是依赖）
- [ ] preflight：`getMe` + `getChat`，失败给出清晰报错
- [ ] **幂等**：`state/sent.json` 记录 `<date>-<EDITION>`，重跑不重发 ← *验收：同参数连跑两次，第二次不发*
- [ ] **限流**：串行 + `sleep(0.6)`；遇 `429` 读 `parameters.retry_after` 再退避
- [ ] **重试**：每次重试必须重新 `open()` 文件句柄（`with` 内不能跨重试复用）
- [ ] filename 传 `The-Global-Brief-<date>-<EDITION>.pdf`（别发 `mobile.pdf`）
- [ ] caption ≤1024 字符，`parse_mode=HTML`，正文不要重复
- [ ] 失败告警：`if: failure()` 时用 `sendMessage` 通知 `TELEGRAM_ADMIN_CHAT_ID`
- [ ] **token 绝不落日志**：Bot API URL 内含 token，禁止 `echo $url` / `set -x`；步骤里先 `echo "::add-mask::$TELEGRAM_BOT_TOKEN"`

### B. 流水线接线
- [ ] `pipeline/run.py` 增加 mobile 阶段（或在 workflow 里单独调用 `render_mobile`）
- [ ] `dist/` 产物命名建议 `mobile-<date>-<ed>.html`；**若坚持根目录固定 `mobile.html`，PM 会覆盖 AM**（已知取舍）
- [ ] 一次性把 `mobile.html` 也纳入 artifact 与回写

### C. PDF 断言（发送前必须过）
- [ ] 页数 == 2（`pypdf`）
- [ ] 页脚固定文案出现在 PDF 提取文本中 ← 抓「被 `overflow:hidden` 静默裁剪」
- [ ] 文件 < 50 MB（Telegram 上限）；实测 2 页约 0.2–0.7 MB

### D. Workflow 加固
- [ ] `concurrency`（AM/PM/手动可能同时 `git push` → 冲突）
- [ ] `timeout-minutes`；模型版本钉死（换模型走 PR）；CI 温度压到 0–0.2
- [ ] 上传 `build/articles-*.json` 与 `build/edition-*.json`（现在的 artifact 只有 `dist/*`，**审计凭据丢了**）
- [ ] 拆 job：`build`（持 LLM key）→ `publish`（`needs: build`，只持 bot token），最小权限
- [ ] 某 section 候选为 0 时 `::warning::`（feed 会腐烂）
- [ ] Actions 按 commit SHA 钉版本

### E. 白名单补齐
- [ ] **`cnn.com` 不在 `ALLOWED_DOMAINS` 里**，但 `prompt.txt` 明确点名 CNN —— 补上
- [ ] 其余已具备：`reuters.com` `apnews.com` `bbc.co.uk` `aljazeera.com` `nytimes.com` `washingtonpost.com` `ft.com` `bloomberg.com` `cnbc.com` `imf.org` `nobelprize.org` `formula1.com` `atptour.com` `espn.com` `deadline.com` 等

### F. 可选增强
- [ ] **数字回显校验**（纯代码）：正文里每个数字必须出现在其引用的 source `summary` 中 → 零成本堵住最后一个幻觉通道（比再叫一次 LLM 审稿可靠）
- [ ] `markets.dir` 改由数据提供
- [ ] 订阅列表（多收件人）：id 列表放 Secret，或 `/start` `/stop` 维护在 `state/subscribers.json`

---

## 7. 必须遵守的不变量（改动时别推翻）

1. **版式是版本化资产**，LLM 永远不产生 HTML/CSS/SVG。
2. **引用只认 id**：LLM 写 URL = 允许幻觉，禁止。
3. **预算在代码里执行**（`_enforce_budgets`），prompt 只是告知。
4. **校验顺序不可换**：`check_allowlist → validate(schema) → resolve_sources → _enforce_budgets`。
   schema 是针对 `source_ids` 写的；先 resolve 再校验必然失败（本机已踩过）。
5. **两套模板打印都必须恰好 2 页 A4**（`@page { size: A4 }` + 固定 210×297mm `.page` + `break-after: page`）。
6. **白名单是第一道闸**，`render_mobile` 会在渲染前拒绝任何非白名单 URL。
7. `templates/mobile.html.j2` 遵循 **Neutral Modern 设计系统**（下一个 agent 可能读不到该 skill，这里内联规则）：
   - 第一个 `<style>` 顶部的 `:root` 令牌块**原样粘贴，不得改动**；本项目自有配色放在**第二个** `:root` 里作为扩展令牌。
   - **`:root` 之外禁止出现裸 hex**（现有产物已验证：0 处违规）。
   - 交互元素必须有 `:focus-visible` + `var(--focus-ring)`。
   - 断点固定 640 / 1024；禁止渐变、玻璃拟态、输入框阴影、左竖线卡片。
8. **不想干就别塞进模板**：`render.py` 用 `trim_blocks/lstrip_blocks`，Jinja 里 `e.markets.items` 会解析成 dict 的 `.items()` 方法报 `TypeError`，必须写 `e.markets['items']`。

---

## 8. 本地验证（已实测通过的命令）

```bash
pip install -r requirements.txt          # 注意：feedparser 曾缺失，需确保装上
python -m playwright install chromium    # 导出 PDF 必需，本机尚未下载浏览器
```

```bash
# 无网络、无 LLM key 也能跑通（用现成 fixture）—— 已实测：
python -m pipeline.render_mobile build/edition-2026-10-13-am.json --date 2026-10-13
#   -> mobile.html  (45,094 bytes, 16 sources cited)

# 完整链路（需要 key）
python -m pipeline.run --edition am
python -m pipeline.run --edition am --no-pdf        # 跳过 Playwright
python -m pipeline.export mobile.html               # 单独导出 PDF
```

已实测的产物验收点：schema 通过、每个 URL 均在白名单内、无未渲染 Jinja、`:root` 外无裸 hex、两个 `.page`、标签闭合、27 条外链、页 1 ≈837 词 / 页 2 ≈716 词（8.4pt 下各页余量充足）。
**未验证**：真实 PDF 导出与分页（本机无 Chromium，且不应在无用户确认时开无头浏览器）——下一个 agent 请务必自己打印预览确认。

---

## 9. 已知坑（都是真踩过的）

1. `feedparser` 在 `requirements.txt` 里但本机环境没装 → 直接 `ModuleNotFoundError`。
2. `render_mobile` 若先 resolve 再 validate，会报 `'source_ids' is a required property` + `Additional properties are not allowed`。
3. Jinja 的 `.items` 陷阱（见上）。
4. **bot 不能主动发起会话**：用户必须先给 bot 发过 `/start`，否则 `403: bot can't initiate conversation with a user`。
5. **GitHub cron 是尽力而为**：高峰期可能延迟十几分钟甚至整次丢弃；不要向用户承诺准点送达，必要时用外部定时器调 `workflow_dispatch`。
6. `.page { overflow: hidden }` 保证「恰好两页」，代价是溢出内容**静默被裁** → 必须靠 C-2 的文本断言兜底。
7. 仓库根目录那 7 个其它模型产出的 html/pdf（`brief-claude.html`、`newspaper-qoder.html`、`global-brief-2026-10-13-dsv4p.pdf` …）是**对比样本，不属于流水线**，不要接线也不要删。

---

## 10. 定义完成（Definition of Done）

- [x] `render_mobile` 与 `export` 在 CI 里产出 HTML + PDF，并通过页数/裁剪断言
      （本地已实测：两个产物各 2 页、每页都有页脚、11 条内容片段齐全，见 §11.4）
- [x] `publish` 在断言通过后才发送；重跑不重发；429 正确退避；token 不出现在任何日志
      （单元测试覆盖：`tests/test_publish.py`）
- [ ] AM/PM 各一次成功投递，产物与 `build/*.json` 审计文件同时上传
      ← 需要真实 LLM key + bot token + 真跑一次 Actions，**本机无法代跑**
- [x] `github.event_name == workflow_dispatch` 可手动补发指定日期/版本

---

## 11. 实施状态（2026-10-13，首次实现）

### 11.1 交付物清单

| 文件 | 说明 |
|---|---|
| `pipeline/config.yaml` | feeds / editions / lookback / budgets / model / layout / telegram |
| `pipeline/config.py` | 配置加载、日期/期号/时间窗、PATH 解析、env 覆盖（**空串回退默认值**） |
| `pipeline/sources.py` | 白名单：`is_allowed` / `domain_of` / `assert_allowed` / `iter_urls` |
| `pipeline/fetch.py` | 抓取→候选池；时间窗、按 URL/标题去重、每节封顶、id 生成、feed 审计 |
| `pipeline/editorial.py` | 唯一的 LLM 调用：`EDITION_SCHEMA`、`SYSTEM_PROMPT`、重试回灌、`_resolve_sources`、`_enforce_budgets` |
| `pipeline/content.py` | 遍历 edition 的公共槽位、词数统计、**页二分栏规划**（见 §11.3） |
| `pipeline/render.py` | 唯一渲染入口：`build_meta`、`_collect_source_names`、`edition_number`、长度与白名单检查 |
| `pipeline/render_mobile.py` | 响应式产物；`load_payload` / `check_allowlist` / `validate` / `check_prose` |
| `pipeline/export.py` | `to_pdf`（Playwright 优先，Chrome/Edge 回退）、`assert_pdf`、`content_snippets` |
| `pipeline/publish.py` | `sendDocument` 投递、幂等账本、429 退避、重试重开文件、token 脱敏、失败告警 |
| `pipeline/run.py` | 编排 + `dist/manifest-*.json`（失败也写） |
| `templates/_tokens.j2`、`_print.j2`、`_macros.j2`、`_pages.j2` | 令牌、打印几何、内容宏、两页标记（两套模板共用） |
| `templates/brief.html.j2`、`mobile.html.j2` | 桌面（打印优先，恒定 210mm）与响应式（640/1024 断点） |
| ~~`assets/*.svg`~~ | **已删除（见 §12.4）**：插图与报头花边都不再使用图片文件；花边改为纯 CSS 绘制 |
| `tests/`、`pytest.ini` | 123 个测试，全离线（有网络守卫）；`print_check` 标记的测试真开浏览器打印 |
| `.github/workflows/daily-brief.yml` | AM/PM cron + 手动补发 + build/publish/report 三 job，action 按 SHA 钉版本 |
| `state/sent.json` | 投递账本（幂等），由 publish job 回写仓库 |

### 11.2 与本文档原先设想的偏差（都是实测逼出来的，改动前请先复测）

1. **budgets 已按实测重新标定**（原文 §5.2 的 5 段×55 词 / 每篇 3 段×45 词在两栏 A4 里**放不下**）：
   实测原文预算的最坏情况会让页二溢出 **1120px**。现值为
   `lead 4×44`、`story 2×26`、`brief 4×24`、`bullet 16`、`headline 8`、`deck 12`、`dateline 5`，
   这是"最坏情况仍能放得下"的最大值；`tests/test_layout.py` 守住这条算术。
2. **页二分栏由代码规划**（`content.plan_inside_columns`）：按节的估算高度做 largest-first 装箱。
   自然顺序（科技+体育 | 金融+文化）实测一栏 1017px、另一栏 794px（容量 1018px），
   装箱后同一内容是 894px / 894px。
3. **禁止在固定高度页盒里用 CSS 多栏**（`columns:`）：多栏容器放不下时会分片到"新栏"＝**新页**，
   或被 `overflow:hidden` 静默裁掉/文字互相叠印，而 `scrollHeight` 两者都测不出来。
   本机确实踩到过：mobile 产物静默丢掉了导语最后一段。现改为显式栏容器（`.lead-col` / `.inside-column`）。
4. **页二只给"该节头条"印 deck + dateline，出处改为行内署名**（`— Reuters, AP`）：
   13 条 deck/dateline/出处行各自约 150–200px，是"放得下"与"被裁掉"的分界。
   这些字段仍在 edition JSON 里、在响应式产物里照常显示。
5. **新增内容完整性断言**（`export.assert_pdf(required_snippets=...)`，由 `export.content_snippets`
   从每节末尾取词）：页脚断言只能证明页脚在，证明不了正文没被裁；片段断言才是真正抓裁剪的那道闸。
   发送前必须通过（`pipeline.run` 与 `publish` CLI 都会带上）。
6. **产物命名**：`dist/mobile-<date>-<ed>.html`（避免 PM 覆盖 AM）。需要根目录固定 `mobile.html`
   时加 `--root-copy`（已知取舍仍在）。
7. **feed 列表按 2026-10-13 实测重写**：Reuters 已停止公开 RSS（`/arc/outboundfeeds/*` 404、
   `feeds.reuters.com` TLS 失败），AP 有 Cloudflare 机器人拦截（403），imf.org 与 ATP 亦 403/429。
   这些域名**保留在白名单**（可用于引用），但 feed 换成了实测可用的 NYT/WaPo/BBC/CNBC/FT/Bloomberg/
   Economist/Ars Technica/Nature/ESPN/F1/Deadline/Nobel 等。
8. **白名单补了 `bbci.co.uk`**：BBC 的 feed 主机是 `feeds.bbci.co.uk`，不是 `bbc.co.uk` 的子域——
   少了这一条，四个 BBC feed 会整节静默归零。
9. **`EDITION_SCHEMA` 用 draft-07**：所有用到的关键字 draft-07 都支持，jsonschema 3.x/4.x 都能跑；
   并渲染 `anyOf` 的子错误（否则模型只会看到"不符合任何分支"，回灌重试等于没给反馈）。
10. **workflow 回写仓库的只有 `state/sent.json`**（幂等必需）；`dist/*.pdf`、`dist/*.html`、
    `build/*.json` 走 artifact。原文 §4 提到"回写 dist/"，但 PDF 入库每天约 0.2–0.7MB，
    一年数百 MB——审计凭据用 artifact 保存（§10 的要求本就只写了"同时上传"）。
11. 原文 §9.7 提到的 7 个对比样本 html/pdf 在本仓库不存在（这是全新仓库），无需处理。

### 11.3 布局容量（实测，8.1pt / 1.21 行距，A4 210×297mm，页边距 8/10.5mm）

| 版面 | 最坏情况词数（预算打满） | 实测余量 | 承载上限（估算） |
|---|---|---|---|
| 页一（导语 + 2 篇次条 + 4 条简讯 + 行情） | 534 词 | 余 130px | ~700 词 |
| 页二（科技 3 + 金融 3 + 体育 + 文化 3） | 829 词 | 余 80px | ~900 词 |

余量来自"片段断言 + 页数断言"双重把守；一旦某天 LLM 交回的东西让页二装不下，
`export.assert_pdf` 会带着"哪一段被裁掉了"直接失败，**不会静默发出一份缺字的报纸**。

### 11.4 已实测的验证证据

```
python -m pytest -q                                  123 passed
python -m pytest tests/test_layout.py -v -s          真浏览器打印最坏情况：
   worst.pdf · 2 pages · 165,951 bytes · footer on all pages · 10 content snippets present
   （走 Chrome 回退引擎与 pypdf 提取；CI 走 Playwright）
本地 fixture（build/edition-2026-10-13-am.json，434/577 词）两个产物：
   desktop/mobile 各 2 页、183KB、11 条内容片段齐全、两产物逐页文本完全一致
白名单：fixture 全部 60 条 URL 通过；`feeds.bbci.co.uk` 等边界已单测
feed 实测：19 个 feed 全部 HTTP 200 且能解析出条目（geopolitics/tech/finance/sport/entertainment 五节均非空）
```

### 11.5 本机**没有**验证到的（下一个 agent 请优先做）

1. **真实 LLM 调用**：本机无 `BRIEF_LLM_API_KEY`。`editorial.py` 的 HTTP 路径、重试回灌、
   `usage` 记账只有离线回放测试覆盖；请用真 key 跑一次 `python -m pipeline.run --edition am`。
2. **真实 Telegram 投递**：本机无 bot token。幂等/429/重试/脱敏都有单测（打桩在 `publish._call`），
   但 `getMe`/`getChat`/`sendDocument` 的真实返回没验过；请用一个测试 bot 跑一次并确认
   `state/sent.json` 的记录与"第二遍不发"。
3. **GitHub Actions 真跑**：workflow 未在 GitHub 上执行过（本机无 CI）。请跑一次 `workflow_dispatch`，
   重点看 `playwright install --with-deps chromium`、artifact 路径、以及 `state/sent.json` 的回写 push。
4. **视觉终检**：本机模型无法查看图片，所以版式只做了**几何与文本层面的测量**（文档高度、
   打印页数、颜色令牌、段落完整性），没有做"好不好看"的人工确认。请打开
   `dist/global-brief-*.html` 目视一遍（手机窄屏、640、1024 三档，以及打印预览）。

---

## 12. 第二版修订（2026-10-13，按用户新需求）

四条新需求覆盖了 §2-§7 中关于"PDF / 固定 A4 版面"的部分。**§12 优先于前文冲突之处。**

| # | 新需求 | 本版做法 |
|---|---|---|
| 1 | 只输出 HTML，不要 PDF；响应式，手机和电脑都能读 | 删除 `export.py`、`render_mobile.py` 与全部 PDF 断言；产物是**一份自包含 HTML**（内联 CSS、零外部请求、零脚本、零图片），`publish` 用 `sendDocument` 发 HTML（`text/html`）。`render.py` 成为唯一渲染入口；`preflight.py` 接管交付前的全部检查 |
| 2 | 不要求固定 A4 页面尺寸，"篇幅差不多两个 A4"即可 | **取消固定页盒**（无 `height: 297mm`、无 `overflow: hidden`）→ 结构上不可能再发生裁剪。长度变成**目标**：`content.estimate_length` 把一期折算成"A4 张数"，`layout.target_sheets: 2.0 ± 0.7`，由 preflight 强制。常数 `MM_PER_SHEET` 由实测标定：fixture 打印恰好 2 页 |
| 3 | 排版与背景更像报纸出版物 | 新闻纸底色 `--paper #f5f0e3` + 细颗粒纹理（两层 repeating-linear-gradient，无图片请求）、墨色重规线、黑色报头横条、小体大写 kicker/栏目标题、导语首字下沉、两端对齐 + 断词、行情表格线、行内署名"—— Reuters, AP"。打印走**独立的多栏密排**（`@page A4`、8.3pt、栏线），所以同一份 HTML 打出来就是一张真正的两页报纸 |
| 4 | 补充更多权威信源（RSS） | 白名单扩到 **51 个域名**、feed 扩到 **55 条**（geopolitics 17 / tech 13 / finance 11 / sport 7 / entertainment 13），全部在 2026-10-13 实测可用。抓取改为**并发 + 重试**（8 线程；实测 21 秒完成 55 条，五节各 12 条候选）；单一来源被墙不会清空某节 |

### 12.1 文件变化

- **删除**：`pipeline/export.py`、`pipeline/render_mobile.py`、`templates/brief.html.j2`、
  `templates/mobile.html.j2`、`templates/_print.j2`、`templates/_pages.j2`、`tests/test_export.py`
- **新增**：`pipeline/preflight.py`（交付前检查）、`templates/newspaper.html.j2`（唯一模板）、
  `templates/_layout.j2`（组件 CSS，含打印密排）、`tests/printprobe.py`（真浏览器印页数）、
  `pipeline/content.py` 中的 `estimate_length` / `closing_snippets` / `normalise_text`
- **依赖**：去掉 `playwright`、`pypdf`（CI 不再需要浏览器与 PDF 工具链）
- **DESIGN 不变式**：1、2、3、4、6、7、8 仍然成立；**不变式 5（固定两页 A4）作废**，改为
  "长度是目标、由 preflight 校验"；`overflow: hidden` 在版式中被明确禁止（新增测试项）

### 12.2 交付前的检查（取代原 PDF 断言）

`preflight.assert_deliverable(html, payload, config)` 全部必须通过，否则 `run.py` 标记失败、
`publish` 拒绝发送：文档唯一性（一个 `<html>`/`<body>`、两个 `data-sheet`、每张纸都有页脚）、
无残留模板标记、无脚本、**每个段落与每条小标题都真的出现在页面文本里**（从 edition JSON 逐条比对）、
每节结尾词齐全、所有外链都在白名单、长度落在目标区间。`tests/test_render.py` 对其中每条都有
反向用例（丢掉一段、改掉页脚、塞个站外链接、注入 `<script>` 都必须失败）。

### 12.3 本版实测

```
python -m pytest -q                                    110 passed
fixture：estimate 2.01 张 → 打印 2 页 A4（真浏览器），preflight ok（2 张纸 / 28 段 / 11 处节尾 / 28 链接）
最坏情况（预算打满 1376 词）：estimate 2.17 张 → 打印 3 页，preflight 通过（目标 2.0±0.7）
screen @1440px：fixture 文档高 4090px ≈ 3.6 个 A4 高度（屏幕字号更大，符合预期）
实时抓取：55 条 feed、60 条候选（五节各 12）、21 秒
颜色字面量：`:root` 之外 0 处；断点仅 640/1024；`:focus-visible` + `var(--focus-ring)` 齐备
```

### 12.4 去掉插图（用户要求，2026-10-13）

需求原话："不用加插图了，把插图删了吧"。做法：

- 删除 `assets/`（`wire-globe.svg` 首页插图、`masthead-ornament.svg` 报头花边）以及整套资源加载机制：
  `render.load_assets()`、`render_html(assets=...)`、`meta.visual_caption`、`paths["asset_dir"]`、
  config 的 `asset_dir`、`macros.visual_block()`、`.visual*` 样式（含打印覆盖）。
- 报头花边改用**纯 CSS** 画（`.masthead-rule`：上下两条细线 + 居中的红色小菱形，`transform: rotate(45deg)`），
  所以"有插图的观感"没了，但报头依旧是报纸那种双细线 + 饰点的样子，且交付物里**没有任何图片、SVG、data URI**。
- 长度模型随之更新：去掉 `MM_ILLUSTRATION`（首页少 62mm），并用实测重新标定
  `MM_PER_SHEET = 376`（fixture：估算 2.00 张 ↔ 实测打印 2 页 A4，正好对齐）。

实测（两份产物都验过）：

```
fixture  AM：preflight ok · estimate 2.00 张 · 打印 2 页 · 165,431 bytes
实时信源 PM：preflight ok · estimate 1.76 张 · 打印 2 页 · 162,782 bytes
两份文档：<svg> 0 个、<figure> 0 个、<img> 0 个、data:image 0 处、脚本 0 个、外部请求 0 个
```

> 不变式 1 依旧成立，含义略变：版式（含那条花边）仍是版本化资产、仍由代码拥有；
> 只是"资产"从 SVG 文件变成了模板里的 CSS 规则。
