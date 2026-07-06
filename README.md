# 历史粘贴板

一个面向 Windows 的本地离线剪贴板历史工具。软件运行期间会记录复制过的文字和图片，并保存到用户指定的数据文件夹；用户可以搜索、排序、置顶、删除、批量管理，也可以把历史内容再次复制回剪贴板。

## 功能特性

- 记录文字、多行文字、网址、截图和图片。
- 图片保存原图和缩略图，支持双击放大查看。
- 支持搜索、时间升序/降序、类型筛选、置顶和删除。
- 支持图片备注，支持 OCR 识别结果搜索。
- OCR 为可选组件，主程序默认保持轻量。
- 支持批量选择、批量复制文字汇总、批量导出和批量删除。
- 支持暂停记录、保留天数、最大历史数量和单张图片大小上限。
- 支持数据备份、恢复、诊断报告和本地状态检查。
- 支持最小化到系统托盘，支持 `Ctrl+Alt+V` 呼出窗口。

## 项目结构

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

以下内容不进入代码仓库：`dist/`、`build/`、用户历史数据、测试保存文件夹、本机配置文件和打包压缩包。

## 开发运行

```powershell
python -m pip install -r requirements-dev.txt
python main.py
```

也可以双击：

```text
run_app.bat
```

## 检查与测试

```powershell
python -c "from pathlib import Path; files=[Path('main.py'),Path('ocr_worker.py')]+list(Path('clipboard_history').glob('*.py'))+list(Path('tests').glob('*.py')); [compile(p.read_text(encoding='utf-8'), str(p), 'exec') for p in files]; print('syntax ok')"
python -B -m unittest discover -s tests
python main.py --self-test
```

需要构造大量测试记录时：

```powershell
python main.py --perf-seed 测试保存文件夹 500
```

## 打包

```powershell
build_exe.bat
```

打包后会生成：

- `dist/历史粘贴板/`：轻量主程序发布目录。
- `dist/OCR组件/ocr_runtime/`：可选 OCR 组件。

发布给普通用户时，请发送整个 `dist/历史粘贴板/` 文件夹，不要只发送单独的 exe，因为 `_internal/` 目录里包含运行依赖。

如果需要 OCR，把 `ocr_runtime` 文件夹复制到 `历史粘贴板.exe` 所在目录旁边；不需要 OCR 时只发送轻量主程序文件夹即可。

## GitHub 发布建议

代码仓库只保存源码、文档、测试和构建脚本。打包好的 exe、OCR 组件和 zip 文件建议放到 GitHub Releases：

```text
Release v1.0.0
├─ history-clipboard-lite-v1.0.0.zip
└─ history-clipboard-ocr-runtime-v1.0.0.zip
```

不要把自己的剪贴板历史数据、图片缓存、配置文件或备份文件上传到公开仓库。

## 隐私说明

本工具默认本地运行，不需要账号，不使用云同步。历史数据保存在用户选择的数据文件夹中。发送软件给别人时，只发送发布目录，不要发送自己的数据文件夹。

## 许可证

当前尚未选择开源许可证。如需公开开源，可以后续添加 MIT License 或其他许可证。
