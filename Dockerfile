FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

COPY gemini_web2api.py ./
COPY config.example.json ./
COPY README.md README_CN.md LICENSE ./
COPY logo.png ./

EXPOSE 8080

CMD ["sh", "-c", "python gemini_web2api.py --port ${PORT:-8080}"]
