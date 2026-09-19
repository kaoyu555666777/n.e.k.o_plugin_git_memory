import {
  Alert,
  Button,
  Card,
  CodeBlock,
  DataTable,
  Divider,
  Field,
  FileDownload,
  Grid,
  Inline,
  InlineError,
  Input,
  KeyValue,
  Modal,
  Page,
  PasswordInput,
  SegmentedControl,
  Select,
  Stack,
  StatCard,
  StatusBadge,
  Switch,
  Text,
  Textarea,
  Tip,
  Toolbar,
  ToolbarGroup,
  Warning,
  useClipboard,
  useConfirm,
  useEffect,
  useState,
  useToast,
} from "@neko/plugin-ui"
import type { HostedAction, PluginSurfaceProps } from "@neko/plugin-ui"

type InstallLink = { label?: string; url?: string }
type InstallCommand = { label?: string; command?: string }
type InstallInfo = {
  kind?: string
  links?: InstallLink[]
  commands?: InstallCommand[]
  notes?: string[]
}
type PlatformInfo = {
  platform?: string
  label?: string
  detail?: string
  system?: string
  manager?: string
  install?: InstallInfo
}
type GitInfo = {
  available?: boolean
  version?: string
  path?: string
  git_found?: boolean
  gitpython_found?: boolean
  gitpython_version?: string
  error?: string
  error_code?: string
  checked_at?: string
}
type RepoSnapshot = {
  memory_dir?: string
  memory_exists?: boolean
  file_count?: number
  initialized?: boolean
  branch?: string
  dirty?: boolean
  changed_files?: number
  untracked_files?: number
  head_commit?: string
  head_subject?: string
  remote_name?: string
  remote_url?: string
  remote_web_url?: string
}
type ProviderOption = {
  id?: string
  label?: string
  site_url?: string
  token_url?: string
  token_note?: string
  signup_url?: string
  docs_url?: string
  supports_self_hosted?: boolean
}
type RepositoryView = {
  full_name?: string
  name?: string
  private?: boolean
  clone_url?: string
  web_url?: string
  default_branch?: string
  updated_at?: string
  can_push?: boolean
}
type SyncStep = { id?: string; status?: string; detail?: string }
type PendingChoice = {
  code?: string
  branch?: string
  remote?: string
  message?: string
  relation?: { ahead?: number; behind?: number }
  remote_head?: { ref?: string; sha?: string; short?: string; author?: string; subject?: string; committed_at?: string }
  detected_at?: string
  trigger?: string
}
type LastSync = {
  status?: string
  code?: string
  message?: string
  detail?: string
  trigger?: string
  commit?: string
  changed_files?: number
  finished_at?: string
  steps?: SyncStep[]
  relation?: { ahead?: number; behind?: number }
  remote_head?: { sha?: string; short?: string; subject?: string; author?: string }
}
type SettingsView = {
  memory_dir?: string
  remote_name?: string
  branch?: string
  commit_message?: string
  author_name?: string
  author_email?: string
  provider?: string
  repository?: string
  provider_base_url?: string
  auto_sync_enabled?: boolean
  auto_sync_interval_minutes?: number
  pull_before_push?: boolean
  remote_update_policy?: string
  proxy_url?: string
  proxy_mode?: string
  notify_on_error?: boolean
  gitignore_preset?: string
  gitignore_extra?: string
}
type GitMemoryPanelState = {
  ui_token?: string
  error?: string
  plugin?: Record<string, any>
  paths?: { storage_root?: string; memory_dir?: string; memory_dir_custom?: boolean }
  git?: GitInfo
  os?: PlatformInfo
  repo?: RepoSnapshot
  auth?: {
    provider?: string
    token_configured?: boolean
    username?: string
    base_url?: string
    providers?: ProviderOption[]
  }
  sync?: {
    auto_sync_enabled?: boolean
    interval_minutes?: number
    interval_options?: number[]
    remote_update_policy?: string
    proxy?: { mode?: string; url?: string; source?: string }
    running?: boolean
    next_sync_at?: string
    last_sync?: LastSync
    pending_choice?: PendingChoice
  }
  settings?: SettingsView
  gitignore_presets?: string[]
  gitignore_preview?: string
}

const INTERVAL_FALLBACK = [5, 10, 30, 60]
const DEFAULT_REPO_NAME = "neko-memory"

type SettingsDraft = {
  branch: string
  remote_name: string
  commit_message: string
  author_name: string
  author_email: string
  proxy_url: string
  proxy_mode: string
  pull_before_push: boolean
  notify_on_error: boolean
  remote_update_policy: string
  gitignore_preset: string
  gitignore_extra: string
}

function settingsDraft(settings: SettingsView): SettingsDraft {
  return {
    branch: String(settings.branch || "main"),
    remote_name: String(settings.remote_name || "origin"),
    commit_message: String(settings.commit_message || "chore(memory): sync {timestamp}"),
    author_name: String(settings.author_name || ""),
    author_email: String(settings.author_email || ""),
    proxy_url: String(settings.proxy_url || ""),
    proxy_mode: String(settings.proxy_mode || "auto"),
    pull_before_push: settings.pull_before_push !== false,
    notify_on_error: settings.notify_on_error !== false,
    remote_update_policy: String(settings.remote_update_policy || "ask"),
    gitignore_preset: String(settings.gitignore_preset || "default"),
    gitignore_extra: String(settings.gitignore_extra || ""),
  }
}

function hasAction(actions: HostedAction[], id: string): boolean {
  return actions.some((action) => action.id === id || action.entry_id === id)
}

function pickResult(response: any): Record<string, any> {
  if (response && typeof response === "object") {
    const value = (response as Record<string, any>).result
    if (value && typeof value === "object") return value as Record<string, any>
    return response as Record<string, any>
  }
  return {}
}

function describeError(error: unknown): string {
  if (error instanceof Error && error.message) return error.message
  return String(error)
}

function formatTime(value?: string): string {
  const text = String(value || "")
  if (!text) return "—"
  const date = new Date(text)
  if (Number.isNaN(date.getTime())) return text
  return date.toLocaleString()
}

