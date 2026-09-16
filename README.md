# Git Memory

A N.E.K.O plugin that keeps the `memory` folder in Git and pushes it to a
private GitHub / GitLab / Gitee repository, using GitPython.

一个把 `memory` 目录纳入 Git 并同步到 GitHub / GitLab / Gitee 私有仓库的
N.E.K.O 插件，底层使用 GitPython。

### 功能 / Features

- **启动检测** — 插件启动与面板打开时检测 Git、GitPython 与 memory 仓库状态。
- **缺失 Git 的引导** — 未安装 Git 时弹窗提示，按操作系统给出下载链接与安装命令：
  Windows（Git for Windows / winget / scoop）、macOS（Xcode CLT / Homebrew）、
  Linux（按 `/etc/os-release` 匹配 apt / dnf / zypper / pacman / apk / emerge / nix / xbps / eopkg）。
- **仓库初始化** — 检测 `memory` 下是否已有 `.git`，没有就 `git init` 并写入 `.gitignore` 与提交身份。
- **令牌向导** — GitHub / GitLab / Gitee 三选一，提供对应令牌创建链接与所需权限说明，
  校验通过后把令牌加密保存在插件私有数据目录。
- **关联仓库** — 读取账号下的仓库列表并关联为远端，或直接在账号下创建私有仓库并关联。
- **同步方式** — 手动同步，或每 5 / 10 / 30 / 60 分钟自动同步；同步 = 提交 → 拉取 → 推送。
- **可调整的 Git 设置** — 分支、远端名、提交信息模板、作者、代理、冲突策略、
  拉取开关、失败提醒、`.gitignore` 预设与追加规则。
- **AI 可调用入口** — `sync_memory_now`（同步记忆到 Git）与 `memory_sync_status`（查看同步状态）。

面板内的完整图文流程见 [docs/quickstart.md](docs/quickstart.md)。

## Development

The plugin source and its Git repository live at:

```text
N.E.K.O/plugin/plugins/git_memory
```

插件源码及其 Git 仓库直接位于：

```text
N.E.K.O/plugin/plugins/git_memory
```

プラグインのソースと Git リポジトリは次の場所にあります：

```text
N.E.K.O/plugin/plugins/git_memory
```

When publishing to the plugin market, use this GitHub repository name:

发布到插件市场时，请使用以下 GitHub 仓库名：

プラグインマーケットへ公開する際は、次の GitHub リポジトリ名を使用してください：

```text
n.e.k.o_plugin_git_memory
```

From this plugin repository root:

```bash
uvx ruff==0.12.4 check --ignore-noqa --config ruff.toml .
```

From the N.E.K.O repository root / 在 N.E.K.O 仓库根目录中 / N.E.K.O リポジトリのルートで：

```bash
uv run --with pip neko-plugin sync git_memory --clean
uv run neko-plugin check git_memory
uv run neko-plugin check -r git_memory
```

Python runtime dependencies are declared in `pyproject.toml` and synced into
`vendor/` for packaging. The generated `vendor/` directory is not committed;
local builds and CI recreate it before release checks.

Python 运行时依赖声明在 `pyproject.toml` 中，并在打包时同步到 `vendor/`。
生成的 `vendor/` 不提交；本地构建和 CI 会在发布检查前重新生成它。

Python ランタイム依存関係は `pyproject.toml` に宣言し、パッケージ化時に
`vendor/` へ同期します。生成された `vendor/` はコミットせず、ローカルビルドと
CI が公開前チェックで再生成します。

## Market release / Market 发布 / Market 公開

Publish the version declared in `plugin.toml`. By default this pushes the Git
tag, waits for the standard GitHub Release, and notifies the plugin market.

发布 `plugin.toml` 中声明的版本。默认会推送 Git tag、等待标准 GitHub
Release，然后通知插件市场。

`plugin.toml` で宣言されたバージョンを公開します。既定では Git tag を
push し、標準 GitHub Release を待ってからプラグインマーケットへ通知します。

```bash
uv run neko-plugin publish git_memory
```

To run only one half explicitly / 如需仅执行一部分 / 一方のみを実行する場合:

```bash
uv run neko-plugin publish github git_memory
uv run neko-plugin publish market https://github.com/owner/repo/releases/tag/v0.1.0
```

The generated `.github/workflows/release.yml` builds and uploads
`git_memory.neko-plugin`. The market independently verifies that Release
before publishing it.

生成的 `.github/workflows/release.yml` 会构建并上传插件包；Market 会独立验证
该 Release 后再发布。

生成された `.github/workflows/release.yml` がプラグインパッケージをビルドして
アップロードし、Market はその Release を独立検証してから公開します。

## Entry

```toml
entry = "plugin.plugins.git_memory:GitMemoryPlugin"
```
