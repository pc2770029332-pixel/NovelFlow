# NovelFlow · AI 小说全流程创作工作台

> 一个本地运行的 AI 小说创作系统：**背景设计 → 章节细纲 → 主笔创作 → 润色修改 → 自动归档**，五步流水线一条龙完成。

## ✨ 能做什么

- 🎭 五个 AI 角色分工：背景设计师、细纲设计师、主笔、润色编辑、归档助理
- 📡 实时流式输出：边生成边看，不用干等
- 📚 逐章创作：每章自动带上「世界观 + 细纲 + 前情提要」
- ✨ 自动润色：初稿写完自动逐章润色
- 📦 一键归档：自动生成全书文档，可下载
- 💾 本地保存：所有产物存在 `output/` 文件夹

---

## 🚀 新手 3 步启动（不用会代码）

### 第 0 步：装 Python（只需要一次）

- Windows：去 https://www.python.org/downloads/ 下载，安装时**勾选 Add Python to PATH**。
- Mac：一般自带，终端输入 `python3 --version` 能显示版本号即可。

### 第 1 步：下载并解压

点这个链接下载整个项目（ZIP）：

**https://github.com/pc2770029332-pixel/NovelFlow/archive/refs/heads/main.zip**

下载后解压到任意地方（桌面也可以）。

### 第 2 步：一键启动

- **Windows**：进入解压出来的 `NovelFlow` 文件夹，**双击 `启动.bat`**。
- **macOS / Linux**：终端进入文件夹，运行 `bash 启动.sh`。

第一次会自动安装依赖，等它跑完，窗口里出现 `http://127.0.0.1:8021` 就成功了。

> ⚠️ 那个黑色/终端窗口**不要关**，关了程序就停了。

### 第 3 步：填 API Key（最关键！）

1. 浏览器打开 **http://127.0.0.1:8021**
2. 在右侧「🤖 AI 角色配置 → ⚙️ 默认配置」里填三样东西：
   - **API Key**：你的密钥
   - **API 端点 (Base URL)**：见下表
   - **模型**：见下表
3. 填书名、点「🚀 开始全流程创作」即可。

**没有 API Key？推荐用 DeepSeek（国内、便宜、注册就送额度）：**

| 服务商 | 注册地址 | Base URL | 模型 |
|--------|----------|----------|------|
| DeepSeek（推荐） | https://platform.deepseek.com | `https://api.deepseek.com/v1` | `deepseek-chat` |
| OpenAI | https://platform.openai.com | `https://api.openai.com/v1` | `gpt-4o-mini` |
| Kimi/Moonshot | https://platform.moonshot.cn | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` |

> 注册后在「API Keys / 密钥管理」里创建一个 Key，是一串 `sk-` 开头的字符，复制粘贴进来即可。

---

## 🔧 常见问题

**Q：页面能打开，但点「开始」就报错？**
99% 是 API Key 没填对，或 Base URL / 模型名填错了。对着上面表格检查一遍。

**Q：双击 `启动.bat` 闪一下就没了？**
说明没装 Python，或装的时候没勾选 `Add Python to PATH`。重装一遍 Python 即可。

**Q：作品存在哪里？**
在项目目录下的 `output/` 文件夹里。

---

## 🏗️ 项目结构

```
NovelFlow/
├── 启动.bat / 启动.sh   # 一键启动脚本
├── run.py               # 启动入口
├── requirements.txt     # 依赖
├── .env.example         # 环境变量示例
├── src/
│   ├── main.py          # FastAPI 服务（REST + SSE + 静态页面）
│   ├── llm_client.py    # OpenAI 兼容 LLM 客户端
│   ├── workflow/
│   │   ├── engine.py    # 五阶段工作流引擎
│   │   └── prompts.py   # 提示词体系
│   └── static/          # 前端页面
└── output/              # 创作产物（自动生成）
```

## 📄 License

MIT
