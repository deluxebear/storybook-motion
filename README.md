# Storybook Motion

**Storybook Motion** 是一个端到端的多模态 AI 动画制作流水线，专门将开源儿童绘本（如 Book Dash、StoryWeaver 等 CC BY 4.0 绘本）转化为高品质的有声动画绘本（Animated Storybooks）。

> 兼容性说明：Google Drive 上现有的 `MyDrive/vidio/` 路径暂时保留为内部存储命名空间，避免项目改名破坏已生成资产、模型缓存和断点恢复状态。

---

## 核心特性

- **保持原画艺术风格**：新项目默认基于 LTX-2.5 图像到视频工作流（模板默认 5 秒，24 fps；已有 H3 工程继续兼容），利用首帧原画单参考图驱动，默认生成 16:9 (0.9MP) 构图稳定的分镜视频。
- **高拟真多角色配音**：
  - 基于 **Qwen3-TTS VoiceDesign** 进行角色性格与音色定制。
  - 基于 **IndexTTS 2.5** 结合 8 维情感向量与语速系数进行高质量批量端到端台词合成。
- **智能音画时长对齐**：视频镜头的生成秒数会根据 TTS 实际合成语音时长动态加权更新（默认实测时长 + 1s 自然尾声），确保视频画面与台词完美对齐。
- **TTS 后提示词精修**：新 LTX 工程根据实测逐句配音时长，以确定性规则补充动作节奏、旁白时间线与结尾留白，保存最终提示词供提交和恢复使用。当前不是语言模型语义重写，也不保证逐词动作同步。
- **自动字幕文件**：L4 配音后用原始台词做强制对齐，最终合成时生成 UTF-8 SRT / WebVTT；对齐失败自动回退到 WAV 时长，无需另开 GPU 实例。
- **云端高效算力调度**：
  - 深度集成 **Google Colab CLI** 与 **Google Drive**。
  - 用户预先运行并挂载 Drive 的两个固定 **L4 GPU 会话**：`voice` 专用于 VoiceDesign，`tts` 专用于 IndexTTS 2.5 与字幕对齐；流水线只验证、投递和恢复，不创建、挂载或停止它们。
  - **A100 GPU 实例** 承载 ComfyUI 服务与最终云端视频拼接合成（`assemble_final.py`），零本地大文件吞吐。
- **AI Agent 原生设计**：
  - 大模型只负责理解素材并冻结 `production_plan.json`；冻结后的种子、清单、时长、提示词绑定、重试和合成都由确定性代码执行。
  - 仓库级 Supervisor 常驻监听 `books/`，按书幂等推进；进程重启、单阶段失败或临时端点中断后可从持久状态恢复。
  - 严格遵循**“输出分层”**与**“正常静默、异常取样”**哲学，最小化上下文 Token 开销。
  - 采用独立进程守护（Supervisor）机制，禁止无意义的轮询与休眠。

---

## 文档索引

- **AI Agent 指令与开发铁律**：[AGENTS.md](AGENTS.md)
- **Token 优化实践案例**：[docs/ai-agent-token-optimization-colab-case.md](docs/ai-agent-token-optimization-colab-case.md)
- **绘本动画核心技能**：[.agents/skills/bookdash-animation-pipeline/SKILL.md](.agents/skills/bookdash-animation-pipeline/SKILL.md)
- **ComfyUI 会话管理技能**：[.agents/skills/colab-comfyui-session/SKILL.md](.agents/skills/colab-comfyui-session/SKILL.md)

---

## 快速上手

```bash
# 1. 校验绘本生产计划
python .agents/skills/bookdash-animation-pipeline/scripts/pipeline.py validate --book-dir books/444797-where-is-your-school

# 2. 编译生产清单
python .agents/skills/bookdash-animation-pipeline/scripts/pipeline.py compile --book-dir books/444797-where-is-your-school

# 3. 启动仓库监听器（只需一次；固定使用现有 voice / tts 会话）
python .agents/skills/bookdash-animation-pipeline/scripts/pipeline.py watch-start

# 4. 单次查询监听器或某本书的当前进度
python .agents/skills/bookdash-animation-pipeline/scripts/pipeline.py watch-status
python .agents/skills/bookdash-animation-pipeline/scripts/pipeline.py status --book-dir books/444797-where-is-your-school
```

