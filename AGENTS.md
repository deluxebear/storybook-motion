# AGENTS.md - Vidio Project Agent Guidelines

> **AI Agent 必读**：本文件是当前仓库的执行规范。先确认目标书籍、运行环境和现有状态，再修改或启动任务。

---

## 1. 核心上下文与指引
- **项目说明**：先读 [README.md](README.md)；需要远程交互时读 [token 优化文档](docs/ai-agent-token-optimization-colab-case.md)。
- **绘本动画流水线**：涉及绘本、TTS、渲染或合成时，读取 [.agents/skills/bookdash-animation-pipeline/SKILL.md](.agents/skills/bookdash-animation-pipeline/SKILL.md)。
- **ComfyUI 会话**：涉及启动、恢复或检查 ComfyUI 时，读取 [.agents/skills/colab-comfyui-session/SKILL.md](.agents/skills/colab-comfyui-session/SKILL.md)。
- `PROJECT_MEMORY.md` 当前不存在；若后续补充，应作为项目记忆参考，不覆盖本文件。

---

## 2. 行为铁律 (Strict Invariants)

1. **异步任务交给 Supervisor**：
   - 长时间运行任务（TTS、ComfyUI 渲染、合成）交由独立的守护进程（Supervisor）托管。
   - Agent 触发启动后立即汇报 `launched_pid`、日志路径并交还控制权。
   - 只有在用户主动询问或下一步触发时，单次执行 `pipeline.py status` 查看状态。

2. **输出保持结构化且短**：
   - 远程 Colab 命令禁止直接回传全量 pip/进度条/冗长日志。
   - “正常静默、异常取样”：成功仅输出结构化探针确认结果；失败仅保留末尾 20 行核心错误。

3. **GPU 生命周期安全**：
   - VoiceDesign 固定使用用户预先运行并挂载 Drive 的 L4 会话 `voice`；IndexTTS 与字幕对齐固定使用会话 `tts`。
   - 仓库监听器使用独立的 `voice`、`tts`、`video` 单任务槽位；不同绘本可跨阶段并行。`voice_design` 已成功的绘本必须直接路由到 `tts`，不得重做音色。
   - 流水线不得创建、挂载、停止或重命名这两个会话；只允许健康探针和任务投递。
   - ComfyUI 运行在用户指定的 A100 High-Mem 实例上，绝不擅自降级、多重创建或擅自终止用户运行中的 ComfyUI 实例。

4. **数据与幂等性**：
   - 所有模型和生成媒体资产直接写往挂载的 Google Drive：`/content/drive/MyDrive/vidio/`。
   - 本地工作区禁止下载大型 MP4 视频或大型模型权重文件，只同步 JSON 元数据和轻量 QA 报告。
   - 生产计划通过 `plan_hash` 冻结。已提交或成功的镜头任务保持幂等，不得无故覆写。

5. **先检查后操作**：
   - 修改前查看相关文件和 `git diff`，保留用户已有改动。
   - 运行前确认目标目录、ComfyUI URL、Drive 挂载和当前状态；缺少必要条件时只报告阻断原因。
   - 不下载大型 MP4、模型权重或全量远程日志到本地。

6. **完成标准**：
   - 修改后只运行与改动相关的最小验证；报告命令、结果和未解决问题。
   - 不提交、推送、停止用户实例或删除数据，除非用户明确要求。

---

## 3. 常用命令入口

```bash
# 校验绘本工程规范
python .agents/skills/bookdash-animation-pipeline/scripts/pipeline.py validate --book-dir books/<book_slug>

# 编译生成运行时清单
python .agents/skills/bookdash-animation-pipeline/scripts/pipeline.py compile --book-dir books/<book_slug>

# 启动 books/ 常驻监听器（启动后立即返回 PID 和日志）
python .agents/skills/bookdash-animation-pipeline/scripts/pipeline.py watch-start

# 查询监听器
python .agents/skills/bookdash-animation-pipeline/scripts/pipeline.py watch-status

# 单次查询流水线状态
python .agents/skills/bookdash-animation-pipeline/scripts/pipeline.py status --book-dir books/<book_slug>
```
