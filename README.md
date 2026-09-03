# EkNodes 免费服务器自动续期

> 全自动帮你点面板上的 **Renovar（续期）**，跑在 GitHub Actions 上，免费、无需服务器、无需抓验证码。
> 注册一次、配置一次，以后每 2 天自动帮你把免费服务器续满。

## 它是做什么的

EkNodes 的免费服务器到期前需要在面板手动点「Renovar」才能继续用，忘了点就会被停服。
本项目通过 GitHub Actions **每 2 天自动帮你续期一次**，从此不用再手动登录面板。

> 安全起见：只自动续期 **免费套餐**（价格 = 0）。万一以后你开了付费套餐，脚本会自动跳过，不会误扣你的 coins。

## 特性

- ✅ 定时自动续期（默认每 2 天一次，可自行改）
- ✅ 支持手动触发（Actions 页面点一下立即运行）
- ✅ 可选 Telegram 推送每次结果
- ✅ 可选走你自己的 VLESS 节点出口（机房 IP 被拦时用）
- ✅ 纯 Python 标准库，零依赖，无需服务器 / VPS

## 快速开始（约 2 分钟）

### 1. Fork 这个仓库

点右上角 **Fork**，把本仓库复制到你的账号下。

### 2. 添加两个 Secrets

进入你 Fork 后的仓库：**Settings → Secrets and variables → Actions → New repository secret**，依次添加：

| Secret | 必填 | 说明 |
| --- | --- | --- |
| `EK_EMAIL` | ✅ | 你的 EkNodes 登录邮箱 |
| `EK_PASSWORD` | ✅ | 你的 EkNodes 登录密码 |
| `TG_BOT` | ❌ | Telegram 通知：`chat_id,bot_token`（逗号分隔），不要可不填 |
| `VLESS_NODE` | ❌ | 你自己的 `vless://` 节点链接，用于出口代理（可选） |

> 密码只会保存在 GitHub 的 Secret 里，任何人都看不到，也不会出现在代码或日志中。

### 3. 启用 Actions 并测试

1. 打开你仓库的 **Actions** 页，如果提示要启用，点 **I understand my workflows, go ahead and enable them**。
2. 左侧点 **EkNodes Auto Renew** → 右侧 **Run workflow** → 绿色按钮运行一次。
3. 等十几秒，点进这次运行看日志，出现 `✅ 续期成功` 或 `⏳ 未到续期窗口` 就说明一切正常。

之后就不用管了：**默认每 2 天（UTC 03:00）自动运行一次**，到期前会自动帮你续期。

## 效果说明

日志里每一台服务器会出现三种状态之一：

| 状态 | 含义 |
| --- | --- |
| ✅ 续期成功 | 已把到期时间往后顺延（免费套餐 +7 天） |
| ⏳ 未到续期窗口 | 服务器剩余时间还很多，还没到需要续期的时候（正常） |
| ❌ 失败 | 检查邮箱密码是否填错，或去 Actions 日志看原因 |

## 可选配置

- **修改续期频率**：编辑 `.github/workflows/renew.yml` 里的 `cron` 行（当前 `0 3 */2 * *` = 每 2 天 03:00 UTC）。GitHub 定时任务用的是 UTC 时间。
- **Telegram 通知**：在 [@BotFather](https://t.me/BotFather) 创建机器人拿到 token，再对 [@userinfobot](https://t.me/userinfobot) 发条消息拿到你的数字 ID，填成 `TG_BOT=你的数字ID,你的bot_token`。
- **本地调试**：
  ```bash
  # Windows (PowerShell)
  $env:EK_EMAIL="你的邮箱"; $env:EK_PASSWORD="你的密码"
  python renew.py --dry-run   # 只查看，不改动
  python renew.py             # 正式执行
  ```

## 文件说明

```
├── renew.py                    # 主脚本（纯 Python 标准库）
├── generate_xray_config.py     # 可选：把 VLESS 链接转成 Xray 客户端配置
├── .env.example                # 本地运行时的环境变量示例
└── .github/workflows/renew.yml # GitHub Actions 定时任务
```

## 常见问题

- **Q：多久能到续期窗口？** 免费服每次续期 +7 天，脚本在**剩余不足 48 小时**时才真正续期，避免过度打扰。
- **Q：会不会影响付费服？** 不会，付费服自动跳过。
- **Q：为什么我没有 VLESS_NODE 也能用？** 该变量完全可选，只有你的网络出口被面板拦截时才需要。

## 免责声明

本项目仅供学习与个人自动化使用。请遵守 EkNodes 的服务条款，使用风险自行评估。