监听器包含相互独立的 `voice`、`tts`、`video`、`final` 四个单任务槽位。ComfyUI 完成一本书后立即释放 `video` 槽，下一本可以继续提交和生成；上一本文书在独立 `final` 槽中继续合成。Final 仍运行于现有 `ComfyUI` 会话，但仅使用低 CPU/I/O 优先级、受限线程数的 CPU FFmpeg，不占用 GPU。VoiceDesign 完成后，持久化状态会让该书在下一轮扫描直接进入 `tts`，不会再次生成音色。监听器从仓库根目录的 `url` 文件读取 ComfyUI 地址；地址缺失或健康检查失败时，只阻塞 `video` 槽位，已完成的语音结果保持不变，其他槽位继续处理。监听器写入 `.pipeline/notifications.jsonl` 并尽力发送桌面通知，待用户更新 `url` 后自动续跑。单本书也可以用 `pipeline.py start --book-dir ...` 投递，同样复用 `voice` 和 `tts`。

## 字幕工作流

默认情况下，监听器或单本 Supervisor 会在全部镜头成功后把书交给独立 `final` 阶段，在现有 `ComfyUI` 会话中生成带旁白的 MP4 以及 SRT/VTT 字幕边车文件。它不会占用视频调度槽，CPU 编码也使用低优先级和受限线程。会话名称不同时使用 `--assembly-session <现有会话名>`；只有明确只需要分镜视频时才传 `--no-assemble`。视频、音频和字幕均保存在该书的 Drive 目录中。

默认产物：

- `video/final/narrated_final.srt`：播放器和剪辑软件使用的外挂字幕。
- `video/final/narrated_final.vtt`：网页播放器字幕。
- `video/final/narrated_final.subtitles.json`：每条字幕的台词 ID、起止毫秒、文字和文件校验值；`assembly_manifest.json` 同时记录字幕结果。

每条 TTS 台词对应一条字幕。`tts` L4 上的 TTS 子进程退出后，自动在独立 Python 环境中运行 Qwen3-ForcedAligner-0.6B（`qwen-asr==0.0.6`），保存词级时间戳到 `qa/subtitle_alignment.json`；任务结束只释放本地会话锁，`tts` Colab 会话继续运行。模型缓存直接写入 Drive 的 `vidio/models/huggingface/`，运行日志保存在该书的 `qa/subtitle_alignment.log`。

A100 合成时读取对齐结果，以首词开始和末词结束作为字幕范围，字幕文字仍使用原始台词。最终字幕 JSON 同时包含换算到成片时间轴的词级时间戳，便于后续实现逐词高亮；SRT / WebVTT 目前仍按台词显示，不自动切分长句。镜头间按合成片段实测时长累计，保留尾部留白和无对白镜头。

对齐检查包括时间单调、正时长、音频边界和原文字符覆盖；这些检查不等于人工听审或发音正确性认证。语言不支持、安装失败、模型异常、对齐校验失败时，受影响台词回退到完整 WAV 时长，并记录 `fallback_count` 和逐条原因。安装上限 10 分钟，对齐批次上限 30 分钟（仍受 Supervisor 的总阶段超时约束）。已通过校验的结果按台词、语言和 WAV 内容哈希复用；音频变化时不使用旧时间戳。

当前支持中文、英文、粤语、法语、德语、意大利语、日语、韩语、葡萄牙语、俄语和西班牙语，依据 [Qwen 官方说明](https://github.com/QwenLM/Qwen3-ASR)。已有项目若 TTS 阶段已完成，恢复时不会为了补对齐重新分配 L4；没有可用对齐结果时，合成阶段自动使用台词时长方案。

重新执行同一合成任务时，若成片复用校验通过，会补齐或修复字幕而不重新编码视频。字幕默认不烧录进画面。仅运行 TTS 或视频生成阶段不会生成最终字幕，因为此时最终时间轴尚未确定。
