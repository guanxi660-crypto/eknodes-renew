# EkNodes 自动续期（GitHub Actions）

自动续期 [EkNodes](https://dash.eknodes.es) 的免费服务器（点击面板「Renovar」的效果），
使用 GitHub Actions 定时任务运行，**无需服务器、无需 VPS、无需抓验证码**。

## 工作原理

面板后端基于 **Supabase**。点「Renovar」最终做的事只有两步：

1. Cloudflare Turnstile 人机验证；
2. 把 `servers.expires_at` 更新为续期周期之后的时间。

免费计划（Plan Free / weekly）续期 = **`expires_at = now + 7 天`**。

本脚本直接调用 Supabase 公开 API（GoTrue 邮箱登录 + PostgREST 更新 `expires_at`），
与点按钮结果一致，且绕开 Turnstile，稳定性远超浏览器方案。

> 仅自动续期**免费计划**（`price_monthly == 0`）；付费计划会被跳过，避免误扣 coins。

## 文件说明

```
eknodes-renew/
├── renew.py                    # 主脚本（纯 Python 标准库，零依赖）
├── .github/workflows/renew.yml # GitHub Actions 定时任务
├── .env.example                # 环境变量示例
└── README.md
```

## 本地运行

```bash
# 直接设环境变量
set EK_EMAIL=你的邮箱
set EK_PASSWORD=你的密码

# 先 dry-run（不写库）
python renew.py --dry-run

# 正式运行（距到期 < 48h 才续期）
python renew.py
```

可选环境变量：

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `EK_EMAIL` | EkNodes 登录邮箱 | 必填 |
| `EK_PASSWORD` | EkNodes 登录密码 | 必填 |
| `EK_RENEW_THRESHOLD_HOURS` | 距到期不足多少小时才触发续期 | `48` |
| `EK_SUPABASE_URL` | Supabase 项目地址 | 已内置默认值 |
| `EK_SUPABASE_ANON` | Supabase anon key（公开） | 已内置默认值 |
| `TG_BOT` | Telegram 通知：`chat_id,bot_token`，逗号分隔 | 不通知 |
| `EK_DRY_RUN` | 设为 `1` 只模拟不写库 | 关闭 |

`--force` 参数可无视阈值强制续期（调试用）。

## 部署到 GitHub Actions

1. 新建仓库并把本目录推送上去：

```bash
git init
git add .
git commit -m "init"
git branch -M main
git remote add origin git@github.com:你的用户名/eknodes-renew.git
git push -u origin main
```

2. 到仓库 **Settings → Secrets and variables → Actions** 添加以下仓库 Secret：

| Secret | 值 |
| --- | --- |
| `EK_EMAIL` | 登录邮箱 |
| `EK_PASSWORD` | 登录密码 |
| `EK_RENEW_THRESHOLD_HOURS` | （可选）阈值小时数，默认 48 |
| `TG_BOT` | （可选）`chat_id,bot_token` |

3. Actions 里已配置：
   - `schedule`：每 6 小时跑一次（`cron: "17 */6 * * *"`，UTC）；
   - `workflow_dispatch`：可随时手动触发。

## 注意事项

- 密码只保存在 GitHub Secret 中，代码里不出现密码。
- 免费服长期不续期会被暂停，建议阈值留足余量（默认剩余 <48h 续期，每 6h 跑一次，足够稳妥）。
- 若账号将来开通付费套餐，脚本会自动跳过，不会误扣 coins。
