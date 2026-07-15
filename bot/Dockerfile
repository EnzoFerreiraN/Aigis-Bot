FROM python:3.12-slim

# Instala ffmpeg (necessário para streaming de áudio)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copia e instala dependências Python primeiro (camada cacheável)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia o restante do código
COPY . .

# Roda como usuário não-privilegiado (hardening — evita processo do bot,
# yt-dlp e ffmpeg rodando como root dentro do container)
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

CMD ["python", "main.py"]
