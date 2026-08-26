# Dockerfile — filament-manager image
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends sshpass openssh-client \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml ./
COPY fm ./fm
RUN pip install --no-cache-dir .
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "fm.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
