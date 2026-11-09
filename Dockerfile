FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY proxima_bot/ ./proxima_bot/
COPY docs/tokora-openapi.json ./docs/tokora-openapi.json
RUN mkdir -p /app/data && chown 65532:65532 /app/data
USER 65532
CMD ["python","-m","proxima_bot"]
