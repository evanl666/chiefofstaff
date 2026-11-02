# Chief of Staff

[English](https://github.com/evanl666/chiefofstaff/blob/main/README.md) · **简体中文**

用一个收件箱管理你所有的 GitHub Copilot session。你说要做什么，它把每个任务交给一个单独的
Copilot session；后续的追问会发给已经了解情况的那个 session；Slack 消息它帮你起草回复；
事情做完了，再提醒你来检查。

![收件箱：做完的任务在等你检查](https://raw.githubusercontent.com/evanl666/chiefofstaff/main/docs/images/inbox-review.png)

可以先用脚本模拟的 session 体验一下，不需要登录任何账号：

```bash
uvx chiefofstaff --demo
```

## 为什么做这个

我平时会同时开好几个 Copilot session：一个修测试，一个看 PR，一个翻日志。每隔几分钟就得在
几个终端之间来回切，看哪个卡住了，哪个在等我回答，哪个已经悄悄做完了。中间 Slack 还不停地
@我。时间一长，我就记不清哪个 session 知道什么了，有时候一个旧 session 一分钟就能回答的追问，
我却开了个新的从头来。

现在我只跟其中一个 session 说话，也就是 Router，其余的交给 Chief of Staff 去管。

## 能做什么

- 把一句话拆成几个任务。比如"修好那个不稳定的登录测试，然后加上限流，再对比一下几个
  feature flag 服务"，会变成三个任务。第二个会等第一个做完，带着它的结果开始。
- 追问会发给做过这件事的那个 session，它有之前的对话和代码目录，不用从头再来。
- 把实际观察到的东西放在 worker 的汇报旁边：跑了哪些测试、提交了什么、改了多少行、PR 检查
  有没有过。如果汇报里说"测试全部通过"，但根本没看到跑测试，卡片上会直接标出来。
- 不碰你自己的代码目录。每个 worker 有自己的文件夹和 git worktree，跑在 Copilot 的系统沙箱里，
  写不到别的地方。
- 按你的语气起草 Slack 私信和 @ 的回复。你不点 Send，什么都不会发出去。
- 可以直接在 Slack 里委派：给任何一条消息点个 🤖（比如"谁能看下导出功能的 bug？"），它就
  变成一个任务。做完以后，回复会起草在那条消息的 thread 里。
- 专注模式：选一个时长，期间通知先压着，Router 能处理的自己处理，结束后给你一份总结。
- 发现卡住或者原地打转的 worker，会去推它一把。

它用的就是你现有的 Copilot：你的订阅、模型、MCP server 和公司的策略，不需要额外的 AI 服务
或 API key。

![批准一条 Slack 回复](https://raw.githubusercontent.com/evanl666/chiefofstaff/main/docs/images/approve-dark.png)

## 安装

需要 macOS 或 Linux、Python 3.11 及以上、[uv](https://docs.astral.sh/uv/getting-started/installation/)
（或者 pipx），以及 [GitHub Copilot CLI](https://docs.github.com/en/copilot/how-tos/copilot-cli)：

```bash
npm install -g @github/copilot
copilot login
uvx chiefofstaff            # 或者：pipx install chiefofstaff && chiefofstaff
```

浏览器会打开 `http://127.0.0.1:8765`。先添加一个项目文件夹，然后在底部的输入框里说你要做什么。

所有东西都在你自己的电脑上跑，数据存在 `~/Library/Application Support/chiefofstaff`（macOS）
或 `~/.local/share/chiefofstaff`（Linux）。关掉网页不会停掉任何东西，要停就在终端里按 Ctrl+C。
下次启动时，各个 session 会接着之前的进度继续。

常用参数：`--port`、`--data-dir`、`--no-browser`、`--demo`。升级：`uvx chiefofstaff@latest`
或 `pipx upgrade chiefofstaff`。

## 登录

一共有几处登录，只有第一个是必须的。

1. **Copilot**：运行一次 `copilot login`。如果还没登录，页面上会告诉你怎么做。公司账号可能
   需要管理员先为你开启 Copilot CLI。
2. **本地网页**：每次启动，终端里都会打印一个带新 token 的私密链接，并自动打开。登录状态保持
   24 小时，重启之后要用新的链接。这个链接别给别人，拿到它就能以你的身份操作。
3. **GitHub**：如果要让它帮你 review 别人请你看的 PR，需要 `gh auth login`。开着沙箱的话，还要
   运行一次 `copilot sandbox ca create && copilot sandbox ca trust`，这样沙箱里的 `gh` 才能用。
4. **Slack 和其他服务**：在 Settings 里粘贴 Slack user token
   （[怎么获取](https://github.com/evanl666/chiefofstaff/blob/main/docs/guide.md#slack)，英文）。
   worker 需要用到别的服务时，比如公司文档或 issue 系统，收件箱里会出现一张登录卡片。

## 工作原理

每个 session 都是普通的 Copilot CLI session，在 `copilot --resume` 里能找到，名字是
"Chief of Staff · …"。Chief of Staff 通过 Copilot SDK 来驱动它们：

```
你 ── 收件箱 ── Router ── 任务 ──► worker · worker · worker
                  ▲                (各自的文件夹、worktree、沙箱)
Slack、GitHub ────┘    Slack session ──► 等你批准的草稿
                       Watchdog ──► 推一把卡住的 worker
```

session 之间从不直接对话。它们调用 `spawn_worker`、`report`、`ask_session` 这类工具，由
Chief of Staff 负责传话。每条消息发出去之前都会先存下来，并标明是谁发的，所以一条 Slack 消息
没法冒充成你。

更多文档（英文）：

- [使用指南](https://github.com/evanl666/chiefofstaff/blob/main/docs/guide.md)
- [架构](https://github.com/evanl666/chiefofstaff/blob/main/docs/ARCHITECTURE.md)
- [安全说明](https://github.com/evanl666/chiefofstaff/blob/main/SECURITY.md)：哪些限制由系统沙箱
  保证，哪些由 Chief of Staff 保证，哪些要靠模型遵守指令。

## 当前状态

早期版本（0.1），会有不少粗糙的地方。Copilot 的用量和平时一样算在你的订阅里，收件箱会显示
每个任务用了多少 credits。

欢迎在 issue 里提 bug 和想法。想参与开发的话，可以先看
[CONTRIBUTING.md](https://github.com/evanl666/chiefofstaff/blob/main/CONTRIBUTING.md) 和
[适合新手的任务](https://github.com/evanl666/chiefofstaff/blob/main/docs/good-first-issues.md)。

## 许可证

MIT。Chief of Staff 是独立项目，不是 GitHub 出品，和 GitHub 也没有关联。GitHub Copilot 是
GitHub, Inc. 的商标。
