# 校园小助手 · Render 云端部署说明

> 目标平台：Render 免费版（xxx.onrender.com 永久域名，云端 7×24）
> 特性：免费 15 分钟无访问自动休眠（下次打开等 30-60 秒冷启动）；免费版磁盘重置后对话记录清空，知识库会自动重建（首次启动自动导入仓库内的爬取数据）

## 一、云端适配（已完成，随仓库分发）

| 适配项 | 说明 |
|---|---|
| PORT 环境变量 | 平台注入端口自动生效，绑定 0.0.0.0 |
| 密钥走环境变量 | `DEEPSEEK_API_KEY` / `ACCESS_TOKEN` / `AMAP_KEY` / `TAVILY_API_KEY` / `SCHOOL_ID`，不落盘 |
| lock_config | render.yaml 默认锁定 base_url（防密钥外发） |
| 知识库自动播种 | backend/seed_knowledge.py：SQLite 为空时自动导入 output/byau_crawl_state.json（485 页官网数据）+ 部门电话簿 |
| render.yaml | Render 读取后自动配置构建/启动命令 |

## 二、部署步骤（需本人操作）

### 步骤 1：注册 GitHub 并建仓库
1. 访问 github.com 注册账号
2. 点右上角 `+` → New repository
3. 名称填 `campus-agent`，选 **Private**，其余默认，点 Create（不要勾选初始化 README）

### 步骤 2：推送代码
在项目目录执行（或让助手代跑，首次推送时会弹浏览器登录 GitHub 授权）：
```
git remote add origin https://github.com/<你的用户名>/campus-agent.git
git push -u origin master
```

### 步骤 3：注册 Render 并创建服务
1. 访问 render.com，选 **Log in with GitHub** 登录
2. Dashboard → New → **Web Service** → 选 `campus-agent` 仓库 → Connect
3. 确认信息（render.yaml 会自动生效：Python / 免费版 / 构建与启动命令已填好）
4. Environment Variables 处添加：
   - `DEEPSEEK_API_KEY` = 你的 sk- 开头密钥（不填则演示模式）
   - `ACCESS_TOKEN` = 访问口令（必填！不填则公网无口令裸奔）
5. 点 Create Web Service，等 3-5 分钟构建完成
6. 顶部拿到 `https://campus-agent.onrender.com` 公网地址

### 步骤 4：验证
- 打开公网地址 → 应弹口令框 → 输入 ACCESS_TOKEN 进入
- 问"请假找谁""资助中心电话" → 应命中官网爬取知识库

## 三、本地开发不受影响

本地 `python main.py`（或 启动.bat）仍读 `backend/config.json`、仍开 127.0.0.1:8300、仍弹浏览器；环境变量只在云端生效。

## 四、常见问题

- **休眠唤醒慢**：免费版特性，首次打开等 30-60 秒
- **对话记录丢失**：免费版磁盘重置，属预期；知识库自动重建无需干预
- **更新代码**：本地改完 → `git add . && git commit -m "xxx" && git push` → Render 自动重新部署
- **想固定国内低延迟**：后续可换付费方案或国内云服务器

> AI生成