export default function GitMemoryPanel(props: PluginSurfaceProps<GitMemoryPanelState>) {
  const state = (props.state || {}) as GitMemoryPanelState
  const actions = (Array.isArray(props.actions) ? props.actions : []) as HostedAction[]
  const t = (key: string, fallback: string, params?: Record<string, any>) =>
    props.t(key, { ...(params || {}), defaultValue: fallback })

  const paths = state.paths || {}
  const git = state.git || {}
  const os = state.os || {}
  const repo = state.repo || {}
  const auth = state.auth || {}
  const sync = state.sync || {}
  const serverSettings = state.settings || {}
  const providers = Array.isArray(auth.providers) ? auth.providers : []
  const intervalOptions = Array.isArray(sync.interval_options) && sync.interval_options.length > 0
    ? sync.interval_options
    : INTERVAL_FALLBACK
  const presetOptions = Array.isArray(state.gitignore_presets) && state.gitignore_presets.length > 0
    ? state.gitignore_presets
    : ["default", "minimal", "none"]

  const uiToken = String(state.ui_token || "")
  const can = (id: string) => !!uiToken && hasAction(actions, id)
  const providerId = String(auth.provider || serverSettings.provider || "")
  const firstProviderId = String(providers[0]?.id || "")
  const gitAvailable = !!git.available
  const gitChecked = Object.keys(git).length > 0
  const initialized = !!repo.initialized
  const remoteLinked = !!repo.remote_url
  const tokenConfigured = !!auth.token_configured
  const syncRunning = !!sync.running
  const pendingChoice = (sync.pending_choice || {}) as PendingChoice
  const pendingChoiceKey = `${pendingChoice.code || ""}:${pendingChoice.remote_head?.sha || ""}`
  const lastSync = (sync.last_sync || {}) as LastSync
  const proxyInfo = (sync.proxy || {}) as { mode?: string; url?: string; source?: string }
  const syncSteps = Array.isArray(lastSync.steps) ? lastSync.steps : []
  const install = os.install || {}
  const installLinks = Array.isArray(install.links) ? install.links : []
  const installCommands = Array.isArray(install.commands) ? install.commands : []
  const installNotes = Array.isArray(install.notes) ? install.notes : []

  const [busy, setBusy] = useState("")
  const [localError, setLocalError] = useState("")
  const [installOpen, setInstallOpen] = useState(false)
  const [installPrompted, setInstallPrompted] = useState(false)
  const [choiceOpen, setChoiceOpen] = useState(false)
  const [tokenInput, setTokenInput] = useState("")
  const [providerChoice, setProviderChoice] = useState(providerId)
  const [baseUrl, setBaseUrl] = useState(String(auth.base_url || serverSettings.provider_base_url || ""))
  const [repositories, setRepositories] = useState<RepositoryView[]>([])
  const [repositoryQuery, setRepositoryQuery] = useState("")
  const [selectedRepo, setSelectedRepo] = useState(String(serverSettings.repository || ""))
  const [newRepoName, setNewRepoName] = useState(DEFAULT_REPO_NAME)
  const [newRepoDescription, setNewRepoDescription] = useState("")
  const [newRepoPrivate, setNewRepoPrivate] = useState(true)
  const [draft, setDraft] = useState<SettingsDraft>(() => settingsDraft(serverSettings))
  const [draftDirty, setDraftDirty] = useState(false)
  const toast = useToast()
  const clipboard = useClipboard()

  // 下拉框是「当前要操作的平台」的唯一真源。之前这里只认已经保存的
  // provider，第一次打开面板时它还是空的，于是「创建访问令牌 / 注册账号 /
  // 令牌说明文档」三个链接永远不会出现；选了平台也不刷新。
  const selectedProvider = providers.find((item) => item.id === providerChoice) || providers[0]

  useEffect(() => {
    setProviderChoice((current) => current || providerId || firstProviderId)
  }, [providerId, firstProviderId])

  useEffect(() => {
    setChoiceOpen(Boolean(pendingChoiceKey && pendingChoiceKey !== ":"))
  }, [pendingChoiceKey])

  useEffect(() => {
    setBaseUrl((current) => current || String(auth.base_url || serverSettings.provider_base_url || ""))
  }, [auth.base_url, serverSettings.provider_base_url])

  useEffect(() => {
    if (!draftDirty) setDraft(settingsDraft(serverSettings))
  }, [serverSettings, draftDirty])

  useEffect(() => {
    if (gitChecked && !gitAvailable && !installPrompted) {
      setInstallPrompted(true)
      setInstallOpen(true)
    }
  }, [gitChecked, gitAvailable, installPrompted])

  const confirm = useConfirm()

  async function callAction(actionId: string, args: Record<string, any>, timeoutMs: number) {
    const response = await props.api.call(actionId, { ...args, ui_token: uiToken }, { timeoutMs, userInitiated: true })
    return pickResult(response)
  }

  async function runAction(options: {
    actionId: string
    args?: Record<string, any>
    success: string
    timeoutMs?: number
    refresh?: boolean
  }): Promise<Record<string, any> | null> {
    const { actionId, args = {}, success, timeoutMs = 120000, refresh = true } = options
    setBusy(actionId)
    setLocalError("")
    try {
      const payload = await callAction(actionId, args, timeoutMs)
      if (refresh) await props.api.refresh()
      toast.success(String(payload.message || success))
      return payload
    } catch (error) {
      const message = describeError(error)
      setLocalError(message)
      toast.error(message)
      return null
    } finally {
      setBusy("")
    }
  }

  function patchDraft(patch: Partial<SettingsDraft>) {
    setDraftDirty(true)
    setDraft((current) => ({ ...current, ...patch }))
  }

  async function checkEnvironment() {
    await runAction({
      actionId: "ui_check_environment",
      success: t("panel.toast.checked", "已重新检测 Git 环境。"),
      timeoutMs: 60000,
    })
  }

  async function syncNow() {
    await runAction({
      actionId: "sync_memory_now",
      success: t("panel.toast.synced", "记忆目录同步完成。"),
      timeoutMs: 300000,
    })
  }

  async function keepRemoteVersion() {
    const payload = await runAction({
      actionId: "ui_keep_remote_version",
      success: t("panel.toast.keepRemote", "已用远端版本覆盖本地记忆。"),
      timeoutMs: 300000,
    })
    if (payload) setChoiceOpen(false)
  }

  async function keepLocalVersion() {
    const payload = await runAction({
      actionId: "ui_keep_local_version",
      success: t("panel.toast.keepLocal", "已用本地版本覆盖远端仓库。"),
      timeoutMs: 300000,
    })
    if (payload) setChoiceOpen(false)
  }

  async function initializeRepository() {
    const accepted = await confirm({
      title: t("panel.confirm.init.title", "初始化 memory 仓库"),
      message: t("panel.confirm.init.message", "将在记忆目录里执行 git init、写入 .gitignore，并保存提交身份设置，继续吗？"),
      confirmLabel: t("panel.confirm.init.confirm", "初始化"),
      cancelLabel: t("panel.confirm.cancel", "取消"),
    })
    if (!accepted) return
    const payload = await runAction({
      actionId: "ui_init_repository",
      args: {
        branch: draft.branch,
        author_name: draft.author_name,
        author_email: draft.author_email,
        gitignore_preset: draft.gitignore_preset,
      },
      success: t("panel.toast.initialized", "记忆目录已初始化为 Git 仓库。"),
      timeoutMs: 180000,
    })
    if (payload) setDraftDirty(false)
  }

  async function saveToken() {
    const value = tokenInput.trim()
    if (!value) {
      toast.error(t("panel.auth.tokenRequired", "请先填写访问令牌。"))
      return
    }
    const payload = await runAction({
      actionId: "ui_save_token",
      args: { provider: providerChoice, token: value, base_url: baseUrl.trim() },
      success: t("panel.toast.tokenSaved", "访问令牌校验通过，已加密保存。"),
      timeoutMs: 90000,
    })
    if (payload) setTokenInput("")
  }

  async function clearToken() {
    const accepted = await confirm({
      title: t("panel.confirm.token.title", "清除访问令牌"),
      message: t("panel.confirm.token.message", "将删除插件私有目录中保存的访问令牌，已关联的远端不会被修改。"),
      tone: "danger",
      confirmLabel: t("panel.confirm.token.confirm", "清除"),
      cancelLabel: t("panel.confirm.cancel", "取消"),
    })
    if (!accepted) return
    await runAction({
      actionId: "ui_clear_token",
      args: { provider: providerChoice },
      success: t("panel.toast.tokenCleared", "访问令牌已清除。"),
      timeoutMs: 60000,
    })
  }

  async function loadRepositories() {
    setBusy("ui_list_repositories")
    setLocalError("")
    try {
      const payload = await callAction(
        "ui_list_repositories",
        { provider: providerChoice, base_url: baseUrl.trim(), query: repositoryQuery.trim() },
        90000,
      )
      const items = Array.isArray(payload.repositories) ? (payload.repositories as RepositoryView[]) : []
      setRepositories(items)
      toast.success(t("panel.toast.repositories", "已读取 {count} 个仓库。", { count: items.length }))
    } catch (error) {
      const message = describeError(error)
      setLocalError(message)
      toast.error(message)
    } finally {
      setBusy("")
    }
  }

  async function connectRepository() {
    const target = selectedRepo.trim() || repositoryQuery.trim()
    if (!target) {
      toast.error(t("panel.repo.selectFirst", "请先读取并选择、或直接填写要关联的仓库。"))
      return
    }
    const accepted = await confirm({
      title: t("panel.confirm.connect.title", "关联远端仓库"),
      message: t("panel.confirm.connect.message", "将把 {target} 设置为 memory 仓库的远端并立即同步一次，继续吗？", { target }),
      confirmLabel: t("panel.confirm.connect.confirm", "关联并同步"),
      cancelLabel: t("panel.confirm.cancel", "取消"),
    })
    if (!accepted) return
    await runAction({
      actionId: "ui_connect_repository",
      args: { repository: target, branch: draft.branch },
      success: t("panel.toast.connected", "已关联远端仓库并完成一次同步。"),
      timeoutMs: 300000,
    })
  }

  async function createRepository() {
    const name = newRepoName.trim()
    if (!name) {
      toast.error(t("panel.repo.nameRequired", "请先填写仓库名称。"))
      return
    }
    const accepted = await confirm({
      title: t("panel.confirm.create.title", "创建私有仓库"),
      message: t("panel.confirm.create.message", "将在 {provider} 账号下创建私有仓库 {name}，并关联当前记忆目录，继续吗？", {
        provider: selectedProvider?.label || providerChoice,
        name,
      }),
      confirmLabel: t("panel.confirm.create.confirm", "创建并关联"),
      cancelLabel: t("panel.confirm.cancel", "取消"),
    })
    if (!accepted) return
    await runAction({
      actionId: "ui_create_repository",
      args: { name, description: newRepoDescription.trim(), private: newRepoPrivate },
      success: t("panel.toast.created", "私有仓库已创建并关联到记忆目录。"),
      timeoutMs: 300000,
    })
  }

  async function disconnectRepository() {
    const accepted = await confirm({
      title: t("panel.confirm.disconnect.title", "解除远端关联"),
      message: t("panel.confirm.disconnect.message", "将删除记忆仓库上的远端关联，本地文件与提交历史都会保留，继续吗？"),
      tone: "warning",
      confirmLabel: t("panel.confirm.disconnect.confirm", "解除关联"),
      cancelLabel: t("panel.confirm.cancel", "取消"),
    })
    if (!accepted) return
    await runAction({
      actionId: "ui_disconnect_repository",
      success: t("panel.toast.disconnected", "已解除远端关联。"),
      timeoutMs: 60000,
    })
  }

  async function saveSettings() {
    const payload = await runAction({
      actionId: "ui_save_settings",
      args: { settings: { ...draft } },
      success: t("panel.toast.settingsSaved", "Git 设置已保存。"),
      timeoutMs: 60000,
    })
    if (payload) setDraftDirty(false)
  }

  async function setAutoSync(enabled: boolean) {
    await runAction({
      actionId: "ui_set_auto_sync",
      args: { enabled, interval_minutes: sync.interval_minutes || 30 },
      success: enabled
        ? t("panel.toast.autoOn", "已开启自动同步。")
        : t("panel.toast.autoOff", "已关闭自动同步。"),
      timeoutMs: 60000,
    })
  }

  async function setInterval(minutes: number) {
    await runAction({
      actionId: "ui_set_auto_sync",
      args: { interval_minutes: minutes, enabled: true },
      success: t("panel.toast.interval", "自动同步间隔已设置为 {minutes} 分钟。", { minutes }),
      timeoutMs: 60000,
    })
  }

  const busyLabel = (actionId: string, idle: string, working: string) => (busy === actionId ? working : idle)
  const memoryPath = String(repo.memory_dir || paths.memory_dir || "")

  return (
    <Page
      title={t("panel.title", "Git 记忆同步")}
      subtitle={t("panel.subtitle", "把 memory 目录备份并同步到 GitHub / GitLab / Gitee 私有仓库")}
    >
      <Stack>
        {state.error ? (
          <InlineError title={t("panel.errors.state", "读取插件状态失败")} message={String(state.error)} />
        ) : null}
        {localError ? <InlineError title={t("panel.errors.action", "操作失败")} message={localError} /> : null}
        {gitChecked && !gitAvailable ? (
          <Alert tone="danger">
            {t(
              "panel.git.missingAlert",
              "没有检测到可用的 Git，自动同步与手动同步都会被跳过。请先安装 Git，安装完成后点击「重新检测」。",
            )}
          </Alert>
        ) : null}

        <Grid cols={4}>
          <StatCard
            label={t("panel.stats.git", "Git 环境")}
            value={gitAvailable ? t("panel.stats.gitReady", "可用") : t("panel.stats.gitMissing", "未安装")}
          />
          <StatCard
            label={t("panel.stats.repo", "记忆仓库")}
            value={initialized ? t("panel.stats.repoReady", "已初始化") : t("panel.stats.repoPending", "未初始化")}
          />
          <StatCard label={t("panel.stats.changes", "待同步改动")} value={String(repo.changed_files ?? 0)} />
          <StatCard
            label={t("panel.stats.autoSync", "自动同步")}
            value={
              sync.auto_sync_enabled
                ? t("panel.stats.autoSyncOn", "每 {minutes} 分钟", { minutes: sync.interval_minutes || 0 })
                : t("panel.stats.autoSyncOff", "已关闭")
            }
          />
        </Grid>

        <Toolbar>
          <ToolbarGroup>
            <Button
              tone="info"
              disabled={!!busy || !can("ui_check_environment")}
              onClick={checkEnvironment}
            >
              {busyLabel(
                "ui_check_environment",
                t("panel.actions.recheck", "重新检测"),
                t("panel.actions.rechecking", "检测中…"),
              )}
            </Button>
            <Button
              tone="primary"
              disabled={!!busy || syncRunning || !initialized || !remoteLinked || !can("sync_memory_now")}
              onClick={syncNow}
            >
              {syncRunning || busy === "sync_memory_now"
                ? t("panel.actions.syncing", "同步中…")
                : t("panel.actions.syncNow", "立即同步")}
            </Button>
            <Button tone="default" disabled={!!busy} onClick={() => { void props.api.refresh() }}>
              {t("panel.actions.refresh", "刷新状态")}
            </Button>
          </ToolbarGroup>
          <ToolbarGroup>
            <StatusBadge
              tone={syncRunning ? "info" : "default"}
              label={syncRunning ? t("panel.sync.running", "同步进行中") : t("panel.sync.idle", "空闲")}
            />
            <StatusBadge
              tone={tokenConfigured ? "success" : "warning"}
              label={tokenConfigured ? t("panel.auth.tokenReady", "令牌已配置") : t("panel.auth.tokenMissing", "未配置令牌")}
            />
          </ToolbarGroup>
        </Toolbar>

        {initialized && !remoteLinked ? (
          <Warning>
            {t(
              "panel.sync.remoteRequired",
              "还没有关联远端仓库：请先在下方「账号与仓库」里选择或创建一个私有仓库，关联完成后再同步。",
            )}
          </Warning>
        ) : null}

        {pendingChoice.code ? (
          <Warning>
            <Stack>
              <Text>{t("panel.sync.pending.title", "远端仓库有更新的版本")}</Text>
              <Text>
                {t(
                  "panel.sync.pending.summary",
                  "远端领先 {behind} 个提交，本地领先 {ahead} 个提交。请选择保留哪一版：保留远端会覆盖本地记忆，保留本地会覆盖远端仓库。",
                  {
                    behind: Number(pendingChoice.relation?.behind || 0),
                    ahead: Number(pendingChoice.relation?.ahead || 0),
                  },
                )}
              </Text>
              {pendingChoice.remote_head?.short ? (
                <Text>
                  {t("panel.sync.pending.remoteHeadSummary", "远端最新提交：{short} {subject}", {
                    short: pendingChoice.remote_head.short,
                    subject: pendingChoice.remote_head.subject || "",
                  })}
                </Text>
              ) : null}
              <Inline>
                <Button tone="warning" disabled={!!busy || !can("ui_keep_remote_version")} onClick={keepRemoteVersion}>
                  {busyLabel(
                    "ui_keep_remote_version",
                    t("panel.sync.pending.keepRemote", "保留远端版本（覆盖本地）"),
                    t("panel.actions.syncing", "同步中…"),
                  )}
                </Button>
                <Button tone="warning" disabled={!!busy || !can("ui_keep_local_version")} onClick={keepLocalVersion}>
                  {busyLabel(
                    "ui_keep_local_version",
                    t("panel.sync.pending.keepLocal", "保留本地版本（覆盖远端）"),
                    t("panel.actions.syncing", "同步中…"),
                  )}
                </Button>
              </Inline>
            </Stack>
          </Warning>
        ) : null}

        <Card title={t("panel.git.title", "Git 环境")}>
          <Stack>
            <KeyValue
              items={[
                {
                  label: t("panel.git.status", "状态"),
                  value: (
                    <StatusBadge
                      tone={gitAvailable ? "success" : "danger"}
                      label={gitAvailable ? t("panel.git.available", "可用") : t("panel.git.unavailable", "未检测到 Git")}
                    />
                  ),
                },
                { label: t("panel.git.version", "Git 版本"), value: git.version || "—" },
                { label: t("panel.git.path", "Git 路径"), value: git.path || "—" },
                {
                  label: t("panel.git.gitpython", "GitPython"),
                  value: git.gitpython_found
                    ? git.gitpython_version || t("panel.git.installed", "已安装")
                    : t("panel.git.notInstalled", "未安装"),
                },
                { label: t("panel.git.os", "操作系统"), value: os.label || os.platform || "—" },
                { label: t("panel.git.osDetail", "系统信息"), value: os.detail || os.manager || "—" },
                { label: t("panel.git.checkedAt", "检测时间"), value: formatTime(git.checked_at) },
              ]}
            />
            {git.error ? <Text>{git.error}</Text> : null}
            {!gitAvailable && install.links && install.links.length > 0 ? (
              <Text>
                {t("panel.git.installHint", "按当前系统推荐的安装方式：{command}", {
                  command: installCommands[0]?.command || installLinks[0]?.url || "",
                })}
              </Text>
            ) : null}
            {!gitAvailable ? (
              <Inline>
                <Button
                  tone="warning"
                  onClick={() => {
                    setInstallOpen(true)
                  }}
                >
                  {t("panel.git.howToInstall", "查看 Git 安装方法")}
                </Button>
              </Inline>
            ) : null}
          </Stack>
        </Card>

        <Card title={t("panel.repo.title", "记忆仓库")}>
          <Stack>
            <KeyValue
              items={[
                { label: t("panel.repo.dir", "记忆目录"), value: memoryPath || "—" },
                { label: t("panel.repo.files", "文件数量"), value: String(repo.file_count ?? 0) },
                { label: t("panel.repo.branch", "当前分支"), value: repo.branch || draft.branch || "—" },
                {
                  label: t("panel.repo.remote", "远端"),
                  value: repo.remote_url
                    ? `${repo.remote_name || "origin"} · ${repo.remote_url}`
                    : t("panel.repo.noRemote", "尚未关联远端仓库"),
                },
                {
                  label: t("panel.repo.changes", "改动 / 未跟踪"),
                  value: `${repo.changed_files ?? 0} / ${repo.untracked_files ?? 0}`,
                },
                {
                  label: t("panel.repo.head", "最近提交"),
                  value: repo.head_commit
                    ? `${repo.head_commit} · ${repo.head_subject || ""}`
                    : t("panel.repo.noCommit", "暂无提交"),
                },
              ]}
            />
            {!initialized ? (
              <Alert tone="warning">
                {t("panel.repo.notInitialized", "memory 目录还不是 Git 仓库，请先点击「初始化仓库」。")}
              </Alert>
            ) : null}
            <Inline>
              <Button
                tone="primary"
                disabled={!!busy || initialized || !can("ui_init_repository")}
                onClick={initializeRepository}
              >
                {busyLabel(
                  "ui_init_repository",
                  t("panel.actions.init", "初始化仓库"),
                  t("panel.actions.initializing", "初始化中…"),
                )}
              </Button>
              <Button tone="success" disabled={!!busy || !initialized} onClick={syncNow}>
                {t("panel.actions.syncNow", "立即同步")}
              </Button>
              {repo.remote_web_url ? (
                <FileDownload
                  url={String(repo.remote_web_url)}
                  label={t("panel.repo.openRemote", "打开远端仓库")}
                  tone="info"
                />
              ) : null}
              {memoryPath ? (
                <FileDownload path={memoryPath} label={t("panel.repo.openFolder", "打开记忆目录")} tone="default" />
              ) : null}
              {initialized ? (
                <Button
                  tone="warning"
                  disabled={!!busy || !can("ui_disconnect_repository")}
                  onClick={disconnectRepository}
                >
                  {t("panel.actions.disconnect", "解除远端关联")}
                </Button>
              ) : null}
            </Inline>
          </Stack>
        </Card>

        <Card title={t("panel.sync.title", "同步设置")}>
          <Stack>
            <Inline align="center" justify="space-between">
              <Switch
                checked={!!sync.auto_sync_enabled}
                label={t("panel.sync.autoLabel", "自动同步")}
                disabled={!!busy || !can("ui_set_auto_sync")}
                onChange={setAutoSync}
              />
              <StatusBadge
                tone={sync.auto_sync_enabled ? "success" : "default"}
                label={
                  sync.auto_sync_enabled
                    ? t("panel.sync.autoOn", "已开启")
                    : t("panel.sync.autoOff", "已关闭")
                }
              />
            </Inline>
            <Field
              label={t("panel.sync.interval", "同步间隔")}
              help={t("panel.sync.intervalHelp", "开启后插件会按该间隔自动提交并推送记忆改动。")}
            >
              <SegmentedControl
                value={sync.interval_minutes || 30}
                options={intervalOptions.map((minutes) => ({
                  value: minutes,
                  label: `${minutes} ${t("panel.sync.minutes", "分钟")}`,
                }))}
                disabled={!!busy || !can("ui_set_auto_sync")}
                onChange={(value: any) => setInterval(Number(value) || 30)}
              />
            </Field>
            <KeyValue
              items={[
                { label: t("panel.sync.last", "上次同步"), value: formatTime(lastSync.finished_at) },
                { label: t("panel.sync.next", "下次自动同步"), value: formatTime(sync.next_sync_at) },
                {
                  label: t("panel.sync.trigger", "触发方式"),
                  value: lastSync.trigger === "auto"
                    ? t("panel.sync.triggerAuto", "自动同步")
                    : lastSync.trigger
                      ? t("panel.sync.triggerManual", "手动同步")
                      : "—",
                },
                { label: t("panel.sync.result", "结果"), value: lastSync.message || "—" },
                { label: t("panel.sync.commit", "提交"), value: lastSync.commit || "—" },
                {
                  label: t("panel.sync.proxy", "当前代理"),
                  value: proxyInfo.url
                    ? proxyInfo.url
                    : t("panel.sync.proxyNone", "未使用代理（直连）"),
                },
              ]}
            />
            {lastSync.code ? (
              <Alert tone="danger">{`${lastSync.message || ""}（${lastSync.code}）`}</Alert>
            ) : null}
            {lastSync.detail ? (
              <Field
                label={t("panel.sync.detail", "Git 输出")}
                help={t(
                  "panel.sync.detailHelp",
                  "上一次同步失败时 git 的原始输出（已隐去令牌），可据此判断是 DNS、证书、代理还是权限问题。",
                )}
              >
                <CodeBlock>{String(lastSync.detail)}</CodeBlock>
              </Field>
            ) : null}
            {syncSteps.length > 0 ? (
              <DataTable
                data={syncSteps}
                rowKey="id"
                columns={[
                  { key: "id", label: t("panel.sync.colStep", "步骤") },
                  { key: "status", label: t("panel.sync.colStatus", "状态") },
                  { key: "detail", label: t("panel.sync.colDetail", "说明") },
                ]}
              />
            ) : null}
            <Inline>
              <Button tone="primary" disabled={!!busy || !initialized || syncRunning} onClick={syncNow}>
                {t("panel.actions.syncNow", "立即同步")}
              </Button>
            </Inline>
            <Tip>
              {t(
                "panel.sync.tip",
                "自动同步在插件运行期间每分钟检查一次是否到点；手动同步与自动同步共用同一把锁，同一时刻只会执行一个同步任务。",
              )}
            </Tip>
          </Stack>
        </Card>

        <Card title={t("panel.auth.title", "账号与远端仓库")}>
          <Stack>
            <Alert tone="info">
              {t(
                "panel.auth.notice",
                "访问令牌只加密保存在插件私有数据目录中，不会写入 memory 目录，也不会出现在日志里。",
              )}
            </Alert>
            <Grid cols={2}>
              <Field
                label={t("panel.auth.provider", "代码托管平台")}
                help={t("panel.auth.providerHelp", "选择平台后按提示创建访问令牌。")}
              >
                <Select
                  value={providerChoice}
                  options={providers.map((item) => ({
                    value: item.id || "",
                    label: item.label || item.id || "",
                  }))}
                  disabled={!!busy}
                  onChange={(value: any) => setProviderChoice(String(value))}
                />
              </Field>
              <Field
                label={t("panel.auth.account", "账号")}
                help={
                  tokenConfigured
                    ? t("panel.auth.accountReady", "令牌已保存，可以直接读取仓库列表。")
                    : t("panel.auth.accountPending", "尚未保存可用的令牌。")
                }
              >
                <Input
                  value={String(auth.username || "")}
                  placeholder={t("panel.auth.accountPlaceholder", "校验令牌后自动填入")}
                  disabled
                  onChange={() => {}}
                />
              </Field>
            </Grid>
            <Inline>
              {selectedProvider?.token_url ? (
                <FileDownload
                  url={selectedProvider.token_url}
                  label={t("panel.auth.createToken", "创建访问令牌")}
                  tone="primary"
                />
              ) : null}
              {selectedProvider?.signup_url ? (
                <FileDownload
                  url={selectedProvider.signup_url}
                  label={t("panel.auth.signup", "注册账号")}
                  tone="info"
                />
              ) : null}
              {selectedProvider?.docs_url && selectedProvider.docs_url !== selectedProvider.token_url ? (
                <FileDownload
                  url={selectedProvider.docs_url}
                  label={t("panel.auth.docs", "令牌说明文档")}
                  tone="default"
                />
              ) : null}
            </Inline>
            {selectedProvider?.token_note ? <Text>{selectedProvider.token_note}</Text> : null}
            <Field label={t("panel.auth.token", "访问令牌")} help={t("panel.auth.tokenHelp", "令牌只在保存时用于校验账号，之后用于读取仓库列表与推送。")}>
              <PasswordInput
                value={tokenInput}
                placeholder={
                  tokenConfigured
                    ? t("panel.auth.tokenPlaceholderSet", "输入新令牌以替换已保存的令牌")
                    : t("panel.auth.tokenPlaceholder", "粘贴访问令牌")
                }
                disabled={!!busy}
                onChange={setTokenInput}
              />
            </Field>
            {selectedProvider?.supports_self_hosted ? (
              <Field
                label={t("panel.auth.baseUrl", "自建站点地址")}
                help={t("panel.auth.baseUrlHelp", "使用官方站点时留空；自建 GitLab / Gitee 时填写站点根地址。")}
              >
                <Input
                  value={baseUrl}
                  placeholder="https://gitlab.example.com"
                  disabled={!!busy}
                  onChange={setBaseUrl}
                />
              </Field>
            ) : null}
            <Inline>
              <Button
                tone="primary"
                disabled={!!busy || !tokenInput.trim() || !can("ui_save_token")}
                onClick={saveToken}
              >
                {busyLabel(
                  "ui_save_token",
                  t("panel.actions.saveToken", "校验并保存令牌"),
                  t("panel.actions.savingToken", "校验中…"),
                )}
              </Button>
              <Button
                tone="danger"
                disabled={!!busy || !tokenConfigured || !can("ui_clear_token")}
                onClick={clearToken}
              >
                {t("panel.actions.clearToken", "清除令牌")}
              </Button>
            </Inline>
            <Divider />

            <Field
              label={t("panel.repo.query", "搜索仓库")}
              help={t("panel.repo.queryHelp", "可以留空，直接读取最近更新的仓库列表。")}
            >
              <Input
                value={repositoryQuery}
                placeholder={t("panel.repo.queryPlaceholder", "owner/repo，或留空")}
                disabled={!!busy}
                onChange={setRepositoryQuery}
              />
            </Field>
            <Inline>
              <Button
                tone="info"
                disabled={!!busy || !tokenConfigured || !can("ui_list_repositories")}
                onClick={loadRepositories}
              >
                {busyLabel(
                  "ui_list_repositories",
                  t("panel.actions.listRepos", "读取仓库列表"),
                  t("panel.actions.listingRepos", "读取中…"),
                )}
              </Button>
              <Button
                tone="success"
                disabled={!!busy || !initialized || !tokenConfigured || !can("ui_connect_repository")}
                onClick={connectRepository}
              >
                {t("panel.actions.connect", "关联选中仓库并同步")}
              </Button>
              <Text>
                {t("panel.repo.selected", "当前选择：{repo}", {
                  repo: selectedRepo || t("panel.repo.none", "未选择"),
                })}
              </Text>
            </Inline>
            <DataTable
              data={repositories}
              rowKey="full_name"
              selectedKey={selectedRepo}
              emptyText={t("panel.repo.emptyList", "还没有读取仓库列表。")}
              onSelect={(row: RepositoryView) => setSelectedRepo(String(row.full_name || ""))}
              columns={[
                { key: "full_name", label: t("panel.repo.colName", "仓库") },
                {
                  key: "private",
                  label: t("panel.repo.colPrivate", "私有"),
                  render: (row) => (row.private ? t("panel.repo.yes", "是") : t("panel.repo.no", "否")),
                },
                {
                  key: "can_push",
                  label: t("panel.repo.colPush", "可推送"),
                  render: (row) => (row.can_push === false ? t("panel.repo.no", "否") : t("panel.repo.yes", "是")),
                },
                { key: "default_branch", label: t("panel.repo.colBranch", "默认分支") },
                {
                  key: "updated_at",
                  label: t("panel.repo.colUpdated", "更新时间"),
                  render: (row) => formatTime(row.updated_at),
                },
              ]}
            />
            <Divider />
            <Field
              label={t("panel.repo.newName", "新仓库名称")}
              help={t("panel.repo.newNameHelp", "在当前账号下创建同名私有仓库，并自动关联为远端。")}
            >
              <Input value={newRepoName} placeholder={DEFAULT_REPO_NAME} disabled={!!busy} onChange={setNewRepoName} />
            </Field>
            <Field
              label={t("panel.repo.newDescription", "仓库描述")}
              help={t("panel.repo.newDescriptionHelp", "可选，仅用于远端仓库说明。")}
            >
              <Input
                value={newRepoDescription}
                placeholder={t("panel.repo.newDescriptionPlaceholder", "例如：N.E.K.O 记忆目录备份")}
                disabled={!!busy}
                onChange={setNewRepoDescription}
              />
            </Field>
            <Switch
              checked={newRepoPrivate}
              label={t("panel.repo.private", "创建为私有仓库（推荐）")}
              disabled={!!busy}
              onChange={setNewRepoPrivate}
            />
            <Inline>
              <Button
                tone="success"
                disabled={!!busy || !initialized || !tokenConfigured || !can("ui_create_repository")}
                onClick={createRepository}
              >
                {busyLabel(
                  "ui_create_repository",
                  t("panel.actions.createRepo", "创建私有仓库并关联"),
                  t("panel.actions.creatingRepo", "创建中…"),
                )}
              </Button>
            </Inline>
            <Warning>
              {t("panel.repo.warning", "创建仓库需要令牌具备建仓权限；默认创建私有仓库，记忆内容不会被公开。")}
            </Warning>
          </Stack>
        </Card>

        <Card title={t("panel.settings.title", "Git 设置")}>
          <Stack>
            <Grid cols={2}>
              <Field label={t("panel.settings.branch", "分支")} help={t("panel.settings.branchHelp", "同步使用的分支名，建议使用 main。")}>
                <Input
                  value={draft.branch}
                  disabled={!!busy}
                  onChange={(value: string) => patchDraft({ branch: value })}
                />
              </Field>
              <Field label={t("panel.settings.remoteName", "远端名称")} help={t("panel.settings.remoteNameHelp", "默认 origin。")}>
                <Input
                  value={draft.remote_name}
                  disabled={!!busy}
                  onChange={(value: string) => patchDraft({ remote_name: value })}
                />
              </Field>
              <Field label={t("panel.settings.authorName", "提交用户名")} help={t("panel.settings.authorNameHelp", "留空时使用 Git 全局配置。")}>
                <Input
                  value={draft.author_name}
                  disabled={!!busy}
                  onChange={(value: string) => patchDraft({ author_name: value })}
                />
              </Field>
              <Field label={t("panel.settings.authorEmail", "提交邮箱")} help={t("panel.settings.authorEmailHelp", "留空时使用 Git 全局配置。")}>
                <Input
                  value={draft.author_email}
                  disabled={!!busy}
                  onChange={(value: string) => patchDraft({ author_email: value })}
                />
              </Field>
            </Grid>
            <Field
              label={t("panel.settings.commitMessage", "提交信息模板")}
              help={t("panel.settings.commitMessageHelp", "支持 {timestamp} / {date} / {time} / {hostname} / {count} 占位符。")}
            >
              <Input
                value={draft.commit_message}
                disabled={!!busy}
                onChange={(value: string) => patchDraft({ commit_message: value })}
              />
            </Field>
            <Grid cols={2}>
              <Field
                label={t("panel.settings.remotePolicy", "远端有新版本时")}
                help={t("panel.settings.remotePolicyHelp", "同步前发现远端有本地没有的提交时怎么办：默认每次询问你，也可以固定保留某一侧。")}
              >
                <Select
                  value={draft.remote_update_policy}
                  options={[
                    { value: "ask", label: t("panel.settings.policyAsk", "每次询问我") },
                    { value: "keep_local", label: t("panel.settings.policyKeepLocal", "总是保留本地版本") },
                    { value: "keep_remote", label: t("panel.settings.policyKeepRemote", "总是保留远端版本") },
                  ]}
                  disabled={!!busy}
                  onChange={(value: any) => patchDraft({ remote_update_policy: String(value) })}
                />
              </Field>
              <Field
                label={t("panel.settings.proxy", "网络代理")}
                help={t("panel.settings.proxyHelp", "手动模式下使用这个地址，例如 http://127.0.0.1:7890。")}
              >
                <Input
                  value={draft.proxy_url}
                  placeholder="http://127.0.0.1:7890"
                  disabled={!!busy}
                  onChange={(value: string) => patchDraft({ proxy_url: value })}
                />
              </Field>
            </Grid>
            <Field
              label={t("panel.settings.proxyMode", "代理模式")}
              help={t(
                "panel.settings.proxyModeHelp",
                "git 不读取 Windows/macOS 的系统代理设置，所以这里默认「自动」：先用手动地址，其次应用环境变量，其次系统代理。开关过 N.E.K.O 的直连模式时会自动直连。",
              )}
            >
              <SegmentedControl
                value={draft.proxy_mode}
                options={[
                  { value: "auto", label: t("panel.settings.proxyAuto", "自动（推荐）") },
                  { value: "manual", label: t("panel.settings.proxyManual", "只用手动地址") },
                  { value: "off", label: t("panel.settings.proxyOff", "直连") },
                ]}
                disabled={!!busy}
                onChange={(value: any) => patchDraft({ proxy_mode: String(value) })}
              />
            </Field>
            <Inline align="center" justify="space-between">
              <Switch
                checked={draft.pull_before_push}
                label={t("panel.settings.pull", "推送前先拉取远端")}
                disabled={!!busy}
                onChange={(value: boolean) => patchDraft({ pull_before_push: value })}
              />
              <Switch
                checked={draft.notify_on_error}
                label={t("panel.settings.notify", "自动同步失败时提醒我")}
                disabled={!!busy}
                onChange={(value: boolean) => patchDraft({ notify_on_error: value })}
              />
            </Inline>
            <Field
              label={t("panel.settings.gitignorePreset", ".gitignore 预设")}
              help={t("panel.settings.gitignorePresetHelp", "初始化仓库时写入记忆目录的忽略规则。")}
            >
              <SegmentedControl
                value={draft.gitignore_preset}
                options={presetOptions.map((preset) => ({
                  value: preset,
                  label: t(`panel.settings.preset.${preset}`, preset),
                }))}
                disabled={!!busy}
                onChange={(value: any) => patchDraft({ gitignore_preset: String(value) })}
              />
            </Field>
            <Field
              label={t("panel.settings.gitignoreExtra", "追加忽略规则")}
              help={t("panel.settings.gitignoreExtraHelp", "每行一条规则，保存后在下一次同步时生效。")}
            >
              <Textarea
                value={draft.gitignore_extra}
                placeholder={"*.log\n.env"}
                disabled={!!busy}
                onChange={(value: string) => patchDraft({ gitignore_extra: value })}
              />
            </Field>
            <Inline>
              <Button tone="primary" disabled={!!busy || !can("ui_save_settings")} onClick={saveSettings}>
                {busyLabel(
                  "ui_save_settings",
                  t("panel.actions.saveSettings", "保存 Git 设置"),
                  t("panel.actions.savingSettings", "保存中…"),
                )}
              </Button>
              <Button
                tone="default"
                disabled={!!busy || !draftDirty}
                onClick={() => {
                  setDraft(settingsDraft(serverSettings))
                  setDraftDirty(false)
                }}
              >
                {t("panel.actions.resetSettings", "放弃修改")}
              </Button>
            </Inline>
            {draftDirty ? <Alert tone="warning">{t("panel.settings.unsaved", "有尚未保存的修改。")}</Alert> : null}
            <Field
              label={t("panel.settings.gitignorePreview", ".gitignore 预览")}
              help={t("panel.settings.gitignorePreviewHelp", "这里是已保存设置生成的忽略规则。")}
            >
              <CodeBlock>{String(state.gitignore_preview || "")}</CodeBlock>
            </Field>
          </Stack>
        </Card>

        <Modal
          open={choiceOpen && !!pendingChoice.code}
          size="lg"
          title={t("panel.sync.pending.title", "远端仓库有更新的版本")}
          onClose={() => {
            setChoiceOpen(false)
          }}
          footer={
            <Inline justify="end">
              <Button
                tone="default"
                disabled={!!busy}
                onClick={() => {
                  setChoiceOpen(false)
                }}
              >
                {t("panel.sync.pending.later", "稍后决定")}
              </Button>
              <Button
                tone="warning"
                disabled={!!busy || !can("ui_keep_local_version")}
                onClick={keepLocalVersion}
              >
                {busyLabel(
                  "ui_keep_local_version",
                  t("panel.sync.pending.keepLocal", "保留本地版本（覆盖远端）"),
                  t("panel.actions.syncing", "同步中…"),
                )}
              </Button>
              <Button
                tone="primary"
                disabled={!!busy || !can("ui_keep_remote_version")}
                onClick={keepRemoteVersion}
              >
                {busyLabel(
                  "ui_keep_remote_version",
                  t("panel.sync.pending.keepRemote", "保留远端版本（覆盖本地）"),
                  t("panel.actions.syncing", "同步中…"),
                )}
              </Button>
            </Inline>
          }
        >
          <Stack>
            <Text>
              {t(
                "panel.sync.pending.detail",
                "同步时发现远端 {branch} 分支上有 {behind} 个本地没有的提交（本地领先 {ahead} 个）。请选择保留哪一版：",
                {
                  branch: pendingChoice.branch || draft.branch,
                  behind: Number(pendingChoice.relation?.behind || 0),
                  ahead: Number(pendingChoice.relation?.ahead || 0),
                },
              )}
            </Text>
            <KeyValue
              items={[
                {
                  label: t("panel.sync.pending.remoteHead", "远端最新提交"),
                  value: pendingChoice.remote_head?.short
                    ? `${pendingChoice.remote_head.short} ${pendingChoice.remote_head.subject || ""}`
                    : "—",
                },
                {
                  label: t("panel.sync.pending.remoteAuthor", "提交者"),
                  value: pendingChoice.remote_head?.author || "—",
                },
                {
                  label: t("panel.sync.pending.detectedAt", "检测时间"),
                  value: formatTime(pendingChoice.detected_at),
                },
              ]}
            />
            <Warning>
              {t(
                "panel.sync.pending.keepRemoteNote",
                "保留远端版本会用远端内容覆盖本地 memory 目录，本地尚未推送的提交会被丢弃。",
              )}
            </Warning>
            <Warning>
              {t(
                "panel.sync.pending.keepLocalNote",
                "保留本地版本会把本地提交强制推送到远端，远端上别人推送的提交会被覆盖。",
              )}
            </Warning>
          </Stack>
        </Modal>

        <Modal
          open={installOpen}
          size="lg"
          title={t("panel.install.title", "没有检测到 Git")}
          onClose={() => setInstallOpen(false)}
          footer={
            <Inline justify="end">
              <Button
                tone="default"
                onClick={() => {
                  setInstallOpen(false)
                }}
              >
                {t("panel.install.close", "关闭")}
              </Button>
              <Button tone="info" disabled={!!busy || !can("ui_check_environment")} onClick={checkEnvironment}>
                {t("panel.actions.recheck", "重新检测")}
              </Button>
            </Inline>
          }
        >
          <Stack>
            <Text>
              {t(
                "panel.install.intro",
                "Git 记忆同步依赖本机的 Git 命令行。请先按下面的方式安装 Git，安装完成后回到这里点击「重新检测」。",
              )}
            </Text>
            <KeyValue
              items={[
                { label: t("panel.install.platform", "当前系统"), value: os.label || os.platform || "—" },
                { label: t("panel.install.detail", "系统信息"), value: os.detail || "—" },
                {
                  label: t("panel.install.manager", "包管理器"),
                  value: os.manager || t("panel.install.managerNone", "未识别发行版"),
                },
              ]}
            />
            {installLinks.length > 0 ? (
              <Stack>
                <Text>{t("panel.install.linksTitle", "下载地址 / 官方说明")}</Text>
                <Inline>
                  {installLinks.map((link) => (
                    <FileDownload
                      key={String(link.url || link.label)}
                      url={String(link.url || "")}
                      label={link.label || String(link.url || "")}
                      tone="primary"
                    />
                  ))}
                </Inline>
              </Stack>
            ) : null}
            {installCommands.length > 0 ? (
              <Stack>
                <Text>{t("panel.install.commandsTitle", "安装命令")}</Text>
                <DataTable
                  data={installCommands}
                  rowKey="command"
                  columns={[
                    { key: "label", label: t("panel.install.colPurpose", "适用环境") },
                    { key: "command", label: t("panel.install.colCommand", "命令") },
                    {
                      key: "copy",
                      label: "",
                      render: (row) => (
                        <Button
                          tone="info"
                          onClick={() => {
                            void clipboard.write(String(row.command || ""))
                          }}
                        >
                          {t("panel.install.copy", "复制")}
                        </Button>
                      ),
                    },
                  ]}
                />
              </Stack>
            ) : null}
            {installNotes.length > 0 ? (
              <Stack>
                {installNotes.map((note) => (
                  <Text key={note}>{`• ${note}`}</Text>
                ))}
              </Stack>
            ) : null}
            <Alert tone="info">
              {t(
                "panel.install.afterInstall",
                "安装或更新 Git 之后请重启 N.E.K.O（让新的 PATH 生效），再回到面板重新检测。",
              )}
            </Alert>
          </Stack>
        </Modal>
      </Stack>
    </Page>
  )
}
