# 用输出分层优化 AI Agent：一个 Colab PyTorch 升级案例

## 摘要

AI Agent 执行远程命令时，真正消耗上下文的不一定是命令本身，而是命令产生的大量日志。本文以本项目中的 Colab PyTorch nightly 升级为例，展示如何让 Agent 继续“完整监听”和“可靠验证”，但只把必要信息返回给模型，从而降低 token 消耗、提高稳定性。

## 场景：升级 ComfyUI 的 PyTorch

目标是在名为 `ComfyUI` 的 Colab 会话中执行：

```bash
pip uninstall -y torch torchvision torchaudio
pip install --pre torch torchvision torchaudio \
  --index-url https://download.pytorch.org/whl/nightly/cu132
```

升级成功的判据不是“pip 命令退出了”，而是远程 Python 实际导入后的版本满足：

```text
2.15.0.dev<日期>+cu132
```

一次安装会输出下载进度、依赖解析、缓存命中、安装过程和警告。如果全部转发给 Agent，输出可能达到数千 token，但其中绝大多数对决策没有帮助。

## 关键区分：监听不等于回传

优化前，脚本逐行读取远程输出并立即打印：

```python
for line in proc.stdout:
    print(line, end="")
```

这会让模型看到每一行 pip 日志。优化后仍然逐行读取并保存输出，但不转发正常日志：

```python
output = []
for line in proc.stdout:
    output.append(line)
```

因此脚本仍然可以：

- 等待远程命令结束；
- 获取退出码；
- 在失败时分析日志；
- 在最后执行版本验证。

只是正常过程不再进入 Agent 的上下文。

## 失败时保留有用信息

完全丢弃错误输出会降低可诊断性。更合理的策略是只保留末尾少量日志：

```python
if return_code:
    print("远程命令失败，末尾输出：", file=sys.stderr)
    print("".join(output[-20:]), file=sys.stderr, end="")
    raise subprocess.CalledProcessError(return_code, proc.args)
```

这是一种“正常静默、异常取样”的输出策略：成功路径低 token，失败路径仍有足够线索定位问题。

## 用结构化验证替代日志猜测

安装日志中的 `Successfully installed` 不能证明当前内核导入的是新版本。脚本应单独执行一个最小验证单元：

```python
import torch
print("PyTorch version:", torch.__version__)
```

然后只匹配目标版本：

```python
TARGET_VERSION = re.compile(r"\b2\.15\.0\.dev\d+\+cu132\b")
match = TARGET_VERSION.search(verification)
if not match:
    raise SystemExit("PyTorch upgrade was not verified")
```

本案例最终只返回一行：

```text
PyTorch upgrade verified: 2.15.0.dev20260909+cu132
```

这比让 Agent 从几百行 pip 输出中寻找版本号更可靠，也更省 token。

## 一个可迁移的 Agent 命令模式

适用于安装、编译、下载模型、数据库迁移等任务：

1. 远程执行命令，后台完整读取输出；
2. 不回传正常进度日志；
3. 等待进程退出并检查退出码；
4. 失败时只回传最后 N 行，必要时再请求完整日志；
5. 用一个短小、结构化的探针验证最终状态；
6. 只向 Agent 返回状态、关键指标和错误摘要。

伪代码如下：

```python
output = run_remote_silently(command)
if output.exit_code != 0:
    report_tail(output.lines, count=20)
    fail()

probe = run_remote_silently("print(actual_state())")
if not matches_expected_state(probe):
    fail("verification failed")

report("success", actual_state=probe)
```

## 哪些输出应该保留

| 输出类型 | 建议 | 原因 |
|---|---|---|
| 下载百分比、进度条 | 丢弃 | 不影响决策 |
| 普通依赖解析 | 丢弃 | 成功时没有诊断价值 |
| 命令退出码 | 保留 | 判断流程是否完成 |
| 最终版本、文件路径、URL | 保留 | 是任务结果 |
| 错误最后 20 行 | 失败时保留 | 通常包含根因 |
| 完整日志 | 按需获取 | 只在摘要不足时使用 |

## 进一步优化建议

- 让远程程序自己输出 JSON，例如 `{"status":"ok","version":"..."}`。
- 对长任务使用心跳状态，而不是持续转发日志。
- 给每个步骤设置明确的超时和退出码。
- 将“验证命令”与“执行命令”分离，避免从安装日志推断状态。
- 只有在摘要无法诊断时，才升级为读取完整日志。
- 对重复运行使用幂等检查，例如先检查目标版本，已经满足时直接跳过重装。

## 结论

节省 token 的核心不是少做工作，而是减少无决策价值的输出。一个高质量 Agent 可以在远端完整监听、等待和验证，同时只把最终状态或有限错误摘要放入上下文。本案例中，原本可能产生大量 pip 日志的升级流程，最终压缩为一行可验证结果，并保留了失败时的诊断能力。

相关实现：[upgrade_pytorch_nightly.py](../.agents/skills/colab-comfyui-session/scripts/upgrade_pytorch_nightly.py)
