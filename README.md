# rename
Rename the class and function names in the specified directorys

## 思路

主要用到find、ctags、awk、sed、sort四个命令的特性
- ctags支持的语言特性，查找类、函数、全局变量、宏等等
- awk语法特性找出对应字符串
- sed执行替换文件对应字符串
- sort对文件相同字符串去重

## 查看支持的语言

- 查看支持的语言以及对应语言的特性

ctags --list-kinds

- 查找指定目录下的cpp/cc/cxx/h结尾的文件

比如: find -E src -path "src/jolog/common" -prune -o -path "src/json" -prune -o -regex '.*\.(cpp|cc|cxx|h)' -type f

find -E '${src_dirs}' '${excludr_dir_str}' -regex '.*\.(cpp|cc|cxx|h)' -type f

- sed全局替换文件

gsed -i "s/\<$funcName\>/$dstFuncName/g"

- awk找出对应类名、函数名....

echo $line|awk -F " " '{print $1}' >> temp/funcs_exclude_name.txt

## rename.py（通用版，推荐）

`rename.sh` 依赖 `find -E` / `gsed` / BSD `ctags` 的方言组合，且把目录、黑名单硬编码在脚本里；`rename.py` 是与项目完全解耦的替代实现，可直接拷到任意工程使用。

```bash
# 生成配置模板（所有选项可落盘，配置内相对路径按配置文件所在目录解析）
python3 rename.py --init-config rename.config.json
python3 rename.py --config rename.config.json --apply

# 目录递归 + 统一前缀（默认 dry-run，只预览不写盘）
python3 rename.py ./src --prefix acme_ --dry-run
python3 rename.py ./src --prefix acme_ --apply

# 后缀 / 前后缀组合 / 多目录
python3 rename.py ./src ./include --suffix _v2 --dry-run
python3 rename.py ./src --prefix acme_ --suffix _impl --dry-run

# 人工 review 后的清单驱动（最安全），或精确映射 JSON / CSV / old=new
python3 rename.py ./src --symbols symbols.txt --prefix acme_ --apply
python3 rename.py ./src --map mapping.json --dry-run

# 幂等自检（检测重复前后缀）
python3 rename.py ./src --check --prefix acme_
```

要点：

- 默认 dry-run，`--apply` 才写盘；写盘前自动备份、git 工作区检查（未安装 git 时自动跳过）、原子写入、CRLF 保真
- 只替换代码中的标识符，自动跳过字符串/字符字面量、注释、raw string、JS 模板字符串、`#include` 行；C++14 数字分隔符 `1'000` 不会被误判为字符字面量
- 按扩展名自动适配 C/C++/Java/Kotlin/JS/TS/Python/Go/Rust 等词法规则；`--lexer plain` 可处理任意文本
- 符号来源可插拔：`--symbols` 清单、ctags、内置正则启发式（`--extract`）、`--symbol-pattern` 自定义正则
- 一次性 alternation 替换 + 跳过已改名符号，重复执行零改动；`--symbols` 白名单完全信任清单，不会误跳过本来就带该前缀的符号
- `--follow-symlinks` 会跟随符号链接文件与目录，并内置成环检测
- 产物在 `.rename-out/`：`symbols.txt`、`plan.csv`、`rejected.txt`（非法符号）、`skipped.txt`（幂等跳过）、`hit_counts.csv`、`changes.diff`、`report.json`
- `--apply` 中途若有文件写入失败，会继续处理其余文件并返回退出码 **3**，报告里含 `applied_files` / `write_errors` 与备份路径，需人工核对后回滚

完整参数与更多示例见 `python3 rename.py --help`。

## 安装与依赖

### Python 运行时

`rename.py` **零第三方 Python 依赖**，只使用 Python 3.9+ 标准库：`argparse`、`csv`、`dataclasses`、`difflib`、`fnmatch`、`json`、`os`、`pathlib`、`re`、`shutil`、`subprocess`、`sys`、`tempfile`、`time`（`from __future__ import annotations`，类型注解延迟求值）。因此**不需要任何依赖声明文件**，运行时不会安装任何第三方包；依赖状态由 `pyproject.toml` 的 `dependencies = []` 声明。

```bash
python3 --version                          # 需要 >= 3.9
python3 -m pip install .                   # 普通安装（兼容性最好）
python3 -m pip install -e .                # 可编辑安装，生成 rename-symbols 命令
rename-symbols ./src --prefix acme_ --dry-run
python3 rename.py --help                   # 或免安装直接运行
```

可编辑安装需要 `pip >= 21.3` / `setuptools >= 64`（PEP 660）。老环境（如 macOS 系统 Python 3.9 自带的 venv：pip 21.2.4 + setuptools 58）报 `editable mode currently requires a setuptools-based build` 时：

```bash
python3 -m pip install -U pip setuptools   # 升级构建工具（仅构建期需要）
python3 -m pip install .                   # 或直接用普通安装
```

安装过程会按 `pyproject.toml` 的 build-system 自动拉取 `setuptools>=61`，运行时依赖仍为零。

### 可选的系统工具

下面两个是**系统二进制，不能用 pip 安装**；不装也能完整运行。

**universal-ctags** —— 让 `--extract ctags` / `auto` 基于语法精确提取符号：

```bash
# macOS
brew install universal-ctags

# Ubuntu / Debian
sudo apt install universal-ctags          # 老版本源用 exuberant-ctags

# Fedora
sudo dnf install ctags

# Arch Linux
sudo pacman -S ctags

# Windows
scoop install universal-ctags            # 或从 universal-ctags GitHub Releases 下载
```

**git** —— 仅用于 `--apply` 前的工作区脏检查，非 git 仓库自动跳过，不强制要求；用系统包管理器或官方安装包即可。安装后可用 `ctags --version`、`git --version` 确认。

> ⚠️ macOS 自带的 `/usr/bin/ctags` 是 Xcode BSD 版，不支持 `--*-kinds`。装完 brew 版要用 `which ctags` 确认 brew 路径排在 `/usr/bin` 之前，否则 `--extract auto` 会回退到内置正则启发式并打印告警。追求精确时推荐直接用 `--symbols` 提供人工 review 过的清单。

### 可选的开发工具

```bash
python3 -m pip install ruff
ruff check rename.py
```

> 原始 `rename.sh` 另依赖 `find -E`(BSD) + `gsed`/`egrep`(GNU) 的方言混搭；若要跑它，macOS 需 `brew install gnu-sed`，且 BSD `ctags` 仍不兼容 `--c++-kinds`。推荐直接使用 `rename.py`。


