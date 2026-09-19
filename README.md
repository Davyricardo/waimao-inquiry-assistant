# 外贸询盘助手 (Foreign Trade Inquiry Assistant)

面向外贸制造与出口企业的智能化询盘处理、客户画像、商业单证生成与全流程订单管理系统。

---

## 核心特性

- **询盘智能研判与回复**：对接 DeepSeek / 商业大模型，自动解析外贸询盘意向分值、产品特征与采购需求，生成专业中英文商务回复；
- **全链路客户与商机管理**：支持客户跟进记录、社媒开发潜客追踪、多触点沉淀与客户画像标签；
- **外贸单证与报价自动化**：自主产品库管理，一键生成出口商业发票 (Commercial Invoice)、形式发票 (PI)、装箱单 (Packing List) 及货代多维比价单；
- **外贸公司独立管理**：独立维护外贸出口公司抬头信息、境内外收款银行账户与外汇结算账户；
- **多账户与自动化轮询**：多邮箱 IMAP/SMTP 异步收发与轮询，安全凭证加密存储。

---

## 项目安全与三库隔离架构

为保证商业机密与云端生产安全，本项目采用**业务代码、云端运维与商业数据三向隔离架构**：

```
工作区/
├── 外贸询盘助手/          [本仓库] 核心业务代码库 (完全脱敏，可安全推送到 GitHub)
├── 外贸询盘助手-VPS部署/  [运维仓] 云端 VPS (Linux) 自动化部署编排、系统服务与探针
└── 外贸询盘助手-保密数据/ [金库仓] 真实客户数据库 (mailbutler.db)、单证附件、部署私钥与真实凭证
```

> **安全保证**：本仓库中**绝不包含**任何真实客户数据、商业附件、服务器 SSH 私钥或明文密钥，已配置严格的 `.gitignore` 保护机制，可放心推送到 GitHub 备份。

---

## 本地快速开始

### 1. 安装 Python 依赖
```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. 配置环境变量
- **推荐（推荐三库架构）**：保持同级目录存在 `外贸询盘助手-保密数据/`，程序启动时会自动寻找并挂载金库配置与数据库，实现零配置直连；
- **独立运行模式**：复制示例配置文件并根据需要修改：
  ```bash
  cp .env.example .env
  ```

### 3. 初始化与启动
```bash
# 生成初始化表结构与演示数据（可选）
python init.py
python seed_demo.py

# 启动 Web 服务
python serve.py
```
启动后访问本地控制台：[http://127.0.0.1:8090](http://127.0.0.1:8090)

---

## 自动化测试

项目内置完整的自动化回归测试套件：
```bash
python selftest.py                 # 核心流与引擎自测
python selftest_accounts.py        # 邮箱账户与凭据加密测试
python selftest_ai.py              # AI 研判与规则自测
python test_social_outreach.py     # 社媒潜客流测试
python test_freight.py             # 货运代理与比价测试
python test_orders.py              # 订单流水与品项测试
python test_products_and_docs.py   # 产品库与单证生成测试
python test_company_flow.py        # 公司管理与银行账户测试
```

---

## GitHub 备份操作指引

由于敏感资产已全部移入保密金库并配置了过滤规则，您可以放心在当前目录初始化 Git 并推送到您的 GitHub 个人或企业仓库：

```bash
# 1. 初始化本地仓库
git init

# 2. 检查暂存文件 (确认无敏感数据)
git add .
git status

# 3. 提交初始版本
git commit -m "feat: 外贸询盘助手系统核心业务代码初始提交"

# 4. 关联 GitHub 远程仓库并推送
git branch -M main
git remote add origin https://github.com/您的用户名/外贸询盘助手.git
git push -u origin main
```
