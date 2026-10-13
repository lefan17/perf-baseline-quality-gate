# 平台自身的镜像。一个镜像覆盖三种用法（跑测试 / 跑压测门禁 / 起看板）：
# 它们的依赖完全相同 —— 被测服务（SUT）由这个仓库自己用 uvicorn 起，
# 不存在「SUT 是个独立服务」这回事，所以不需要拆多阶段或多镜像。
#
# 用法见 docker-compose.yml 与 README「容器化」一节。
#
# ⚠️ 本镜像**未在开发机上验证构建**：开发机的 Docker daemon 起不来
#    （WSL 报 E_ACCESSDENIED）。本仓库只做到
#    `docker compose -f docker-compose.yml config --quiet` 静态校验通过。
#    首次真构建请在 daemon 可用的机器上执行 `docker build -t perf-gate .`。

FROM python:3.12-slim

# PYTHONIOENCODING 不是可选项：这个平台的报告里全是中文与 ✅/❌，
# 容器默认编码不是 UTF-8 时，渲染门禁结论会直接抛 UnicodeEncodeError
# （README 第 ⑦ 条踩坑里记过同类问题）。
ENV PYTHONIOENCODING=utf-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# 刻意**不设** PERF_ENV：默认「无档案」时平台行为与本地开发完全一致。
# 需要环境分层时在运行时注入，例如 `docker run -e PERF_ENV=dev ...`。
# 在镜像里写死一个档案，会让「同一份代码在容器内外量出不同数」变得无法解释。

WORKDIR /app

# 先只拷依赖清单再装：requirements.txt 不变时这一层命中缓存，
# 改代码不会触发重装。
COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

COPY . .

# 结果库与报告走可写卷（不写进镜像层，也不写进用户家目录）。
ENV QUALITY_DB=/app/var/quality.db
RUN mkdir -p /app/var /app/reports \
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8765

# 默认动作 = 跑平台自身用例并过套件门禁（等价于 CI 的 platform-tests job）。
# 退出码就是门禁结论：0 放行 / 1 拦住。
CMD ["python", "platform_cli.py", "suite", "--label", "docker-suite"]
