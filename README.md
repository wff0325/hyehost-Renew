# HYEHOST 主机自动续期

使用 GitHub Actions 自动给 HYEHOST 免费共享主机续期(每 30 天需手动确认一次)。
参考项目架构: https://github.com/wff0325/yincangyun

免费计划续期**免费**：脚本自动过 Cloudflare Turnstile 登录面板，点 "Renew free plan" 即续期 30 天，无需支付。

## 仓库结构

```
app.py                        # 续期主脚本
.github/workflows/renew.yml   # GitHub Actions 定时工作流
time.txt                      # 运行心跳(工作流自动更新，防止仓库闲置暂停)
.env.example                  # 本地运行配置模板
```

## 配置

在仓库 `Settings → Secrets and variables → Actions` 添加 Secrets：

| Secret 名称 | 是否必填 | 说明 |
|---|---|---|
| `EMAIL` | 必填 | hyehost 登录邮箱 |
| `PASSWORD` | 必填 | hyehost 登录密码 |
| `SERVICE_URL` | 可选 | 服务详情页 URL，留空则自动从 `/hosting` 发现 |
| `TG_BOT_TOKEN` | 可选 | Telegram Bot Token |
| `TG_CHAT_ID` | 可选 | Telegram Chat ID |
| `NODE_LINK` | 可选 | 代理节点分享链接(注册/续期时用的独享节点)，支持 `vless://` `vmess://` `trojan://` `tuic://` `anytls://` `hysteria2://` `socks5://`；不配则直连 |

## 使用

1. Fork / 上传本仓库到自己的 GitHub
2. 按上表配置 Secrets(`EMAIL`/`PASSWORD` 必填)
3. Actions 菜单里手动触发 `workflow_dispatch` 测试一次
4. 根据到期日修改 `renew.yml` 里的 cron：服务 17 号到期 → `'0 9 17 * *'`
   (cron 是 UTC 时间，北京时间 = UTC+8；`0 9 17 * *` = 北京时间每月 17 日 17:00)

## 本地运行

```bash
pip install patchright requests
cp .env.example .env   # 填入邮箱密码
python app.py          # .env 已在 .gitignore 中，不会提交
```

## 说明

- 脚本以面板成功提示 "Free hosting renewed for another 30 days." 判定续期成功
- 面板到期日期有**后台同步延迟**(DirectAdmin 侧定期刷新)，续期后立即查看可能仍是旧日期，属正常现象
- 未到续期窗口时面板会拒绝(按钮禁用/提示)，脚本识别为"未到续期时间"并正常退出，不视为失败
- 失败时会在运行目录留下 `login_fail.png` / `renew_error.png` 等截图(Actions 里可下载 artifact/查看日志)
- **不要**把 `.env` 或密码提交到公开仓库；如仓库公开，凭证一律走 Secrets
