# lit-digest · 文献自动订阅

每天自动抓取 arXiv + PubMed 最新论文 → 关键词粗筛 → DeepSeek 精筛+中文摘要 → 下载 PDF → 发到你的邮箱。

面向方向：**脑科学 / 波动光学（光学观测脑）/ 电磁学 / AI 用于成像**。

## 一、安装依赖

```powershell
cd E:\deepseek\lit-digest
pip install -r requirements.txt
```

## 二、试运行（先不发邮件）

```powershell
python main.py
```

默认 `config.yaml` 里 `dry_run: true`，只打印结果 + 下载 PDF 到 `pdfs\` 目录，**不会发邮件**。确认结果 OK 后：

1. 在 `config.yaml` 把 `dry_run` 改成 `false`；
2. 或临时用 `python main.py --send` 真正发一次。

## 三、获取 Outlook 应用密码（发邮件必需）

Outlook 已经不能用登录密码发信，需要「应用密码」：

1. 打开 https://account.microsoft.com/security ，登录你的 `kouhengyue@outlook.com`；
2. 开启**双重验证**（两步验证）；
3. 回到「安全性」页面 → 找到「**应用密码**」→ 点「创建新的应用密码」；
4. 复制那串 16 位密码；
5. 打开本目录的 `.env`，把 `SMTP_PASSWORD=` 后面填上这串密码，保存。

> 如果你实在弄不出 Outlook 应用密码，可以改用 QQ 邮箱：把 `.env` 的 SMTP 改成
> `smtp.qq.com` / `465`，`SMTP_USER` 改成你的 QQ 邮箱，`SMTP_PASSWORD` 用 QQ 邮箱的「授权码」。

## 四、每天定时运行（Windows 任务计划程序）

1. 按 `Win` 键，输入「任务计划程序」并打开；
2. 右侧「创建基本任务」→ 名称填 `lit-digest`；
3. 触发器选「每天」，设个时间（比如早上 8:00）；
4. 操作选「启动程序」：
   - 程序：`C:\Python314\python.exe`（或你 `python` 的实际路径，用 `where python` 查）
   - 参数：`E:\deepseek\lit-digest\main.py`
   - 起始于：`E:\deepseek\lit-digest`
5. 完成即可，每天自动跑。

## 五、文件说明

| 文件 | 作用 |
|------|------|
| `main.py` | 主脚本 |
| `config.yaml` | 关键词 / 分类 / 频率 / 邮件地址 / 是否试运行 |
| `.env` | 密钥（DeepSeek Key、SMTP 密码），**不要提交到 Git** |
| `pdfs/` | 下载的 PDF 存放处 |
| `requirements.txt` | Python 依赖 |
