# AGENTS.md - Vidio Project Agent Guidelines

> **AI Agent 必读**：本项目是一个高度精细化设计的多模态自动化流水线项目（绘本 -> 动画视频与语音）。在执行任何修改或运行前，请先阅读本文件及 `PROJECT_MEMORY.md`。

---

## 1. 核心上下文与指引
- **权威项目记忆手册**：参见 [PROJECT_MEMORY.md](PROJECT_MEMORY.md)
- **绘本动画核心技能**：参见 [.agents/skills/bookdash-animation-pipeline/SKILL.md](.agents/skills/bookdash-animation-pipeline/SKILL.md)
- **ComfyUI 会话管理技能**：参见 [.agents/skills/colab-comfyui-session/SKILL.md](.agents/skills/colab-comfyui-session/SKILL.md)
- **Token 优化与远程交互范式**：参见 [docs/ai-agent-token-optimization-colab-case.md](docs/ai-agent-token-optimization-colab-case.md)

---

## 2. 行为铁律 (Strict Invariants)

1. **绝对禁止在循环中进行 Sleep 或轮询 (No Polling Loops)**：
   - 长时间运行任务（TTS、ComfyUI 渲染、合成）交由独立的守护进程（Supervisor）托管。
   - Agent 触发启动后立即汇报 `launched_pid`、日志路径并交还控制权。
   - 只有在用户主动询问或下一步触发时，单次执行 `pipeline.py status` 查看状态。

2. **输出分层与 Token 节约 (Token Optimization)**：
   - 远程 Colab 命令禁止直接回传全量 pip/进度条/冗长日志。
   - “正常静默、异常取样”：成功仅输出结构化探针确认结果；失败仅保留末尾 20 行核心错误。

3. **GPU 实例与费用安全 (GPU Lifecycle)**：
   - TTS 所需的 L4 实例独立归属各单本书，完成或失败后必须确保 `colab stop`。
   - ComfyUI 运行在用户指定的 A100 High-Mem 实例上，绝不擅自降级、多重创建或擅自终止用户运行中的 ComfyUI 实例。

4. **数据存储与持久化规范**：
   - 所有模型和生成媒体资产直接写往挂载的 Google Drive：`/content/drive/MyDrive/vidio/`。
   - 本地工作区禁止下载大型 MP4 视频或大型模型权重文件，只同步 JSON 元数据和轻量 QA 报告。
   - 生产计划通过 `plan_hash` 冻结。已提交或成功的镜头任务保持幂等，不得无故覆写。

---

## 3. 常用命令入口

```bash
# 校验绘本工程规范
python .agents/skills/bookdash-animation-pipeline/scripts/pipeline.py validate --book-dir books/<book_slug>

# 编译生成运行时清单
python .agents/skills/bookdash-animation-pipeline/scripts/pipeline.py compile --book-dir books/<book_slug>

# 启动执行流水线（需在 PTY 中挂载 Drive）
python .agents/skills/bookdash-animation-pipeline/scripts/pipeline.py start --book-dir books/<book_slug> --comfy-url $(cat url)

# 单次查询流水线状态
python .agents/skills/bookdash-animation-pipeline/scripts/pipeline.py status --book-dir books/<book_slug>
```
