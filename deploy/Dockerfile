FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DUCKLE_EXECUTABLE=/usr/local/bin/duckle
WORKDIR /app
COPY deploy/requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r requirements.lock
COPY pyproject.toml README.md LICENSE /app/
COPY src /app/src
RUN pip install --no-cache-dir --no-deps . && useradd --uid 10001 --create-home worker && mkdir /state && chown worker /state
USER 10001:10001
ENTRYPOINT ["python", "-m", "witdem_langfuse.job"]
CMD ["--manifest", "/config/job.json", "--state", "/state"]
