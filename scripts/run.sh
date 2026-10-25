#!/usr/bin/env bash
# 一键脚本（Linux / macOS / Git Bash / WSL）。
#
# 与 scripts/run.ps1 是同一套任务、同一套退出码语义。两份并存是因为
# CI 跑在 ubuntu-latest 上，而开发机是 Windows —— 只有一份的话，
# 总有一边要手敲命令，而手敲就会漏掉"必须用 .venv 里的 python"这类前提。
#
# **只做编排，不做判定**：退出码一律透传自 platform_cli.py
# （0 放行 / 1 拦住）。在脚本里再判一次会产生"脚本绿、门禁红"的分歧。
#
# 用法：
#   ./scripts/run.sh help
#   ./scripts/run.sh test
#   PERF_ENV=dev ./scripts/run.sh perf
set -euo pipefail

# 脚本所在目录的父目录 = 仓库根。不用 $PWD：那会随调用者所在目录变。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
PYTHON="$REPO_ROOT/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="$REPO_ROOT/.venv/Scripts/python.exe"  # Git Bash on Windows

# Windows 控制台默认 GBK，中文报告会直接抛 UnicodeEncodeError。
export PYTHONIOENCODING=utf-8

TASK="${1:-help}"
shift || true

ITEMS="${ITEMS:-20000}"
USERS="${USERS:-10 20}"
DURATION="${DURATION:-8}"

assert_venv() {
  if [ ! -x "$PYTHON" ]; then
    echo "找不到虚拟环境：$PYTHON" >&2
    echo "先跑：./scripts/run.sh setup" >&2
    exit 2
  fi
}

# 打印「这个任务等价于哪条命令」。
#
# 诚实边界：这**不是**实际执行的那份定义（执行在下面的 `case` 里），
# 所以这里说「共用一份定义，不可能漂移」是假的 —— 原先的注释就是这么写的。
# 真话是：两处必须人工保持一致，而 `./scripts/run.sh help` 打出来的文本
# 是给人抄的，抄错了会在 CI 之外得到一份和 CI 不一样的结果。
# 为什么不做成"一份定义两处用"：bash 里要把「命令 + 参数」当数据存放，
# 既要不经 eval（引号地狱）、又要能原样显示，代价远大于这两份几十行的清单。
# 这里改成如实描述，并让 shown 的路径用 POSIX 风格（原先写的是
# `& ".venv/bin/python"`，那是 PowerShell 的调用符，复制到 bash 里直接语法错）。
show_commands() {
  cat <<EOF
--- setup ---
  python -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -r requirements.txt -r requirements-dev.txt
--- env ---
  .venv/bin/python platform_cli.py env
--- test ---
  .venv/bin/python -m pytest tests/ -q
--- cov ---
  .venv/bin/python -m pytest tests/ -q --cov=bench --cov=metrics --cov=sut --cov=dashboard --cov=platform_cli --cov-report=term-missing --cov-fail-under=40
--- lint ---
  .venv/bin/python -m ruff check .
  .venv/bin/python -m mypy
  .venv/bin/python scripts/check_deps_sync.py
--- lock ---
  PIP_TOOLS_CACHE_DIR=.pip-tools-cache .venv/bin/python -m piptools compile requirements.txt --output-file requirements.lock --strip-extras
--- perf ---
  .venv/bin/python platform_cli.py run --label local-perf --items $ITEMS --step-users $USERS --step-duration $DURATION
--- probe ---
  .venv/bin/python platform_cli.py probe --label local-n1-probe --items $ITEMS --users 4 --duration $DURATION
--- dash ---
  .venv/bin/python platform_cli.py serve --port 8765
EOF
}

if [ "$TASK" = "help" ]; then
  echo "仓库根：$REPO_ROOT"
  echo "Python：$PYTHON"
  echo "环境档案：${PERF_ENV:-（未设 PERF_ENV = 默认行为）}"
  echo
  echo "可用任务：help setup env test cov lint lock perf probe dash all"
  echo
  show_commands
  echo
  echo "注：perf / probe 会真的起被测服务并压测，耗时与 CPU 占用都显著高于其它任务。"
  exit 0
fi

