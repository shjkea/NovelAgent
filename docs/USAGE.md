# NovelAgent 使用说明

[English](USAGE_EN.md) · [返回 README](../README.md)

## 1. 环境准备

- Python 3.10+
- 一个可用的 DeepSeek 兼容 API 账户
- 可选：xAI API，用于非 Canon DLC 扩写
- 可选：`llama-server` 与 GGUF embedding 模型，用于向量记忆

建议先在私有目录中运行，不要直接把自己的写作工作目录设为公开 Git 仓库。

### Windows

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item config.example.json config.json
python app.py
```

也可以双击 `启动NovelAgent_控制台.bat`。确认运行正常后，再使用隐藏窗口版本。

### Linux / macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp config.example.json config.json
export DEEPSEEK_API_KEY='replace-me'
export XAI_API_KEY='replace-me'   # optional
python app.py
```

不要把含真实值的 `export` 命令写进会提交到 Git 的脚本。Linux/macOS 上通过网页“保存 Key”会被拒绝，这是为了避免弱加密落盘。

## 2. 初始配置

首次运行会在缺少 `config.json` 时从 `config.example.json` 自动复制一份。本地配置已被 `.gitignore` 排除。

常用字段：

| 字段 | 说明 |
| --- | --- |
| `web.host` | 默认 `127.0.0.1`，只允许本机访问 |
| `web.port` | 默认 `7860` |
| `deepseek.source` | `official` 或 `volcengine_agent_plan` |
| `generation.chapters_per_run` | 每次连续生成章数 |
| `generation.target_chapter_chars` | 单章参考字数 |
| `embedding.enabled` | 是否使用 embedding 检索 |
| `embedding_server.auto_start` | 是否由 NovelAgent 启动本地 embedding 服务 |
| `context.outline_range_overrides` | 超长作品按章节区间拆分大纲时使用 |
| `external_canon.ranges` | 交给外部流程生产的 Canon 章节区间 |

模型名称必须与账户和端点实际支持的名称一致。如果默认模型不可用，请直接修改 `config.json` 中对应的 `model` 字段。

## 3. 准备故事资料

生成前至少维护以下文件：

| 文件 | 建议内容 |
| --- | --- |
| `story/premise.md` | 作品核心、题材、主要矛盾、终局方向 |
| `story/world.md` | 世界机制、时代地点、力量或技术规则 |
| `story/characters_seed.md` | 人物身份、性格、目标、关系和知识边界 |
| `story/outline.md` | 按章节或章节段落组织的事件与结果 |
| `story/style.md` | 视角、语言、节奏、禁用表达和章节结构 |
| `story/author_notes.md` | 不直接参与正式设定优先级的作者备忘 |

不要在这些文件里写 API Key、真实住址、身份证号、私人通信记录或不能发送给第三方模型服务商的资料。调用云端模型时，相关上下文会离开本机。

## 4. 配置 API 与登录

1. 打开 `http://127.0.0.1:7860`。
2. 首次访问设置管理员用户名和至少 8 位密码。
3. 在 API 面板选择 DeepSeek 官方或火山 Agent Plan。
4. Windows 可在网页中保存 Key；它会写入 `runtime/*.dpapi`。
5. 如需 DLC 扩写，再配置 xAI Key。
6. 点击测试连接，确认服务状态正常。

更换 Windows 用户、重装系统或迁移到另一台机器前，应准备重新输入 API Key；DPAPI 文件不是可移植密码库。

## 5. 配置向量记忆

向量记忆不是 Web 启动的硬依赖，但对长篇召回质量很重要。

1. 准备支持 embedding 的 `llama-server`。
2. 准备 GGUF embedding 模型。
3. 修改 `config.json`：

```json
{
  "embedding_server": {
    "auto_start": true,
    "llama_server_path": "C:\\path\\to\\llama-server.exe",
    "model_path": "C:\\path\\to\\embedding-model.gguf",
    "host": "127.0.0.1",
    "port": 8081
  }
}
```

实际 `config.json` 必须保留其他字段，不要用上面的片段覆盖整个文件。也可以关闭自动启动，自行提供兼容 OpenAI embeddings 接口与 `/health` 的服务。

## 6. 生成 Canon

1. 确认故事资料和当前 `state.json` 的下一章编号。
2. 在控制台选择费用策略、章数、目标字数和修订轮数。
3. 启动 Canon。
4. 观察 Plan、Draft、Review、Revision、Summary/Memory 阶段。
5. 如果 Plan 上下文或历史成本触发保护，检查提示后手动继续或取消。

通过最终质量门后，系统会写入：

- `chapters/NNNN.md`
- `plans/NNNN.md`
- `reviews/NNNN.json`
- `summaries/NNNN.md`
- `handoffs/NNNN.json`
- `runtime/state_snapshots/NNNN.json`
- `novel_memory.sqlite3`

不要在运行中直接编辑这些文件。

## 7. 审计、修复与回滚

- “剧情一致性审计”按重叠窗口检查跨章问题，并生成 `reports/` 报告。
- “审计驱动修复”先创建候选，不会立即覆盖 Canon。
- 查看候选 diff、局部校验与联合复核结果后，再显式提交。
- 提交和从某章重写前会创建 `archive/` 归档。
- 如果提交后人工又修改了正文，回滚会通过哈希识别漂移，避免静默覆盖。

即使程序显示通过，也建议人工抽查人物动机、语气、隐含关系和风格质量；这些不适合完全依赖规则判断。

## 8. DLC、读者版与导出

- DLC 只处理正文中的 `<DLC_SCENE .../>` 标记，结果写入 `dlc/`，不自动改变 Canon。
- `prompts/expansion_reference.md` 是公开的通用参考，可替换成自己的安全资料。
- 读者版分段只允许改变段落边界，并进行逐字校验，输出到 `reader_chapters/`。
- 导出支持 Markdown、纯文本和 ZIP。导出前再次确认范围与缺章提示。

## 9. 常见问题

### 页面打不开

检查终端是否显示 Uvicorn 已监听 `127.0.0.1:7860`，以及 7860 端口是否被其他程序占用。

### API 显示未配置

Windows 请重新保存 Key；Linux/macOS 请确认环境变量是在启动 `python app.py` 的同一 shell 中设置。

### Embedding 一直停止

默认 `auto_start=false`。若已开启，检查可执行文件、模型路径、8081 端口和 `logs/embed_stderr.log`。

### 没有 embedding 是否能写

可以。数据库与全文检索仍可用，但相关长期记忆的语义召回会变弱。

### 准备发布自己的分支

发布前运行：

```powershell
python -m pytest -q
git status --short
git ls-files
```

逐项确认 `config.json`、`runtime/`、数据库、日志、正文、私人设定和压缩备份不在 `git ls-files` 输出中。已经提交过的密钥必须先在服务商侧轮换；仅从最新提交删除并不能清除 Git 历史。
