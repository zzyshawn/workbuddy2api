FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ⚠️ 新增模块时**务必同步加到这里** —— 漏一个就是启动即 ModuleNotFoundError。
# 曾经漏过 environments.py（被几乎所有模块 import），容器直接起不来。
COPY converter.py responses_adapter.py responses_projection.py \
     anthropic_adapter.py desensitize.py model_registry.py checkin.py \
     task_result.py growth_api.py task_scheduler.py environments.py \
     reasoning.py model_capabilities.py \
     state_snapshot.py \
     activity.py travel.py keepalive.py school.py blackcat.py ./

# 动态模型快照 + 签到留档 + 六类任务留档的落盘目录（容器内可写；想跨重启保留就挂个卷）
ENV CODEBUDDY2OPENAI_CACHE_DIR=/app/cache
RUN mkdir -p /app/cache

# 日志目录：compose 里 /logs 是挂载点，这里先建好，避免宿主目录权限/未挂载时报错
RUN mkdir -p /logs

EXPOSE 8787

CMD ["python3", "converter.py", "--host", "0.0.0.0", "--port", "8787", "--skip-check"]
