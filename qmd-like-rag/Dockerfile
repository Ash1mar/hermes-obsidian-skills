FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir . \
    && groupadd --gid 10001 qmd \
    && useradd --uid 10001 --gid qmd --no-create-home --shell /usr/sbin/nologin qmd

USER qmd:qmd
EXPOSE 8781
ENTRYPOINT ["qmd-like-rag"]
CMD ["--help"]