if [ "$TASK" = "setup" ]; then
  if [ ! -d "$REPO_ROOT/.venv" ]; then
    python -m venv "$REPO_ROOT/.venv"
  fi
  "$PYTHON" -m pip install --upgrade pip
  "$PYTHON" -m pip install -r "$REPO_ROOT/requirements.txt" -r "$REPO_ROOT/requirements-dev.txt"
  exit $?
fi

if [ "$TASK" = "all" ]; then
  for t in setup lint test; do
    echo
    echo "==> 任务：$t"
    # 这里必须先把退出码存下来再打印。
    # 原先是 `"$0" "$t" || { echo …; exit $?; }` —— `$?` 在那个位置读到的是
    # **echo 的 0**，于是 `all` 永远以 0 退出：lint 红了、test 红了，
    # 脚本照样报"全部通过"。这正是本仓最反对的那种假绿
    # （README 里那条「加了管道却忘了 pipefail」是同一个病）。
    "$0" "$t"
    code=$?
    if [ "$code" -ne 0 ]; then
      echo "任务 $t 失败（退出码 $code），停止后续任务。" >&2
      exit "$code"
    fi
  done
  echo
  echo "全部通过。"
  exit 0
fi

assert_venv
echo
echo "==> 任务：$TASK"
# 打印这个任务对应的命令（见 show_commands 上方的诚实边界说明）。
#
# 为什么不用 `sed -n "/^--- $TASK ---\$/,/^---/p" | sed '1d;$d'`：
# 末段（`--- dash ---`）后面没有下一个 `---`，`/^---/` 会一路吃到文件结束，
# 随后的 `$d` 就把**唯一那一条命令**当尾行删掉 —— 实测表现是
# `./scripts/run.sh dash` 只打印任务名、不打印任何命令，而且不报错。
# 用纯 bash 的"看到分隔符就停"来写，边界只有一种，不需要论证 sed 的范围语义。
printing=0
while IFS= read -r line; do
  case "$line" in
  "--- $TASK ---") printing=1 ;;
  "--- "*)
    # 进入另一段就停。第一段的标题本身也会命中这个分支，
    # 所以上面那一条必须排在前面。
    [ "$printing" -eq 1 ] && break
    ;;
  *)
    [ "$printing" -eq 1 ] && printf '  %s\n' "$line"
    ;;
  esac
done <<EOF
$(show_commands)
EOF

# 先关掉 errexit 再跑任务：本脚本要**读出**退出码并按 probe 的特殊语义解释它，
# 而 `set -e` 会在命令失败的那一刻直接退出，后面的 `code=$?` 变成死代码 ——
# 表现是"探针任务没有输出、脚本莫名其妙退出 1"。这正是"退出码即结论"
# 这条设计里最容易踩的坑：把结论码当成脚本错误吞掉。
set +e
case "$TASK" in
  env)   "$PYTHON" platform_cli.py env ;;
  test)  "$PYTHON" -m pytest tests/ -q ;;
  cov)   "$PYTHON" -m pytest tests/ -q \
           --cov=bench --cov=metrics --cov=sut --cov=dashboard --cov=platform_cli \
           --cov-report=term-missing --cov-fail-under=40 ;;
  lint)  rc=0
         "$PYTHON" -m ruff check . || rc=$?
         "$PYTHON" -m mypy || rc=$?
         "$PYTHON" scripts/check_deps_sync.py || rc=$?
         code=$rc; set -e
         exit $code ;;
  lock)  PIP_TOOLS_CACHE_DIR="$REPO_ROOT/.pip-tools-cache" \
           "$PYTHON" -m piptools compile requirements.txt \
             --output-file requirements.lock --strip-extras ;;
  perf)  "$PYTHON" platform_cli.py run --label local-perf --items "$ITEMS" \
           --step-users $USERS --step-duration "$DURATION" ;;
  probe) "$PYTHON" platform_cli.py probe --label local-n1-probe --items "$ITEMS" \
           --users 4 --duration "$DURATION" ;;
  dash)  "$PYTHON" platform_cli.py serve --port 8765 ;;
  *)     echo "未知任务：$TASK" >&2; set -e; exit 2 ;;
esac

code=$?
set -e
if [ "$TASK" = "probe" ]; then
  # 探针的"成功"就是"门禁拦住"（退出码 1）。不要翻成 0。
  echo
  echo "探针退出码：$code（1 = 门禁拦住 = 预期结果）"
fi
exit $code
