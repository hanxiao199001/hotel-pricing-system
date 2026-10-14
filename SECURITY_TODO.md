# 安全整改清单（SECURITY TODO）

> 生成日期：2026-08-28。本次提交已从**当前文件**中删除所有敏感信息，
> 但 **git 历史中仍有残留**，必须按下文步骤清理历史并强推。

## 一、曾泄露的敏感信息（已从当前文件删除，凭据本身不在此复述）

| 文件 | 位置（整改前版本） | 泄露内容类型 |
|------|-------------------|--------------|
| `README.md` | "配置说明" 代码块（约第 124-129 行） | 携程商家账号用户名、明文密码、酒店ID |
| `README.md` | 文末"部署位置"一行（约第 185 行） | 生产服务器公网 IP 及部署路径 |
| `config.py` | `CTRIP_CONFIG`（第 7-9 行） | 携程商家账号用户名、明文密码、酒店ID |
| `config.py` | `WECHAT_CONFIG`（第 35 行） | 个人手机号（WhatsApp 通知接收人） |
| `notifier.py` | `WhatsAppNotifier.__init__` 默认参数（第 11 行） | 个人手机号 |

未发现 API key / token 类凭据。上述内容现已改为从环境变量读取（见 `.env.example`）。

## 二、必须立即执行（今天）

1. **立即修改携程商家账号密码**（该密码已随仓库泄露，视为已失窃）。
   如同一密码在其他平台复用，一并修改。
2. 检查携程后台登录日志，确认无异常登录。
3. 评估是否更换服务器公网 IP 或至少收紧防火墙/安全组（IP 已暴露）。

## 三、清理 git 历史（明天执行，不要跳过）

历史提交中仍包含上述凭据。使用 git-filter-repo 清理：

```bash
# 1. 安装工具
pip install git-filter-repo

# 2. 先备份仓库
git clone --mirror <仓库地址> hotel-pricing-backup.git

# 3. 在一个全新克隆中操作（filter-repo 要求 fresh clone）
git clone <仓库地址> hotel-pricing-clean
cd hotel-pricing-clean

# 4. 写一个替换规则文件 replacements.txt（内容为旧凭据，格式如下，
#    左边填真实泄露值，此处用占位符表示）：
#    <旧携程密码>==>REMOVED
#    <旧携程用户名>==>REMOVED
#    <服务器IP>==>REMOVED
#    <手机号>==>REMOVED
git filter-repo --replace-text replacements.txt

# 5. 重新添加远程并强制推送所有分支和标签
git remote add origin <仓库地址>
git push origin --force --all
git push origin --force --tags

# 6. 通知所有协作者删除本地旧克隆、重新 clone
#    （旧克隆里仍有泄露历史）

# 7. 若托管在 GitHub：历史泄露的 blob 可能仍可通过缓存/fork 访问，
#    可联系 GitHub Support 清除缓存视图；并删除所有含泄露历史的 fork。

# 8. 完成后删除 replacements.txt（其中含真实凭据）
```

## 四、后续预防

- `.env` 已加入 `.gitignore`，真实凭据只放 `.env`，勿提交。
- 提交前可用 gitleaks / trufflehog 做一次扫描。
- 考虑在 CI 中加入密钥扫描钩子（pre-commit + gitleaks）。
