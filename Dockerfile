FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY converter.py responses_adapter.py responses_projection.py \
     anthropic_adapter.py desensitize.py model_registry.py checkin.py \
     task_result.py growth_api.py task_scheduler.py \
     activity.py travel.py keepalive.py school.py blackcat.py ./

# 动态模型快照 + 签到留档 + 六类任务留档的落盘目录（容器内可写；想跨重启保留就挂个卷）
ENV CODEBUDDY2OPENAI_CACHE_DIR=/app/cache
RUN mkdir -p /app/cache

EXPOSE 8787

CMD ["python3", "converter.py", "--host", "0.0.0.0", "--port", "8787", "--skip-check"]
