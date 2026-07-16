# ⚡ TokkenShop 生图工作台

**tokkenshop-image-studio** — 零依赖的本地 AI 生图工具：一个 Python 文件 + 一个 HTML 文件，`python3 server.py` 即可调用 gpt-image-2 生图。
支持 OpenAI 及任意 OpenAI 兼容中转接口。

> ⚡ 由 [**TokkenShop**](https://tokken.cc) 中转站提供支持 — 注册即可获取 API Key，请求地址填 `https://tokken.cc/v1`

## ✨ 功能

- **文生图 + 图生图**：拖拽 / 点击 / 粘贴参考图（最多 10 张，自动压缩），自动切换 `images/edits` 接口
- **任意尺寸**：内置方形 / 横竖版 / 高清等预设，支持完全自定义宽 × 高（16 – 8192 px）
- **参数齐全**：质量（auto/high/medium/low）、单次 1–4 张、PNG/WebP/JPEG、透明背景
- **历史持久化**：图片自动保存到 `outputs/`，提示词等元数据存 `outputs/history.jsonl`，重启不丢
- **顺手的细节**：并发任务卡片实时计时、失败一键重试、灯箱预览（← → 翻页）、一键下载 / 复制提示词 / 再来一张 / 成品转参考图继续改、明暗主题、⌘⏎ 快速生成
- **接口友好**：地址自动补全（填 `https://host`、`https://host/v1` 或完整端点均可）、404 自动回退备选路径、设置内一键测试连接

## 🚀 快速开始

```bash
git clone https://github.com/<你的用户名>/tokkenshop-image-studio.git
cd tokkenshop-image-studio
python3 server.py          # 默认端口 8000
python3 server.py 9000     # 或指定端口
```

浏览器打开 http://127.0.0.1:8000 ，点右上角 **⚙️ 接口设置** 填入 API Key 和请求地址即可生图。

只需要 Python 3.8+，**无任何第三方依赖**，无需 pip install。

## ⚙️ 配置

两种方式任选（网页填写优先）：

| 方式 | 说明 |
|------|------|
| 网页设置 | 右上角 ⚙️，保存在浏览器 localStorage |
| `.env` 文件 | 复制 `.env.example` 为 `.env` 填写，适合固定部署 |

```env
API_KEY=sk-xxxxxxxx
BASE_URL=https://tokken.cc/v1
MODEL=gpt-image-2
```

## 📁 项目结构

```
server.py     # 本地服务：静态页面 + API 代理 + 历史管理（纯 stdlib）
index.html    # 全部界面（无构建、无依赖）
outputs/      # 生成的图片与 history.jsonl（已 gitignore）
```

想调整尺寸预设？改 `index.html` 顶部的 `SIZE_PRESETS` 数组即可，一行一个。

## ❓ 常见问题

- **上游返回 404**：报错里会显示实际请求的完整 URL，对照服务商文档检查路径；也可以直接把「请求地址」填成完整端点（以 `/images/generations` 结尾），服务端将不再拼接。
- **上游要求重定向**：按报错提示把请求地址改成重定向目标地址即可（POST 不会自动跟随重定向，避免请求体丢失）。
- **Key 存在哪里，安全吗**：只存在你自己的浏览器 localStorage 或本地 `.env` 中，服务只监听 127.0.0.1，不会上传到任何地方。

## License

MIT
