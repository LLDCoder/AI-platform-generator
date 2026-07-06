ARG PYTHON_BASE_IMAGE=docker.m.daocloud.io/library/python:3.11-slim
FROM ${PYTHON_BASE_IMAGE}

ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY services/ai_generation_service/pyproject.toml /app/pyproject.toml
COPY services/ai_generation_service/app /app/app
RUN pip install --no-cache-dir -i ${PIP_INDEX_URL} -e .

EXPOSE 8091

CMD ["fastapi", "run", "--host", "0.0.0.0", "--port", "8091", "app/main.py"]
