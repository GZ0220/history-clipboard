# 历史粘贴板

一个面向 Windows 的本地离线剪贴板历史工具。软件运行期间会记录复制过的文字和图片，并保存到用户指定的数据文件夹；用户可以搜索、排序、置顶、删除、批量管理，也可以把历史内容再次复制回剪贴板。

## 主要功能

- 记录文字、多行文字、网址、截图和图片。
- 图片保存原图和缩略图，支持双击放大查看。
- 支持搜索、时间升序/降序、类型筛选、置顶和删除。
- 支持图片备注，支持 OCR 识别结果搜索。
- OCR 为可选组件，主程序默认保持轻量。
- 支持批量选择、批量复制文字汇总、批量导出和批量删除。
- 支持暂停记录、保留天数、最大历史数量和单张图片大小上限。
- 支持数据备份、恢复、诊断报告和本地状态检查。
- 支持最小化到系统托盘，支持 `Ctrl+Alt+V` 呼出窗口。

## 下载与安装

在 GitHub Releases 页面下载：

- `history-clipboard-lite-v1.0.0.zip`：主程序，日常使用只需要下载这个。
- `history-clipboard-ocr-runtime-v1.0.0.zip`：可选 OCR 组件，需要图片文字识别时再下载。

主程序使用方式：

1. 下载 `history-clipboard-lite-v1.0.0.zip`。
2. 解压整个压缩包。
3. 双击 `历史粘贴板.exe`。
4. 首次启动时选择一个数据保存文件夹。

请不要只单独移动 `历史粘贴板.exe`，它旁边的 `_internal/` 文件夹也是运行所需内容。

## OCR 组件

如果需要图片文字识别：

1. 下载 `history-clipboard-ocr-runtime-v1.0.0.zip`。
2. 解压后得到 `ocr_runtime/` 文件夹。
3. 把 `ocr_runtime/` 放到 `历史粘贴板.exe` 同级目录。
4. 重新打开软件后，图片卡片里的“识别文字”功能会自动启用。

不需要 OCR 时，可以不下载 OCR 组件。

## 数据与隐私

- 本工具默认本地运行，不需要账号，不使用云同步。
- 历史数据保存在用户自己选择的数据文件夹中。
- 发送软件给别人时，只发送解压后的程序文件夹，不要发送自己的数据文件夹。
- 如果记录了敏感内容，可以在软件中删除对应历史，或开启敏感文字过滤。

## 开发者说明

项目结构：

```text
clipboard_history/        应用源码
assets/                   图标和本地视觉资源
docs/                     需求、设计、打包和质量说明
tests/                    自动化测试
main.py                   主程序入口
ocr_worker.py             可选 OCR 组件入口
build_exe.bat             Windows 打包脚本
run_app.bat               开发运行脚本
*.spec                    PyInstaller 打包配置
requirements-dev.txt      开发和打包依赖
```

开发运行：

```powershell
python -m pip install -r requirements-dev.txt
python main.py
```

检查与测试：

```powershell
python -c "from pathlib import Path; files=[Path('main.py'),Path('ocr_worker.py')]+list(Path('clipboard_history').glob('*.py'))+list(Path('tests').glob('*.py')); [compile(p.read_text(encoding='utf-8'), str(p), 'exec') for p in files]; print('syntax ok')"
python -B -m unittest discover -s tests
python main.py --self-test
```

打包：

```powershell
build_exe.bat
```

以下内容不进入代码仓库：`dist/`、`build/`、用户历史数据、测试保存文件夹、本机配置文件和打包压缩包。

## 许可证

当前尚未选择开源许可证。如需公开开源，可以后续添加 MIT License 或其他许可证。
