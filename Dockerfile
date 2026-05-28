FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY gemini_web2api.py ./
COPY gemini_web2api ./gemini_web2api
COPY config.example.json ./
COPY README.md README_CN.md LICENSE ./
COPY logo.png ./

EXPOSE 8080

CMD ["sh", "-c", "python gemini_web2api.py --port ${PORT:-8080}"]
