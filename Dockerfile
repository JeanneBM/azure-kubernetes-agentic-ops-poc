FROM python:3.12-alpine AS builder
RUN apk upgrade --no-cache
WORKDIR /build
COPY pyproject.toml README.md constraints.txt ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install -c constraints.txt .

FROM python:3.12-alpine AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apk upgrade --no-cache \
    && rm -rf /usr/local/lib/python3.12/site-packages/* /usr/local/bin/pip*
COPY --from=builder /install /usr/local
USER 10001
EXPOSE 8080
CMD ["uvicorn", "agentic_ops.split_app:build_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
