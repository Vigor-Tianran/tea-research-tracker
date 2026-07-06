# 茶叶方向每周学术进展追踪程序

这是一个面向农艺与种业专业、茶叶方向研究生的轻量级文献雷达。它会在每周一上午 9 点（北京时间/新加坡时间）自动检索最近 7 天的茶树相关论文，去除重复记录，按研究方向分类，并把 Markdown 周报保存到 `reports` 文件夹。

你不需要会编程也能使用。日常情况下，只需在 GitHub 网页上查看每周自动生成的报告；需要调整研究方向时，修改 `config.yaml` 里的关键词即可。

## 程序会做什么

- 检索 Crossref、PubMed 和 Semantic Scholar 的公开接口。
- 同时使用中英文关键词，覆盖茶树、品质、育种、种质资源、代谢组、基因组和 SSR 标记。
- 只保留最近 7 天的论文，按 DOI 或标题去重。
- 输出本周概览、重点论文、分类论文列表、完整元数据、摘要和课题启发。
- 每周一自动运行，并把新报告提交回本仓库。

> 注意：免费数据库可能存在收录延迟或元数据缺失。周报适合发现新文献，正式引用前请打开 DOI 或期刊网页核对原文。

## 项目结构

```text
tea-research-tracker/
├─ main.py                         # 主程序
├─ config.yaml                     # 关键词和运行参数
├─ requirements.txt                # Python 依赖
├─ README.md                       # 使用说明
├─ tests/test_smoke.py              # GitHub Actions 离线冒烟测试
├─ reports/                        # 每周报告
└─ .github/workflows/
   ├─ weekly-tracker.yml           # 每周自动检索与生成报告
   └─ project-checks.yml            # 代码变更时执行离线自检
```

## 如何查看每周报告

1. 打开本仓库首页。
2. 点击 `reports` 文件夹。
3. 点击最新的 `weekly_report_YYYY-MM-DD.md`。
4. GitHub 会自动把 Markdown 显示成排版好的网页。

文件名中的日期是报告生成当天的北京时间，例如 `weekly_report_2026-07-06.md`。

## 如何修改关键词

1. 在仓库首页点击 `config.yaml`。
2. 点击右上角铅笔图标（Edit this file）。
3. 找到 `categories:`，在相应方向下增加或删除关键词。
4. 点击页面下方的 **Commit changes** 保存。

示例：在品质方向增加“茶叶香气”和英文词组 `tea aroma`：

```yaml
categories:
  茶叶品质:
    - 茶叶品质
    - tea quality
    - 茶叶香气
    - tea aroma
```

编辑 YAML 时请注意：

- 每个关键词前保留四个空格和一个短横线。
- 英文词组不需要额外加引号。
- `SSR` 之外还配置了 `simple sequence repeat` 和 `microsatellite marker`，可减少缩写歧义带来的漏检。
- `rows_per_keyword` 控制每个关键词、每个数据源最多取回多少条记录；通常不必修改。

## 如何手动运行程序

### 方法一：直接在 GitHub 网页运行（最适合没有编程基础的用户）

1. 打开仓库顶部的 **Actions**。
2. 在左侧点击 **Weekly Tea Research Tracker**。
3. 点击右侧 **Run workflow**。
4. 再点击绿色的 **Run workflow** 按钮。
5. 等待几分钟，刷新仓库的 `reports` 文件夹即可看到新报告。

### 方法二：在自己的电脑运行

电脑需要先安装 Python 3.11 或更高版本。下载本仓库并解压后，在项目文件夹中打开终端，依次运行：

```bash
python -m pip install -r requirements.txt
python main.py
```

Windows 如果提示找不到 `python`，可尝试：

```powershell
py -m pip install -r requirements.txt
py main.py
```

指定某一天作为报告结束日期：

```bash
python main.py --date 2026-07-06
```

只在终端预览、不保存文件：

```bash
python main.py --dry-run
```

## GitHub Actions 如何自动运行

自动任务位于 `.github/workflows/weekly-tracker.yml`。

- 定时表达式是 `0 1 * * 1`，即每周一 01:00 UTC。
- 北京时间和新加坡时间都是 UTC+8，因此对应每周一上午 9:00。
- GitHub 的定时任务有时会因平台繁忙延迟几分钟，这是正常现象。
- 工作流会安装依赖，先运行不联网的冒烟测试，再运行 `main.py`，最后把 `reports` 中的新报告自动提交回 `main` 分支。

第一次使用时，请检查仓库：**Settings → Actions → General → Workflow permissions**，确保选择 **Read and write permissions**。本项目的工作流也声明了 `contents: write`，两者共同决定它能否自动提交报告。

## API Key 是否必需

默认情况下不需要 API Key，三个数据源都可以直接运行：

- **Crossref**：免费公开。建议设置联系邮箱，便于使用其 polite pool。
- **PubMed**：通过 NCBI E-utilities 免费访问。低频的每周任务通常不需要 Key。
- **Semantic Scholar**：基础接口可匿名访问，但匿名额度较低，偶尔可能限流。匿名接口一旦限流，程序会跳过其余 Semantic Scholar 请求，并继续使用 Crossref 和 PubMed 生成报告。

如需提高稳定性，可在仓库 **Settings → Secrets and variables → Actions** 中添加以下可选 Secrets：

| Secret 名称 | 用途 | 获取方式 |
|---|---|---|
| `CONTACT_EMAIL` | Crossref 和 PubMed 的联系邮箱 | 填写你自己的常用邮箱 |
| `NCBI_API_KEY` | 提高 PubMed E-utilities 请求额度 | 登录 NCBI 后在账户设置中创建 |
| `S2_API_KEY` | 提高 Semantic Scholar 接口额度 | 按 Semantic Scholar 官方说明申请 |

不要把 API Key 直接写进 `config.yaml` 或提交到仓库。GitHub Secrets 会在运行时安全地传给程序。

## 常见问题

### 报告中为什么有论文没有摘要？

部分出版社没有向 Crossref 提交摘要，某些数据库记录也只有题录信息。程序会优先合并多个数据源中更完整的记录，但仍可能显示“数据源未提供摘要”。

### 为什么本周论文数量很少或为零？

“最近 7 天”限制较严格，而且数据库收录常有延迟。可以稍后手动运行，或把 `config.yaml` 中的 `lookback_days` 临时改为 14。

### 自动任务失败怎么办？

打开 **Actions**，点击失败的运行记录，再展开红色步骤查看日志。最常见原因是免费接口临时限流；等待一段时间后手动点击 **Run workflow** 即可。

### “对硕士课题可能有启发的地方”是如何生成的？

它不是大模型结论，而是程序根据论文命中的研究方向，用公开透明的规则生成的阅读提示。请把它当作选文辅助，最终判断应基于论文全文、你的研究材料和导师建议。

## 数据来源与使用边界

本项目只保存公开题录和接口返回的摘要，不下载或绕过付费论文全文。请遵守各数据源的服务条款，并在论文写作中引用原始论文，而不是引用本周报。
