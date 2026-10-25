# krkr2renpy

> 把 Kirikiri / KAG 引擎的 `.ks` 剧本，一键转换成 Ren'Py 可运行的工程。

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows-lightgrey)]()
[![AI Assisted](https://img.shields.io/badge/AI%20assisted-DeepSeek%20V4.1%20Flash-purple)]()

转换器会读取 `.ks`（KAG 语法）剧本，产出 `.rpy` 脚本、整理好的图片 / 音频 / 字体资源，并自动生成主菜单、存档、读档、历史记录、设置等完整界面，转换后可直接用 Ren'Py SDK 运行。

![GUI 截图](docs/images/gui.png)

---

## 特性

| 能力 | 说明 |
|---|---|
| 剧本转换 | `.ks`（KAG 语法）→ `.rpy`，支持文本、说话人、语音、背景、立绘、选项、跳转、条件分支 |
| 资源整理 | 自动索引并复制图片（背景 / 立绘 / UI）与音频（BGM / SE / 语音）到工程目录 |
| 汉化支持 | 可同时传入 `data.xp3`（原版）与 `patch.xp3`（汉化补丁）解包目录，汉化剧本优先 |
| 自动界面 | 生成完整 GUI：主菜单、快捷菜单、总菜单、存档 / 读档、历史记录、设置、确认框 |
| 显示适配 | 自动生成全屏、跳过未读、中文字体等配置，适配 Switch |
| 内存优化 | 按规则在场景切换处插入 `renpy.free_memory()`，降低内存占用 |
| 待办报告 | 生成 `conversion_report.md` 与 `todo_report.csv`，列出无法自动转换、需人工处理的内容 |

---

## 快速开始

```bash
# 1. 图形界面（推荐）
py converter/krkr2renpy.py

# 2. 命令行，转汉化版
py converter/krkr2renpy.py -i "D:\解包\data" --patch "D:\解包\patch" -o out
```

转换完成后，**务必**用 Ren'Py SDK 编译一次，否则旧缓存会覆盖新脚本：

```bash
renpy.exe "D:\...\out" compile
```

---

## 环境要求

只需要一个：**Python 3.x 完整版（含 tkinter）**。

1. 打开 <https://www.python.org/downloads/>，下载最新 Windows 安装包（如 3.12.x）。
2. 运行安装程序，**务必勾选「Add python.exe to PATH」**。
3. 验证：

```bash
py --version
py -c "import tkinter"    # 无报错即成功
```

> 无需 `pip install` 任何东西，转换器只用 Python 标准库。

依赖文件已随工具附带，与 `krkr2renpy.py` **放在同一目录**：

- `rules.json` —— 转换规则
- `fonts/1.ttf` —— 内置中文字体（转换时自动复制进工程）

---

## 使用方式

### 方式 A：图形界面（推荐）

```bash
py converter/krkr2renpy.py
```

打开后：

1. **游戏目录（data.xp3）**：填 data.xp3 解包出的目录（含 `scenario/`、`bgimage/` 等）。
2. **补丁目录（patch.xp3，可选）**：填 patch.xp3 解包出的目录（含汉化的 `scenario/*.ks`）。
3. **输出目录**：默认 `out`。
4. 点「▶ 开始转换」，看日志；完成后点「打开输出目录」。

### 方式 B：命令行

```bash
# 只转原版
py converter/krkr2renpy.py -i "D:\解包\data" -o out

# 转汉化版（推荐）：传入 patch 补丁目录
py converter/krkr2renpy.py -i "D:\解包\data" --patch "D:\解包\patch" -o out

# 试运行（只分析不写文件）
py converter/krkr2renpy.py -i "D:\解包\data" --patch "D:\解包\patch" -o out --dry-run

# 只转某几个文件
py converter/krkr2renpy.py -i "D:\解包\data" --only A000.ks,logo.ks -o out
```

---

## 转换结果

`out/game/` 里是完整工程：

```text
out/game/
├── script/*.rpy      # 剧本
├── screens.rpy       # 界面
├── options.rpy       # 配置
├── images/           # 图片
├── audio/            # 音频
└── fonts/            # 字体
```

### 关键一步：预编译

转换后**必须**用 Ren'Py SDK 编译一次，把 `.rpy` 编译成 `.rpyc`，否则旧缓存会覆盖新脚本：

```bash
renpy.exe "D:\...\out" compile
```

---

## 配置

所有配置都在 `rules.json` 里，常用几项：

| 配置项 | 位置 | 作用 |
|---|---|---|
| 游戏名 | `output.game_title` | 主菜单显示的标题 |
| 字体 | `output.font` | 界面字体路径（默认 `fonts/1.ttf`） |
| 全屏 / 跳过未读 | `output.fullscreen` / `output.skip_unseen` | 显示行为（默认都开） |
| 角色名映射 | `characters.overrides` | 汉字角色名 → 英文变量名 |
| 界面模板 | `templates.screens_rpy` | 生成的界面（一般不用改） |
| 自动 / 人工档 | `files.auto` / `files.manual` | 哪些 `.ks` 自动转、哪些标人工 |

**最常见的改动**是 `output.game_title`（改成游戏名）和 `characters.overrides`（补角色名）：

```json
"overrides": { "我": "ore", "花咲": "hanasaki", "美鳩": "mihato" }
```

改完重新转换即可。详细字段说明见 [`docs/rules_reference.md`](docs/rules_reference.md)。

---

## 常见问题

### 1. 界面中文显示成方块

→ 检查 `output.font` 是否指向了存在的字体；检查 `out/game/fonts/1.ttf` 是否被复制出来（转换日志里会打印「内置字体」）。

### 2. 汉化补丁没生效（还是日文）

→ 确认 `--patch` 指向的是 patch.xp3 **解包目录**（里面有 `scenario/` 子目录），而不是 patch.xp3 文件本身；确认补丁里的 `.ks` 文件名和原版一致。

### 3. 角色名是 `speaker_chara_2` 这种

→ 角色名是汉字、转换器无法自动罗马字化。在 `rules.json` 的 `characters.overrides` 里补上，重新转换即可。

### 4. 报告里有大量 TODO

→ 看 `conversion_report.md` 的「TODO 分类」。
- `flag`（CG 回想标记）、`manual-file`（相册 / 系统菜单）属于**需要人工实现的高级功能**，不影响主线剧情运行；
- `tjs` / `macro` 类需要确认是否影响关键剧情。

### 5. 转换后游戏进不去 / 报错

→ 先用 `renpy.exe out lint` 检查语法和资源引用；再跑 `renpy.exe out compile` 重新编译 `.rpyc`（这是最常见的「改了没生效」原因）。

### 6. 报「在 xxx 下没有找到 .ks 文件」

→ 输入的目录不对，它下面没有 `.ks` 文件。解包后 `.ks` 通常在 `scenario/` 子目录里，输入目录应选含 `scenario/` 的那一层（工具会自动递归查找）。

---

## 已知限制

本工具**不能** 100% 全自动，以下内容需人工确认或实现：

- **汉字角色名**无法自动罗马字化，需在 `characters.overrides` 手动补映射。
- **TJS 脚本 / 宏** 无法直接转换，`tjs` / `macro` 类 TODO 需确认是否影响关键剧情。
- **CG 回想 / 相册 / 系统菜单** 等以 `flag`、`manual-file` 标注的内容需人工实现，不影响主线。
- **复杂 KAG 表达式**、非标准标签可能被跳过，详见 `conversion_report.md`。

转换后请务必查看 `conversion_report.md` 与 `todo_report.csv`。

---

## 与打包工具的配合

转换器产出的是 `out/game/`（游戏内容）。要变成 Switch 能装的 `.nsp`：

1. 把 `out/game/` 的**内容**复制进打包源目录的 `romfs/Contents/game/`。
2. 打开打包器 `py packer/nsp_pack_gui.py`，点「打包 NSP」。

详见 [`docs/tutorial-nsp-packer.md`](docs/tutorial-nsp-packer.md)。

---

## 项目结构

```text
krkr2renpy/
├── converter/                  # 转换器主体
│   ├── krkr2renpy.py           # 入口（GUI + CLI）
│   ├── rules.json              # 转换规则（须与主脚本同目录）
│   └── fonts/1.ttf             # 内置中文字体
├── packer/                     # NSP 打包工具（可选）
│   └── nsp_pack_gui.py
├── docs/                       # 文档与截图
├── examples/                   # 样例输入 / 输出
└── tests/                      # 自动化测试
```

---

## 开发说明

本项目在开发过程中使用了 **DeepSeek V4.1 Flash** 辅助完成代码编写、转换规则梳理与文档整理。所有产出均经人工审核与测试。

欢迎 Issue / PR，详见 [`CONTRIBUTING.md`](CONTRIBUTING.md)。

---

## 致谢

- [Ren'Py](https://www.renpy.org/) —— 目标引擎
- Kirikiri / KAG —— 原剧本格式
- 内置字体来源与授权见 [`THIRD_PARTY_LICENSES.md`](THIRD_PARTY_LICENSES.md)

---

## License

[MIT](LICENSE) © 你的用户名
