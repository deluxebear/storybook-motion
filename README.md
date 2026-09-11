# vidio - AI Animated Storybook Pipeline

`vidio` 是一个端到端的多模态 AI 动画制作流水线，专门将开源儿童绘本（如 Book Dash、StoryWeaver 等 CC BY 4.0 绘本）转化为高品质的有声动画绘本（Animated Storybooks）。

---

## 核心特性

- **保持原画艺术风格**：基于 MiniMax H3 图像到视频模型，利用首帧原画单参考图驱动，默认生成 16:9 (0.9MP) 构图稳定的分镜视频。
- **高拟真多角色配音**：
  - 基于 **Qwen3-TTS VoiceDesign** 进行角色性格与音色定制。
  - 基于 **IndexTTS 2.5** 结合 8 维情感向量与语速系数进行高质量批量端到端台词合成。
- **智能音画时长对齐**：视频镜头的生成秒数会根据 TTS 实际合成语音时长动态加权更新（默认实测时长 + 1s 自然尾声），确保视频画面与台词完美对齐。
- **云端高效算力调度**：
  - 深度集成 **Google Colab CLI** 与 **Google Drive**。
  - **L4 GPU 实例** 按需分配用于运行 TTS，执行完毕立即自动释放，杜绝闲置计费。
  - **A100 GPU 实例** 承载 ComfyUI 服务与最终云端视频拼接合成（`assemble_final.py`），零本地大文件吞吐。
- **AI Agent 原生设计**：
  - 严格遵循**“输出分层”**与**“正常静默、异常取样”**哲学，最小化上下文 Token 开销。
  - 采用独立进程守护（Supervisor）机制，禁止无意义的轮询与休眠。

---

## 文档索引

- **项目核心记忆与全景技术手册**：[PROJECT_MEMORY.md](PROJECT_MEMORY.md)
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

# 3. 启动后台流水线（自动拉取 L4 执行语音设计与合成，并向 ComfyUI 提交视频）
python .agents/skills/bookdash-animation-pipeline/scripts/pipeline.py start \
  --book-dir books/444797-where-is-your-school \
  --comfy-url $(cat url)

# 4. 单次查询当前进度
python .agents/skills/bookdash-animation-pipeline/scripts/pipeline.py status --book-dir books/444797-where-is-your-school
```
