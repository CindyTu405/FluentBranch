FROM python:3.11-slim

WORKDIR /app

RUN pip install uv

# 編譯相依（psycopg2 需要）
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-hashes -o requirements.lock.txt \
    && uv pip install --system -r requirements.lock.txt

# compose 會掛載原始碼，但映像仍需 COPY 以正規打包
COPY . .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", ]